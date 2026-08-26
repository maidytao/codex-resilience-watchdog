from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import threading

from codex_resilience_watchdog.models import (
    EvidenceClass,
    Observation,
    RecoveryDecision,
    TaskState,
)
from codex_resilience_watchdog.notify import AuditLogger, WindowsNotifier
from codex_resilience_watchdog.observer import ObservationBatch
from codex_resilience_watchdog.service import DaemonLock, WatchdogService
from codex_resilience_watchdog.store import StateStore
from tests.helpers import TemporaryHomeTestCase


class FakeObserver:
    def __init__(self, observations: tuple[Observation, ...] = ()) -> None:
        self.observations = observations

    def observe(self, session_id: str, since_rowid: int, limit: int = 1000):
        if since_rowid >= len(self.observations):
            return ObservationBatch((), since_rowid, 0, "ok", True)
        return ObservationBatch(
            self.observations,
            len(self.observations),
            len(self.observations),
            "ok",
            True,
        )


class FakeRecoveryController:
    def __init__(self, decision: RecoveryDecision | None = None) -> None:
        self.calls: list[str] = []
        self.call_kwargs: list[dict[str, object]] = []
        self.decision = decision or RecoveryDecision(
            TaskState.RESUMING, "resumed", "test", "task-1"
        )

    def recover(self, task_id: str, **kwargs) -> RecoveryDecision:
        self.calls.append(task_id)
        self.call_kwargs.append(dict(kwargs))
        return RecoveryDecision(
            self.decision.state,
            self.decision.action,
            self.decision.reason,
            task_id,
        )


class FakeNotifier:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def notify(self, title: str, message: str) -> bool:
        self.calls.append((title, message))
        return True


