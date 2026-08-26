from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from codex_resilience_watchdog.backends import (
    BackendResult,
    CapabilityReport,
    CodexCliBackend,
    CodexProcessBackend,
)
from codex_resilience_watchdog.models import (
    EffectClass,
    EvidenceClass,
    Observation,
    TaskState,
)
from codex_resilience_watchdog.observer import ObservationBatch
from codex_resilience_watchdog.reconcile import ResultReconciler
from codex_resilience_watchdog.recovery import RecoveryController
from codex_resilience_watchdog.store import StateStore
from tests.helpers import TemporaryHomeTestCase


class FakeObserver:
    def __init__(self, observations: tuple[Observation, ...] = ()) -> None:
        self.observations = observations

    def observe(self, session_id: str, since_rowid: int, limit: int = 1000):
        return ObservationBatch(
            observations=self.observations,
            last_rowid=len(self.observations),
            rows_scanned=len(self.observations),
            database_status="ok",
            read_only=True,
        )


class FakeCliBackend:
    def __init__(self, *, compatible: bool = True, success: bool = True) -> None:
        self.compatible = compatible
        self.success = success
        self.resume_calls = 0
        self.session_ids: list[str] = []

    def capabilities(self) -> CapabilityReport:
        return CapabilityReport(
            compatible=self.compatible,
            version="test",
            reason="ok" if self.compatible else "missing resume",
        )

    def resume_read_only(self, session_id: str, prompt: str) -> BackendResult:
        self.resume_calls += 1
        self.session_ids.append(session_id)
        return BackendResult(
            success=self.success,
            returncode=0 if self.success else 1,
            reason="resumed" if self.success else "resume failed",
        )


class FakeProcessBackend:
    def __init__(self, *, success: bool = True) -> None:
        self.success = success
        self.restart_calls = 0
        self.verified_calls = 0
        self.expected_paths: list[Path | None] = []

    def verified_executable(self) -> Path | None:
        self.verified_calls += 1
        return Path("C:/Program Files/Codex/Codex.exe")

    def restart_once(self, expected_executable: Path | None) -> BackendResult:
        self.restart_calls += 1
        self.expected_paths.append(expected_executable)
        return BackendResult(
            success=self.success,
            returncode=0 if self.success else 1,
            reason="restarted" if self.success else "restart failed",
        )


