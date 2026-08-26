from __future__ import annotations

from datetime import UTC, datetime
import json
import os
from pathlib import Path
from typing import Protocol
import uuid

from .backends import BackendResult, CapabilityReport
from .models import EvidenceClass, RecoveryDecision, TaskState
from .observer import ObservationBatch
from .policy import CircuitSnapshot, circuit_reason, fault_fingerprint, may_auto_replay
from .reconcile import ResultReconciler
from .store import InvalidTransition, StateStore


class ObserverProtocol(Protocol):
    def observe(
        self, session_id: str, since_rowid: int, limit: int = 1000
    ) -> ObservationBatch: ...


class CliBackendProtocol(Protocol):
    def capabilities(self) -> CapabilityReport: ...

    def resume_read_only(self, session_id: str, prompt: str) -> BackendResult: ...


class ProcessBackendProtocol(Protocol):
    def verified_executable(self) -> Path | None: ...

    def restart_once(self, expected_executable: Path | None) -> BackendResult: ...


class RecoveryController:
    def __init__(
        self,
        *,
        store: StateStore,
        observer: ObserverProtocol,
        reconciler: ResultReconciler,
        cli_backend: CliBackendProtocol,
        process_backend: ProcessBackendProtocol,
        manifests_dir: Path,
        owner_id: str | None = None,
        expected_codex_executable: Path | None = None,
    ) -> None:
        self.store = store
        self.observer = observer
        self.reconciler = reconciler
        self.cli_backend = cli_backend
        self.process_backend = process_backend
        self.manifests_dir = manifests_dir
        self.owner_id = owner_id or f"watchdog-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.expected_codex_executable = expected_codex_executable

    def recover(
        self,
        task_id: str,
        *,
        detector: str = "no-progress",
        last_positive_kind: str = "checkpoint",
        restart_requested: bool = False,
    ) -> RecoveryDecision:
        task = self.store.get_task(task_id)
        if task.state is TaskState.CIRCUIT_OPEN:
            return RecoveryDecision(task.state, "none", task.circuit_reason or "circuit-open", task_id)

        now = datetime.now(UTC)
        if not self.store.acquire_lease(task_id, self.owner_id, now, 120):
            return RecoveryDecision(task.state, "none", "recovery-lease-held", task_id)

        try:
            return self._recover_with_lease(
                task_id,
                detector=detector,
                last_positive_kind=last_positive_kind,
                restart_requested=restart_requested,
            )
        finally:
            self.store.release_lease(task_id, self.owner_id)

    def _recover_with_lease(
        self,
        task_id: str,
        *,
        detector: str,
        last_positive_kind: str,
        restart_requested: bool,
    ) -> RecoveryDecision:
        task = self.store.get_task(task_id)
        if task.state is TaskState.SUSPECT:
            task = self.store.transition(task_id, TaskState.RECONCILING, detector)
        elif task.state is not TaskState.RECONCILING:
            return RecoveryDecision(task.state, "none", "task-not-reconcilable", task_id)

        try:
            checkpoint = self.store.latest_checkpoint(task_id)
        except KeyError:
            pending = self.store.transition(
                task_id,
                TaskState.PENDING_CONFIRMATION,
                "no checkpoint is available",
            )
            return RecoveryDecision(pending.state, "none", "missing-checkpoint", task_id)

        batch = self.observer.observe(task.session_id, 0, limit=1000)
        terminal_observed = any(
            item.kind == "backend_terminal"
            and item.evidence_class is EvidenceClass.POSITIVE_TERMINAL
            for item in batch.observations
        )
        probe = self.reconciler.probe(
            checkpoint,
            terminal_observed=terminal_observed,
        )
        if probe.outcome == "completed":
            completed = self.store.transition(
                task_id,
                TaskState.COMPLETED,
                probe.reason,
            )
            return RecoveryDecision(completed.state, "none", probe.reason, task_id)

        if probe.outcome == "uncertain" or not may_auto_replay(
            checkpoint.effect, checkpoint.repeatable
        ):
            pending = self.store.transition(
                task_id,
                TaskState.PENDING_CONFIRMATION,
                probe.reason,
            )
            return RecoveryDecision(pending.state, "none", probe.reason, task_id)

        fingerprint = fault_fingerprint(
            detector=detector,
            session_id=task.session_id,
            step_id=checkpoint.step_id,
            effect=checkpoint.effect,
            last_positive_kind=last_positive_kind,
        )
        projected_same_count = (
            task.same_fingerprint_count + 1
            if task.last_fault_fingerprint == fingerprint
            else 1
        )
        capabilities = self.cli_backend.capabilities()
        reason = circuit_reason(
            CircuitSnapshot(
                recovery_count=task.recovery_count,
                restart_count=task.restart_count,
                same_fingerprint_count=projected_same_count,
                state_integrity=True,
                cli_compatible=capabilities.compatible,
                recovery_attempted=False,
                recovery_had_progress=True,
                restart_requested=restart_requested,
            )
        )
        if reason:
            opened = self.store.open_circuit(task_id, reason)
            return RecoveryDecision(opened.state, "none", reason, task_id)

        self.store.transition(
            task_id,
            TaskState.RECOVERY_READY,
            "safe read-only replay after reconciliation",
        )
        if restart_requested:
            expected_executable = (
                self.expected_codex_executable
                or self.process_backend.verified_executable()
            )
            if expected_executable is None:
                opened = self.store.open_circuit(task_id, "restart-unverified")
                return RecoveryDecision(
                    opened.state,
                    "none",
                    "restart-unverified",
                    task_id,
                )
            self._write_restart_manifest(task_id, task.session_id, fingerprint)
            self.store.increment_restart(task_id)
            self.store.transition(task_id, TaskState.RESTARTING, detector)
            restart = self.process_backend.restart_once(expected_executable)
            if not restart.success:
                opened = self.store.open_circuit(task_id, "restart-failed")
                return RecoveryDecision(opened.state, "none", "restart-failed", task_id)
            self.store.transition(
                task_id,
                TaskState.RECOVERY_READY,
                "Codex restarted once",
            )

        self.store.transition(task_id, TaskState.RESUMING, detector)
        self.store.increment_recovery(task_id, fingerprint)
        prompt = self._recovery_prompt(task_id, checkpoint.step_id)
        resumed = self.cli_backend.resume_read_only(task.session_id, prompt)
        if not resumed.success:
            opened = self.store.open_circuit(task_id, "resume-failed")
            return RecoveryDecision(opened.state, "none", "resume-failed", task_id)

        return RecoveryDecision(
            TaskState.RESUMING,
            "resumed",
            "read-only repeatable step resumed",
            task_id,
            evidence=(probe.reason, fingerprint),
        )

    def _write_restart_manifest(
        self,
        task_id: str,
        session_id: str,
        fingerprint: str,
    ) -> Path:
        self.manifests_dir.mkdir(parents=True, exist_ok=True)
        target = self.manifests_dir / f"restart-{task_id}-{uuid.uuid4().hex[:8]}.json"
        temporary = target.with_suffix(".tmp")
        payload = {
            "task_id": task_id,
            "session_id": session_id,
            "fault_fingerprint": fingerprint,
            "created_at": datetime.now(UTC).isoformat(),
            "action": "restart-once-then-read-only-resume",
        }
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(target)
        return target

    @staticmethod
    def _recovery_prompt(task_id: str, step_id: str) -> str:
        return (
            "Codex Resilience Watchdog recovery. "
            f"Task {task_id}, checkpoint {step_id}. "
            "First inspect actual outputs and current task state. "
            "Continue only the declared read-only repeatable operation. "
            "Do not write files, send messages, delete data, spend quota, "
            "or replay an operation whose outcome is uncertain. "
            "If any side effect is required, stop and report pending-confirmation."
        )
