from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import ctypes
import json
import os
from pathlib import Path
import sqlite3
import threading
from typing import Callable, Protocol
import uuid

from .models import EvidenceClass, TaskState
from .policy import is_real_progress
from .store import InvalidTransition, StateStore, TaskRecord


@dataclass(frozen=True)
class PollReport:
    tasks_checked: int
    progress_events: int
    terminal_events: int
    recovery_attempts: int
    transient_errors: int


class ObserverProtocol(Protocol):
    def observe(self, session_id: str, since_rowid: int, limit: int = 1000): ...


class RecoveryProtocol(Protocol):
    def recover(self, task_id: str, **kwargs): ...


class NotifierProtocol(Protocol):
    def notify(self, title: str, message: str) -> bool: ...


class AuditProtocol(Protocol):
    def write(self, kind: str, reason: str, **fields: object) -> None: ...


def load_enabled(config_path: Path) -> bool:
    if not config_path.exists():
        return False
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return data.get("enabled") is True


def set_enabled(config_path: Path, enabled: bool) -> None:
    config_path.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, object] = {}
    if config_path.exists():
        try:
            loaded = json.loads(config_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data.update(loaded)
        except (OSError, json.JSONDecodeError):
            pass
    data["enabled"] = enabled
    target = config_path.with_suffix(".tmp")
    target.write_text(
        json.dumps(data, indent=2) + "\n",
        encoding="utf-8",
    )
    target.replace(config_path)


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            process_query_limited_information,
            False,
            pid,
        )
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except (OSError, ProcessLookupError):
        return False
    return True


class DaemonLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.pid = os.getpid()
        self.token = uuid.uuid4().hex
        self.acquired = False

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"pid": self.pid, "token": self.token}).encode("utf-8")
        for _ in range(2):
            try:
                descriptor = os.open(
                    self.path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
            except FileExistsError:
                try:
                    existing = json.loads(self.path.read_text(encoding="utf-8"))
                    existing_pid = int(existing.get("pid", 0))
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    existing_pid = 0
                if _pid_is_alive(existing_pid):
                    return False
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
                continue
            try:
                os.write(descriptor, payload)
            finally:
                os.close(descriptor)
            self.acquired = True
            return True
        return False

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            existing = json.loads(self.path.read_text(encoding="utf-8"))
            if existing.get("token") == self.token:
                self.path.unlink(missing_ok=True)
        except (OSError, json.JSONDecodeError):
            pass
        self.acquired = False


class WatchdogService:
    def __init__(
        self,
        *,
        store: StateStore,
        observer: ObserverProtocol,
        recovery_controller: RecoveryProtocol,
        notifier: NotifierProtocol | None = None,
        restart_probe: Callable[[], bool] | None = None,
        audit_logger: AuditProtocol | None = None,
    ) -> None:
        self.store = store
        self.observer = observer
        self.recovery_controller = recovery_controller
        self.notifier = notifier
        self.restart_probe = restart_probe
        self.audit_logger = audit_logger

    def poll_once(self, now: datetime | None = None) -> PollReport:
        current_time = (now or datetime.now(UTC)).astimezone(UTC)
        checked = progress_events = terminal_events = recovery_attempts = errors = 0
        for task in self.store.list_tasks(limit=50):
            if task.state in {
                TaskState.COMPLETED,
                TaskState.FAILED,
                TaskState.CIRCUIT_OPEN,
                TaskState.PENDING_CONFIRMATION,
            }:
                continue
            checked += 1
            try:
                batch = self.observer.observe(
                    task.session_id,
                    task.last_log_rowid,
                    limit=1000,
                )
                self.store.update_log_cursor(task.task_id, batch.last_rowid)
                terminal = next(
                    (
                        item
                        for item in batch.observations
                        if item.kind == "backend_terminal"
                        and item.evidence_class is EvidenceClass.POSITIVE_TERMINAL
                    ),
                    None,
                )
                if terminal is not None:
                    self._complete_from_positive_terminal(task)
                    terminal_events += 1
                    continue

                progress = [item for item in batch.observations if is_real_progress(item)]
                if progress:
                    self._record_progress(task, progress[-1].kind)
                    progress_events += len(progress)
                    continue

                refreshed = self.store.get_task(task.task_id)
                if self._threshold_expired(refreshed, current_time):
                    if refreshed.state is not TaskState.SUSPECT:
                        self.store.transition(
                            task.task_id,
                            TaskState.SUSPECT,
                            "rolling no-progress threshold expired",
                        )
                    restart_requested = False
                    if self.restart_probe is not None:
                        restart_requested = bool(self.restart_probe())
                    decision = self.recovery_controller.recover(
                        task.task_id,
                        restart_requested=restart_requested,
                    )
                    if self.audit_logger is not None:
                        self.audit_logger.write(
                            "recovery-decision",
                            decision.reason,
                            task_id=task.task_id,
                            state=decision.state.value,
                            action=decision.action,
                            restart_requested=restart_requested,
                        )
                    if self.notifier is not None and decision.state in {
                        TaskState.CIRCUIT_OPEN,
                        TaskState.PENDING_CONFIRMATION,
                    }:
                        self.notifier.notify(
                            "Codex Resilience Watchdog",
                            f"Task {task.task_id}: {decision.reason}",
                        )
                    recovery_attempts += 1
            except (sqlite3.Error, OSError, ValueError, InvalidTransition) as error:
                errors += 1
                if self.audit_logger is not None:
                    self.audit_logger.write(
                        "transient-error",
                        type(error).__name__,
                        task_id=task.task_id,
                    )
        return PollReport(checked, progress_events, terminal_events, recovery_attempts, errors)

    def run(
        self,
        stop_event: threading.Event,
        *,
        poll_seconds: float = 30.0,
        enabled_check: Callable[[], bool] | None = None,
        enabled_check_seconds: float = 1.0,
    ) -> int:
        backoff = poll_seconds
        while not stop_event.is_set() and (
            enabled_check is None or enabled_check()
        ):
            report = self.poll_once()
            backoff = min(300.0, backoff * 2) if report.transient_errors else poll_seconds
            remaining = backoff
            while remaining > 0:
                if stop_event.is_set() or (
                    enabled_check is not None and not enabled_check()
                ):
                    return 0
                interval = min(max(enabled_check_seconds, 0.01), remaining)
                stop_event.wait(interval)
                remaining -= interval
        return 0

    def _record_progress(self, task: TaskRecord, evidence: str) -> None:
        if task.state in {
            TaskState.ARMED,
            TaskState.SUSPECT,
            TaskState.RECONCILING,
            TaskState.RESUMING,
        }:
            self.store.transition(task.task_id, TaskState.RUNNING, evidence)
        self.store.record_heartbeat(task.task_id, evidence)

    def _complete_from_positive_terminal(self, task: TaskRecord) -> None:
        current = self.store.get_task(task.task_id)
        if current.state is TaskState.ARMED:
            self.store.transition(task.task_id, TaskState.RUNNING, "terminal preceded progress")
        current = self.store.get_task(task.task_id)
        if current.state is TaskState.RECOVERY_READY:
            self.store.transition(task.task_id, TaskState.RESUMING, "terminal observed")
        self.store.transition(
            task.task_id,
            TaskState.COMPLETED,
            "positive backend terminal event",
        )

    @staticmethod
    def _threshold_expired(task: TaskRecord, now: datetime) -> bool:
        anchor_text = task.last_progress_at or task.updated_at
        anchor = datetime.fromisoformat(anchor_text)
        return (now - anchor).total_seconds() >= task.threshold_seconds
