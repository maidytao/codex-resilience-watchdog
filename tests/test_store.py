from __future__ import annotations

from datetime import UTC, datetime, timedelta

from codex_resilience_watchdog.models import EffectClass, TaskState
from codex_resilience_watchdog.store import InvalidTransition, StateStore
from tests.helpers import TemporaryHomeTestCase


class StateStoreTest(TemporaryHomeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.database = self.codex_home / "watchdog" / "resilience.db"
        self.store = StateStore(self.database)
        self.store.initialize()

    def arm(self, task_id: str = "task-1") -> None:
        self.store.arm_task(
            task_id=task_id,
            session_id="session-1",
            task_class="ordinary",
            threshold_seconds=300,
        )

    def test_arm_task_persists_minimal_metadata(self) -> None:
        self.arm()

        task = self.store.get_task("task-1")

        self.assertEqual(task.session_id, "session-1")
        self.assertEqual(task.state, TaskState.ARMED)
        self.assertEqual(task.generation, 0)
        self.assertEqual(task.recovery_count, 0)
        self.assertEqual(task.restart_count, 0)

    def test_duplicate_task_id_is_rejected(self) -> None:
        self.arm()

        with self.assertRaises(ValueError):
            self.arm()

    def test_generation_and_circuit_survive_reopen(self) -> None:
        self.arm()
        self.store.increment_recovery("task-1", "fingerprint-a")
        self.store.open_circuit("task-1", "recovery_limit")

        reopened = StateStore(self.database)
        reopened.initialize()
        task = reopened.get_task("task-1")

        self.assertEqual(task.state, TaskState.CIRCUIT_OPEN)
        self.assertEqual(task.generation, 1)
        self.assertEqual(task.recovery_count, 1)
        self.assertEqual(task.circuit_reason, "recovery_limit")

    def test_completed_task_cannot_return_to_running(self) -> None:
        self.arm()
        self.store.transition("task-1", TaskState.RUNNING, "initial_progress")
        self.store.transition("task-1", TaskState.COMPLETED, "positive_terminal")

        with self.assertRaises(InvalidTransition):
            self.store.transition("task-1", TaskState.RUNNING, "late_event")

    def test_reset_circuit_requires_open_circuit_and_is_audited(self) -> None:
        self.arm()
        with self.assertRaises(InvalidTransition):
            self.store.reset_circuit("task-1", "operator")

        self.store.open_circuit("task-1", "same_fault")
        self.store.reset_circuit("task-1", "operator")

        self.assertEqual(
            self.store.get_task("task-1").state,
            TaskState.PENDING_CONFIRMATION,
        )
        incidents = self.store.list_incidents("task-1", limit=10)
        self.assertEqual(incidents[0].kind, "circuit_reset")

    def test_only_one_unexpired_recovery_lease_is_granted(self) -> None:
        self.arm()
        now = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)

        self.assertTrue(self.store.acquire_lease("task-1", "owner-a", now, 60))
        self.assertFalse(self.store.acquire_lease("task-1", "owner-b", now, 60))
        self.assertTrue(
            self.store.acquire_lease(
                "task-1", "owner-b", now + timedelta(seconds=61), 60
            )
        )

    def test_checkpoint_round_trip_preserves_effect_and_probe(self) -> None:
        self.arm()
        self.store.record_checkpoint(
            task_id="task-1",
            step_id="inspect-1",
            effect=EffectClass.READ_ONLY,
            repeatable=True,
            input_digest="abc123",
            probe_kind="file-sha256",
            probe_target="C:/tmp/result.txt",
            expected_value="deadbeef",
        )

        checkpoint = self.store.latest_checkpoint("task-1")

        self.assertEqual(checkpoint.effect, EffectClass.READ_ONLY)
        self.assertTrue(checkpoint.repeatable)
        self.assertEqual(checkpoint.idempotency_key.count(":"), 3)
        self.assertEqual(checkpoint.probe_kind, "file-sha256")

    def test_bounded_queries_cap_results_at_fifty(self) -> None:
        for index in range(55):
            task_id = f"task-{index:02d}"
            self.arm(task_id)
            self.store.record_incident(task_id, "synthetic", "test")

        self.assertEqual(len(self.store.list_tasks(limit=500)), 50)
        self.assertEqual(len(self.store.list_incidents(limit=500)), 50)


if __name__ == "__main__":
    import unittest

    unittest.main()
