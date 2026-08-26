from __future__ import annotations

from codex_resilience_watchdog.models import (
    EffectClass,
    EvidenceClass,
    Observation,
)
from codex_resilience_watchdog.policy import (
    CircuitSnapshot,
    circuit_reason,
    fault_fingerprint,
    is_real_progress,
    may_auto_replay,
)
from tests.helpers import TemporaryHomeTestCase


class ReplayPolicyTest(TemporaryHomeTestCase):
    def test_only_read_only_repeatable_is_replayable(self) -> None:
        for effect in EffectClass:
            expected = effect is EffectClass.READ_ONLY
            self.assertEqual(
                may_auto_replay(effect, repeatable=True),
                expected,
                effect.value,
            )
        self.assertFalse(may_auto_replay(EffectClass.READ_ONLY, repeatable=False))

    def test_timer_and_repeated_thinking_are_not_progress(self) -> None:
        self.assertFalse(is_real_progress(Observation(kind="timer_tick")))
        self.assertFalse(is_real_progress(Observation(kind="daemon_started")))
        self.assertFalse(is_real_progress(Observation(kind="thinking_unchanged")))

    def test_positive_stream_tool_and_terminal_events_are_progress(self) -> None:
        for kind in (
            "stream_event",
            "tool_completed",
            "model_request",
            "backend_terminal",
            "output_changed",
            "counter_advanced",
            "checkpoint",
        ):
            observation = Observation(
                kind=kind,
                evidence_class=EvidenceClass.POSITIVE_PROGRESS,
            )
            self.assertTrue(is_real_progress(observation), kind)

    def test_absence_only_event_never_becomes_progress_by_name(self) -> None:
        observation = Observation(
            kind="tool_completed",
            evidence_class=EvidenceClass.ABSENCE_ONLY,
        )
        self.assertFalse(is_real_progress(observation))


class CircuitPolicyTest(TemporaryHomeTestCase):
    def snapshot(self, **overrides: object) -> CircuitSnapshot:
        values: dict[str, object] = {
            "recovery_count": 0,
            "restart_count": 0,
            "same_fingerprint_count": 0,
            "state_integrity": True,
            "cli_compatible": True,
            "recovery_attempted": False,
            "recovery_had_progress": True,
            "restart_requested": False,
        }
        values.update(overrides)
        return CircuitSnapshot(**values)

    def test_each_hard_limit_opens_the_circuit(self) -> None:
        cases = (
            (self.snapshot(recovery_count=2), "recovery-limit"),
            (
                self.snapshot(restart_count=1, restart_requested=True),
                "restart-limit",
            ),
            (self.snapshot(same_fingerprint_count=2), "same-fault-limit"),
            (self.snapshot(state_integrity=False), "state-integrity"),
            (self.snapshot(cli_compatible=False), "cli-incompatible"),
            (
                self.snapshot(
                    recovery_attempted=True,
                    recovery_had_progress=False,
                ),
                "no-progress-after-recovery",
            ),
        )
        for snapshot, expected in cases:
            self.assertEqual(circuit_reason(snapshot), expected)

    def test_normal_first_recovery_does_not_open_circuit(self) -> None:
        self.assertIsNone(
            circuit_reason(
                self.snapshot(
                    recovery_count=1,
                    restart_count=0,
                    same_fingerprint_count=1,
                )
            )
        )

    def test_fault_fingerprint_is_stable_and_sensitive_to_step(self) -> None:
        first = fault_fingerprint(
            detector="tool-timeout",
            session_id="session-1",
            step_id="step-1",
            effect=EffectClass.READ_ONLY,
            last_positive_kind="tool_started",
        )
        repeated = fault_fingerprint(
            detector="tool-timeout",
            session_id="session-1",
            step_id="step-1",
            effect=EffectClass.READ_ONLY,
            last_positive_kind="tool_started",
        )
        other_step = fault_fingerprint(
            detector="tool-timeout",
            session_id="session-1",
            step_id="step-2",
            effect=EffectClass.READ_ONLY,
            last_positive_kind="tool_started",
        )

        self.assertEqual(first, repeated)
        self.assertNotEqual(first, other_step)
        self.assertEqual(len(first), 64)


if __name__ == "__main__":
    import unittest

    unittest.main()
