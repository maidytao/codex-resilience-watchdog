from __future__ import annotations

from contextlib import redirect_stdout, redirect_stderr
from dataclasses import dataclass
from io import StringIO
import json

from codex_resilience_watchdog.cli import main
from tests.helpers import TemporaryHomeTestCase


@dataclass(frozen=True)
class CliResult:
    exit_code: int
    stdout: str
    stderr: str

    @property
    def json(self):
        return json.loads(self.stdout)


class WatchdogCliTest(TemporaryHomeTestCase):
    def run_cli(self, arguments: list[str]) -> CliResult:
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = main(
                ["--home", str(self.codex_home), "--json", *arguments]
            )
        return CliResult(exit_code, stdout.getvalue(), stderr.getvalue())

    def test_status_json_is_bounded_and_structured(self) -> None:
        result = self.run_cli(["status"])

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.json["service"]["enabled"], False)
        self.assertEqual(result.json["tasks"], [])
        self.assertIn("version", result.json)

    def test_arm_heartbeat_checkpoint_and_complete_round_trip(self) -> None:
        armed = self.run_cli(
            [
                "arm",
                "--task",
                "task-1",
                "--session",
                "session-1",
                "--class",
                "ordinary",
                "--threshold",
                "300",
            ]
        )
        heartbeat = self.run_cli(
            ["heartbeat", "--task", "task-1", "--evidence", "output counter advanced"]
        )
        checkpoint = self.run_cli(
            [
                "checkpoint",
                "--task",
                "task-1",
                "--step",
                "inspect-1",
                "--effect",
                "read-only",
                "--repeatable",
                "--input-digest",
                "abc",
                "--probe",
                "file-exists",
                "--target",
                str(self.root / "missing.txt"),
            ]
        )
        completed = self.run_cli(["complete", "--task", "task-1"])

        self.assertEqual(armed.json["state"], "armed")
        self.assertEqual(heartbeat.json["state"], "running")
        self.assertEqual(checkpoint.json["effect"], "read-only")
        self.assertTrue(checkpoint.json["repeatable"])
        self.assertEqual(completed.json["state"], "completed")

    def test_enable_and_disable_persist_in_config(self) -> None:
        config = self.codex_home / "watchdog" / "config.json"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text('{"codex_executable": "C:/verified/Codex.exe"}', encoding="utf-8")

        self.assertTrue(self.run_cli(["enable"]).json["enabled"])
        self.assertTrue(self.run_cli(["status"]).json["service"]["enabled"])
        self.assertEqual(
            json.loads(config.read_text(encoding="utf-8"))["codex_executable"],
            "C:/verified/Codex.exe",
        )
        self.assertFalse(self.run_cli(["disable"]).json["enabled"])
        self.assertFalse(self.run_cli(["status"]).json["service"]["enabled"])

    def test_incidents_are_bounded(self) -> None:
        self.run_cli(
            [
                "arm",
                "--task",
                "task-1",
                "--session",
                "session-1",
                "--class",
                "ordinary",
                "--threshold",
                "300",
            ]
        )
        for _ in range(60):
            self.run_cli(
                ["heartbeat", "--task", "task-1", "--evidence", "progress"]
            )

        incidents = self.run_cli(["incidents", "--limit", "500"])

        self.assertLessEqual(len(incidents.json["incidents"]), 50)

    def test_reset_circuit_requires_explicit_task(self) -> None:
        result = self.run_cli(["reset-circuit", "--task", "missing"])

        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(result.json["error"], "task-not-found")


if __name__ == "__main__":
    import unittest

    unittest.main()
