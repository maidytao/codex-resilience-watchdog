from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SKILL_DIR = ROOT / "skills" / "codex-resilience-watchdog"


class SkillContractTest(unittest.TestCase):
    def test_required_skill_resources_exist(self) -> None:
        expected = (
            SKILL_DIR / "SKILL.md",
            SKILL_DIR / "agents" / "openai.yaml",
            SKILL_DIR / "scripts" / "watchdog.py",
            SKILL_DIR / "references" / "protocol.md",
        )

        self.assertEqual([path for path in expected if not path.is_file()], [])

    def test_skill_preserves_replay_and_loop_boundaries(self) -> None:
        text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")

        self.assertIn("--effect", text)
        self.assertIn("read-only", text)
        self.assertIn("pending-confirmation", text)
        self.assertIn("two automatic recoveries", text)
        self.assertIn("one Codex restart", text)
        self.assertIn("real evidence", text)

    def test_skill_is_discoverable_and_routes_to_protocol(self) -> None:
        text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        metadata = (SKILL_DIR / "agents" / "openai.yaml").read_text(
            encoding="utf-8"
        )

        self.assertIn("description: Use when", text)
        self.assertIn("references/protocol.md", text)
        self.assertIn("$codex-resilience-watchdog", metadata)

    def test_protocol_documents_bounded_commands_and_effects(self) -> None:
        text = (SKILL_DIR / "references" / "protocol.md").read_text(
            encoding="utf-8"
        )

        for command in ("arm", "heartbeat", "checkpoint", "complete"):
            self.assertIn(command, text)
        for effect in (
            "read-only",
            "write",
            "external-message",
            "delete",
            "paid",
            "unknown",
        ):
            self.assertIn(effect, text)

    def test_wrapper_has_no_hard_coded_profile_and_reports_missing_runtime(self) -> None:
        wrapper = SKILL_DIR / "scripts" / "watchdog.py"
        text = wrapper.read_text(encoding="utf-8")
        self.assertNotIn("Administrator", text)
        self.assertNotIn(".openclaw", text.lower())

        with tempfile.TemporaryDirectory() as temp_dir:
            result = subprocess.run(
                [sys.executable, str(wrapper), "status"],
                env={"CODEX_HOME": temp_dir},
                capture_output=True,
                text=True,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("watchdog runtime is not installed", result.stderr.lower())


if __name__ == "__main__":
    unittest.main()
