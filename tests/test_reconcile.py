from __future__ import annotations

import hashlib

from codex_resilience_watchdog.models import EffectClass
from codex_resilience_watchdog.reconcile import ResultReconciler
from codex_resilience_watchdog.store import StateStore
from tests.helpers import TemporaryHomeTestCase


class ResultReconcilerTest(TemporaryHomeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = StateStore(self.codex_home / "watchdog" / "resilience.db")
        self.store.initialize()
        self.store.arm_task("task-1", "session-1", "ordinary", 300)
        self.reconciler = ResultReconciler()

    def checkpoint(
        self,
        *,
        effect: EffectClass = EffectClass.READ_ONLY,
        probe_kind: str | None = None,
        probe_target: str | None = None,
        expected_value: str | None = None,
    ):
        return self.store.record_checkpoint(
            task_id="task-1",
            step_id=f"step-{len(self.store.list_incidents(limit=50))}",
            effect=effect,
            repeatable=effect is EffectClass.READ_ONLY,
            input_digest=hashlib.sha256(str(probe_target).encode()).hexdigest(),
            probe_kind=probe_kind,
            probe_target=probe_target,
            expected_value=expected_value,
        )

    def test_file_sha256_proves_completed_result(self) -> None:
        output = self.root / "result.txt"
        output.write_text("verified", encoding="utf-8")
        expected = hashlib.sha256(output.read_bytes()).hexdigest()
        checkpoint = self.checkpoint(
            probe_kind="file-sha256",
            probe_target=str(output),
            expected_value=expected,
        )

        result = self.reconciler.probe(checkpoint)

        self.assertEqual(result.outcome, "completed")
        self.assertEqual(result.evidence, expected)

    def test_file_sha256_mismatch_is_not_completion(self) -> None:
        output = self.root / "result.txt"
        output.write_text("different", encoding="utf-8")
        checkpoint = self.checkpoint(
            probe_kind="file-sha256",
            probe_target=str(output),
            expected_value="0" * 64,
        )

        result = self.reconciler.probe(checkpoint)

        self.assertEqual(result.outcome, "missing")

    def test_uncertain_file_write_does_not_become_replayable(self) -> None:
        checkpoint = self.checkpoint(
            effect=EffectClass.WRITE,
            probe_kind="file-exists",
            probe_target=None,
        )

        result = self.reconciler.probe(checkpoint)

        self.assertEqual(result.outcome, "uncertain")

    def test_backend_terminal_requires_positive_terminal_observation(self) -> None:
        checkpoint = self.checkpoint(probe_kind="backend-terminal")

        self.assertEqual(
            self.reconciler.probe(checkpoint, terminal_observed=False).outcome,
            "uncertain",
        )
        self.assertEqual(
            self.reconciler.probe(checkpoint, terminal_observed=True).outcome,
            "completed",
        )

    def test_unknown_probe_is_uncertain_and_executes_nothing(self) -> None:
        checkpoint = self.checkpoint(
            probe_kind="powershell",
            probe_target="Remove-Item C:/important",
        )

        result = self.reconciler.probe(checkpoint)

        self.assertEqual(result.outcome, "uncertain")
        self.assertIn("unsupported", result.reason)


if __name__ == "__main__":
    import unittest

    unittest.main()
