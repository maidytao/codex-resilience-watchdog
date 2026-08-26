from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time

from tests.helpers import TemporaryHomeTestCase


ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install.ps1"
UNINSTALLER = ROOT / "scripts" / "uninstall.ps1"


class InstallerTest(TemporaryHomeTestCase):
    def setUp(self) -> None:
        super().setUp()
        command_dir = self.root / "commands"
        command_dir.mkdir()
        codex_command = command_dir / "codex.cmd"
        codex_command.write_text(
            "@echo off\r\n"
            "if \"%1\"==\"--version\" (echo codex-cli 0.150.0-test& exit /b 0)\r\n"
            "if \"%1\"==\"exec\" if \"%2\"==\"resume\" if \"%3\"==\"--help\" "
            "(echo Usage: codex exec resume [SESSION_ID] [PROMPT]& exit /b 0)\r\n"
            "exit /b 0\r\n",
            encoding="ascii",
        )
        self.script_environment = dict(os.environ)
        self.script_environment["PATH"] = (
            str(command_dir) + os.pathsep + self.script_environment.get("PATH", "")
        )

    def run_script(self, script: Path, *arguments: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
                "-CodexHome",
                str(self.codex_home),
                *arguments,
            ],
            cwd=ROOT,
            env=self.script_environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )

    def install(self) -> subprocess.CompletedProcess:
        result = self.run_script(INSTALLER)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def test_scripts_never_mention_fixed_profile_or_unrelated_runtime(self) -> None:
        for script in (INSTALLER, UNINSTALLER):
            text = script.read_text(encoding="utf-8")
            self.assertNotIn("Administrator", text)
            self.assertNotIn(".openclaw", text.lower())
        installer = INSTALLER.read_text(encoding="utf-8")
        self.assertNotIn("[string]$Home", installer)

    def test_dry_run_does_not_mutate_codex_home(self) -> None:
        before = sorted(path.relative_to(self.codex_home) for path in self.codex_home.rglob("*"))

        result = self.run_script(INSTALLER, "-DryRun")

        self.assertEqual(result.returncode, 0, result.stderr)
        after = sorted(path.relative_to(self.codex_home) for path in self.codex_home.rglob("*"))
        self.assertEqual(after, before)

    def test_install_copies_runtime_skill_and_hash_manifest(self) -> None:
        result = self.install()
        manifest_path = self.codex_home / "watchdog" / "install-manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))

        self.assertTrue((self.codex_home / "watchdog" / "app" / "runtime_entry.py").is_file())
        self.assertTrue((self.codex_home / "watchdog" / "app" / "src" / "codex_resilience_watchdog" / "cli.py").is_file())
        self.assertTrue((self.codex_home / "skills" / "codex-resilience-watchdog" / "SKILL.md").is_file())
        self.assertEqual(manifest["startup"]["method"], "none")
        self.assertGreater(len(manifest["files"]), 10)
        self.assertIn("installed", result.stdout.lower())

        status = subprocess.run(
            [
                sys.executable,
                str(self.codex_home / "watchdog" / "app" / "runtime_entry.py"),
                "--home",
                str(self.codex_home),
                "--json",
                "status",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn("service", json.loads(status.stdout))

    def test_default_uninstall_preserves_database_and_audit_data(self) -> None:
        self.install()
        data_dir = self.codex_home / "watchdog"
        database = data_dir / "resilience.db"
        logs = data_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        audit = logs / "audit.log"
        audit.write_text("retained\n", encoding="utf-8")

        result = self.run_script(UNINSTALLER)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(database.is_file())
        self.assertEqual(audit.read_text(encoding="utf-8"), "retained\n")
        self.assertFalse((data_dir / "app").exists())
        self.assertFalse((self.codex_home / "skills" / "codex-resilience-watchdog").exists())

    def test_purge_removes_only_watchdog_owned_data(self) -> None:
        self.install()
        sentinel = self.codex_home / "keep-me.txt"
        sentinel.write_text("safe", encoding="utf-8")

        result = self.run_script(UNINSTALLER, "-PurgeData")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.codex_home / "watchdog").exists())
        self.assertFalse((self.codex_home / "skills" / "codex-resilience-watchdog").exists())
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "safe")

    def test_installed_daemon_is_single_instance_and_stops_when_disabled(self) -> None:
        self.install()
        runtime = self.codex_home / "watchdog" / "app" / "runtime_entry.py"

        def run_runtime(*arguments: str) -> subprocess.CompletedProcess:
            return subprocess.run(
                [
                    sys.executable,
                    str(runtime),
                    "--home",
                    str(self.codex_home),
                    "--json",
                    *arguments,
                ],
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertEqual(run_runtime("enable").returncode, 0)
        daemon = subprocess.Popen(
            [
                sys.executable,
                str(runtime),
                "--home",
                str(self.codex_home),
                "daemon",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        lock = self.codex_home / "watchdog" / "daemon.lock"
        for _ in range(50):
            if lock.exists():
                break
            time.sleep(0.1)
        self.assertTrue(lock.exists())

        duplicate = run_runtime("daemon")
        self.assertEqual(duplicate.returncode, 0, duplicate.stderr)
        self.assertEqual(json.loads(duplicate.stdout)["reason"], "already-running")

        task = "synthetic-e2e"
        self.assertEqual(
            run_runtime(
                "arm",
                "--task",
                task,
                "--session",
                "synthetic-session",
                "--class",
                "ordinary",
                "--threshold",
                "300",
            ).returncode,
            0,
        )
        self.assertEqual(
            run_runtime(
                "heartbeat",
                "--task",
                task,
                "--evidence",
                "synthetic output counter advanced",
            ).returncode,
            0,
        )
        self.assertEqual(
            run_runtime(
                "checkpoint",
                "--task",
                task,
                "--step",
                "inspect",
                "--effect",
                "read-only",
                "--repeatable",
                "--input-digest",
                "synthetic",
                "--probe",
                "file-exists",
                "--target",
                str(self.root / "missing.result"),
            ).returncode,
            0,
        )
        completed = run_runtime("complete", "--task", task)
        self.assertEqual(json.loads(completed.stdout)["state"], "completed")

        self.assertEqual(run_runtime("disable").returncode, 0)
        daemon.wait(timeout=5)
        self.assertFalse(lock.exists())


if __name__ == "__main__":
    import unittest

    unittest.main()