class WatchdogServiceTest(TemporaryHomeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = StateStore(self.codex_home / "watchdog" / "resilience.db")
        self.store.initialize()

    def arm_running_task(self, task_id: str = "task-1") -> None:
        self.store.arm_task(task_id, "session-1", "ordinary", 300)
        self.store.transition(task_id, TaskState.RUNNING, "initial progress")

    def test_stale_running_task_triggers_one_recovery_review(self) -> None:
        self.arm_running_task()
        controller = FakeRecoveryController()
        service = WatchdogService(
            store=self.store,
            observer=FakeObserver(),
            recovery_controller=controller,
        )
        now = datetime.now(UTC) + timedelta(seconds=301)

        report = service.poll_once(now)

        self.assertEqual(controller.calls, ["task-1"])
        self.assertEqual(report.recovery_attempts, 1)

    def test_verified_unresponsive_process_requests_one_bounded_restart(self) -> None:
        self.arm_running_task()
        controller = FakeRecoveryController()
        service = WatchdogService(
            store=self.store,
            observer=FakeObserver(),
            recovery_controller=controller,
            restart_probe=lambda: True,
        )

        service.poll_once(datetime.now(UTC) + timedelta(seconds=301))

        self.assertEqual(controller.call_kwargs, [{"restart_requested": True}])

    def test_positive_progress_returns_suspect_task_to_running(self) -> None:
        self.arm_running_task()
        self.store.transition("task-1", TaskState.SUSPECT, "threshold expired")
        controller = FakeRecoveryController()
        service = WatchdogService(
            store=self.store,
            observer=FakeObserver(
                (
                    Observation(
                        kind="stream_event",
                        evidence_class=EvidenceClass.POSITIVE_PROGRESS,
                    ),
                )
            ),
            recovery_controller=controller,
        )

        report = service.poll_once(datetime.now(UTC))

        self.assertEqual(self.store.get_task("task-1").state, TaskState.RUNNING)
        self.assertEqual(report.progress_events, 1)
        self.assertEqual(controller.calls, [])

    def test_log_cursor_prevents_recounting_the_same_progress(self) -> None:
        self.arm_running_task()
        service = WatchdogService(
            store=self.store,
            observer=FakeObserver(
                (
                    Observation(
                        kind="stream_event",
                        evidence_class=EvidenceClass.POSITIVE_PROGRESS,
                    ),
                )
            ),
            recovery_controller=FakeRecoveryController(),
        )

        first = service.poll_once(datetime.now(UTC))
        second = service.poll_once(datetime.now(UTC))

        self.assertEqual(first.progress_events, 1)
        self.assertEqual(second.progress_events, 0)

    def test_daemon_restart_does_not_clear_open_circuit(self) -> None:
        self.arm_running_task()
        self.store.open_circuit("task-1", "same-fault-limit")

        reopened_store = StateStore(self.store.database)
        reopened_store.initialize()
        service = WatchdogService(
            store=reopened_store,
            observer=FakeObserver(),
            recovery_controller=FakeRecoveryController(),
        )
        service.poll_once(datetime.now(UTC))

        self.assertEqual(
            reopened_store.get_task("task-1").state,
            TaskState.CIRCUIT_OPEN,
        )

    def test_circuit_decision_emits_one_local_notification(self) -> None:
        self.arm_running_task()
        controller = FakeRecoveryController(
            RecoveryDecision(
                TaskState.CIRCUIT_OPEN,
                "none",
                "same-fault-limit",
                "task-1",
            )
        )
        notifier = FakeNotifier()
        service = WatchdogService(
            store=self.store,
            observer=FakeObserver(),
            recovery_controller=controller,
            notifier=notifier,
        )

        service.poll_once(datetime.now(UTC) + timedelta(seconds=301))

        self.assertEqual(len(notifier.calls), 1)
        self.assertIn("task-1", notifier.calls[0][1])
        self.assertIn("same-fault-limit", notifier.calls[0][1])

    def test_recovery_decision_is_audited_without_prompt_content(self) -> None:
        self.arm_running_task()
        audit_path = self.codex_home / "watchdog" / "logs" / "watchdog.log"
        service = WatchdogService(
            store=self.store,
            observer=FakeObserver(),
            recovery_controller=FakeRecoveryController(),
            audit_logger=AuditLogger(audit_path),
        )

        service.poll_once(datetime.now(UTC) + timedelta(seconds=301))

        record = json.loads(audit_path.read_text(encoding="utf-8"))
        self.assertEqual(record["kind"], "recovery-decision")
        self.assertEqual(record["task_id"], "task-1")
        self.assertNotIn("prompt", record)
        self.assertNotIn("evidence", record)

    def test_windows_notification_launch_is_non_blocking(self) -> None:
        calls: list[tuple[list[str], dict[str, object]]] = []

        def launcher(arguments, **kwargs):
            calls.append((list(arguments), dict(kwargs)))
            return object()

        notifier = WindowsNotifier(launcher=launcher)

        self.assertTrue(notifier.notify("title", "message"))
        self.assertFalse(calls[0][1]["shell"])
        self.assertNotIn("timeout", calls[0][1])

    def test_audit_log_rotates_with_bounded_backups(self) -> None:
        log = self.codex_home / "watchdog" / "logs" / "watchdog.log"
        logger = AuditLogger(log, max_bytes=256, backup_count=3)

        for index in range(100):
            logger.write("test", f"entry-{index}-" + ("x" * 80))

        self.assertTrue(log.exists())
        self.assertLessEqual(len(list(log.parent.glob("watchdog.log.*"))), 3)

    def test_run_stops_after_service_is_disabled(self) -> None:
        service = WatchdogService(
            store=self.store,
            observer=FakeObserver(),
            recovery_controller=FakeRecoveryController(),
        )
        checks = iter((True, False))

        result = service.run(
            threading.Event(),
            poll_seconds=60.0,
            enabled_check_seconds=0.0,
            enabled_check=lambda: next(checks, False),
        )

        self.assertEqual(result, 0)

    def test_daemon_lock_rejects_duplicate_and_reclaims_stale_pid(self) -> None:
        path = self.codex_home / "watchdog" / "daemon.lock"
        first = DaemonLock(path)
        second = DaemonLock(path)

        self.assertTrue(first.acquire())
        self.assertFalse(second.acquire())
        first.release()
        path.write_text(json.dumps({"pid": 99999999, "token": "stale"}), encoding="utf-8")
        self.assertTrue(second.acquire())
        second.release()


if __name__ == "__main__":
    import unittest

    unittest.main()