class RecoveryControllerTest(TemporaryHomeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.store = StateStore(self.codex_home / "watchdog" / "resilience.db")
        self.store.initialize()
        self.cli = FakeCliBackend()
        self.process = FakeProcessBackend()

    def prepare_task(
        self,
        task_id: str,
        *,
        effect: EffectClass,
        repeatable: bool,
        probe_kind: str | None,
        probe_target: str | None = None,
    ) -> None:
        self.store.arm_task(task_id, f"session-{task_id}", "ordinary", 300)
        self.store.transition(task_id, TaskState.RUNNING, "initial progress")
        self.store.transition(task_id, TaskState.SUSPECT, "threshold expired")
        self.store.record_checkpoint(
            task_id=task_id,
            step_id="step-1",
            effect=effect,
            repeatable=repeatable,
            input_digest="input-digest",
            probe_kind=probe_kind,
            probe_target=probe_target,
        )

    def controller(self, observer: FakeObserver | None = None) -> RecoveryController:
        return RecoveryController(
            store=self.store,
            observer=observer or FakeObserver(),
            reconciler=ResultReconciler(),
            cli_backend=self.cli,
            process_backend=self.process,
            manifests_dir=self.codex_home / "watchdog" / "recovery-manifests",
            owner_id="test-controller",
        )

    def test_read_only_repeatable_missing_result_can_resume(self) -> None:
        missing = self.root / "missing.txt"
        self.prepare_task(
            "task-read",
            effect=EffectClass.READ_ONLY,
            repeatable=True,
            probe_kind="file-exists",
            probe_target=str(missing),
        )

        decision = self.controller().recover("task-read")

        self.assertEqual(self.cli.resume_calls, 1)
        self.assertEqual(self.cli.session_ids, ["session-task-read"])
        self.assertEqual(decision.action, "resumed")
        self.assertEqual(decision.state, TaskState.RESUMING)
        self.assertEqual(self.store.get_task("task-read").recovery_count, 1)

    def test_side_effect_checkpoint_never_resumes(self) -> None:
        self.prepare_task(
            "task-write",
            effect=EffectClass.WRITE,
            repeatable=False,
            probe_kind="file-exists",
            probe_target=str(self.root / "missing.txt"),
        )

        decision = self.controller().recover("task-write")

        self.assertEqual(self.cli.resume_calls, 0)
        self.assertEqual(decision.state, TaskState.PENDING_CONFIRMATION)

    def test_uncertain_read_only_result_does_not_resume(self) -> None:
        self.prepare_task(
            "task-uncertain",
            effect=EffectClass.READ_ONLY,
            repeatable=True,
            probe_kind=None,
        )

        decision = self.controller().recover("task-uncertain")

        self.assertEqual(self.cli.resume_calls, 0)
        self.assertEqual(decision.state, TaskState.PENDING_CONFIRMATION)

    def test_positive_backend_terminal_completes_without_resume(self) -> None:
        self.prepare_task(
            "task-complete",
            effect=EffectClass.READ_ONLY,
            repeatable=True,
            probe_kind="backend-terminal",
        )
        observer = FakeObserver(
            (
                Observation(
                    kind="backend_terminal",
                    evidence_class=EvidenceClass.POSITIVE_TERMINAL,
                ),
            )
        )

        decision = self.controller(observer).recover("task-complete")

        self.assertEqual(decision.state, TaskState.COMPLETED)
        self.assertEqual(self.cli.resume_calls, 0)

    def test_second_occurrence_of_same_fault_opens_circuit(self) -> None:
        self.prepare_task(
            "task-loop",
            effect=EffectClass.READ_ONLY,
            repeatable=True,
            probe_kind="file-exists",
            probe_target=str(self.root / "missing.txt"),
        )
        controller = self.controller()
        first = controller.recover("task-loop", detector="tool-timeout")
        self.assertEqual(first.state, TaskState.RESUMING)
        self.store.transition("task-loop", TaskState.SUSPECT, "stalled again")

        second = controller.recover("task-loop", detector="tool-timeout")

        self.assertEqual(self.cli.resume_calls, 1)
        self.assertEqual(second.state, TaskState.CIRCUIT_OPEN)
        self.assertEqual(second.reason, "same-fault-limit")

    def test_second_restart_request_opens_circuit(self) -> None:
        self.prepare_task(
            "task-hung",
            effect=EffectClass.READ_ONLY,
            repeatable=True,
            probe_kind="file-exists",
            probe_target=str(self.root / "missing.txt"),
        )
        controller = self.controller()
        first = controller.recover(
            "task-hung",
            detector="process-hung-1",
            restart_requested=True,
        )
        self.assertEqual(first.state, TaskState.RESUMING)
        self.assertEqual(self.process.restart_calls, 1)
        self.store.transition("task-hung", TaskState.SUSPECT, "stalled after restart")

        second = controller.recover(
            "task-hung",
            detector="process-hung-2",
            restart_requested=True,
        )

        self.assertEqual(self.process.restart_calls, 1)
        self.assertEqual(second.state, TaskState.CIRCUIT_OPEN)
        self.assertEqual(second.reason, "restart-limit")

    def test_restart_manifest_is_written_before_backend_call(self) -> None:
        self.prepare_task(
            "task-manifest",
            effect=EffectClass.READ_ONLY,
            repeatable=True,
            probe_kind="file-exists",
            probe_target=str(self.root / "missing.txt"),
        )

        self.controller().recover(
            "task-manifest",
            detector="process-hung",
            restart_requested=True,
        )

        manifests = list(
            (self.codex_home / "watchdog" / "recovery-manifests").glob("*.json")
        )
        self.assertEqual(len(manifests), 1)
        self.assertIn("task-manifest", manifests[0].read_text(encoding="utf-8"))
        self.assertEqual(self.process.verified_calls, 1)
        self.assertEqual(
            self.process.expected_paths,
            [Path("C:/Program Files/Codex/Codex.exe")],
        )


class CodexCliBackendTest(TemporaryHomeTestCase):
    def test_resume_uses_fixed_read_only_arguments_without_bypass(self) -> None:
        captured: list[tuple[list[str], bool]] = []

        @dataclass
        class Completed:
            returncode: int = 0
            stdout: str = "{}"
            stderr: str = ""

        def runner(args, *, shell, **kwargs):
            captured.append((list(args), shell))
            return Completed()

        backend = CodexCliBackend("C:/Codex/codex.exe", runner=runner)

        result = backend.resume_read_only("session-1", "continue safely")

        self.assertTrue(result.success)
        args, shell = captured[0]
        self.assertFalse(shell)
        self.assertEqual(args[:5], ["C:/Codex/codex.exe", "-s", "read-only", "-a", "never"])
        self.assertIn("resume", args)
        self.assertIn("session-1", args)
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", args)


class CodexProcessBackendTest(TemporaryHomeTestCase):
    def test_process_path_must_be_uniquely_verified_before_restart(self) -> None:
        calls: list[list[str]] = []

        @dataclass
        class Completed:
            returncode: int = 0
            stdout: str = "C:/Program Files/Codex/Codex.exe\n"
            stderr: str = ""

        def runner(args, **kwargs):
            calls.append(list(args))
            return Completed()

        backend = CodexProcessBackend(runner=runner)

        self.assertEqual(
            backend.verified_executable(),
            Path("C:/Program Files/Codex/Codex.exe"),
        )
        self.assertIn("powershell", calls[0][0].lower())

    def test_unresponsive_probe_fails_closed_without_verified_window(self) -> None:
        @dataclass
        class Completed:
            returncode: int = 4
            stdout: str = ""
            stderr: str = ""

        backend = CodexProcessBackend(runner=lambda *args, **kwargs: Completed())

        self.assertFalse(backend.is_unresponsive(None))


if __name__ == "__main__":
    import unittest

    unittest.main()
