from __future__ import annotations

from pathlib import Path

from tests.helpers import TemporaryHomeTestCase

from codex_resilience_watchdog.models import EffectClass, TaskState
from codex_resilience_watchdog.paths import WatchdogPaths


class WatchdogPathsTest(TemporaryHomeTestCase):
    def test_codex_home_override_controls_all_owned_paths(self) -> None:
        paths = WatchdogPaths.from_environment(
            {"CODEX_HOME": str(self.codex_home)}, self.profile
        )

        self.assertEqual(
            paths.skill_dir,
            self.codex_home / "skills" / "codex-resilience-watchdog",
        )
        self.assertEqual(paths.data_dir, self.codex_home / "watchdog")
        self.assertEqual(paths.database, paths.data_dir / "resilience.db")

    def test_user_profile_is_used_when_codex_home_is_missing(self) -> None:
        paths = WatchdogPaths.from_environment({}, self.profile)

        self.assertEqual(paths.codex_home, self.profile / ".codex")

    def test_assert_owned_accepts_skill_and_watchdog_descendants(self) -> None:
        paths = WatchdogPaths.from_environment(
            {"CODEX_HOME": str(self.codex_home)}, self.profile
        )

        self.assertEqual(
            paths.assert_owned(paths.skill_dir / "SKILL.md"),
            (paths.skill_dir / "SKILL.md").resolve(),
        )
        self.assertEqual(
            paths.assert_owned(paths.data_dir / "logs" / "watchdog.log"),
            (paths.data_dir / "logs" / "watchdog.log").resolve(),
        )

    def test_assert_owned_rejects_openclaw_and_codex_parent(self) -> None:
        paths = WatchdogPaths.from_environment(
            {"CODEX_HOME": str(self.codex_home)}, self.profile
        )

        with self.assertRaises(ValueError):
            paths.assert_owned(self.profile / ".openclaw" / "workspace")
        with self.assertRaises(ValueError):
            paths.assert_owned(paths.codex_home / "config.toml")

    def test_relative_codex_home_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            WatchdogPaths.from_environment(
                {"CODEX_HOME": str(Path("relative-home"))}, self.profile
            )


class DomainEnumTest(TemporaryHomeTestCase):
    def test_effect_values_match_the_checkpoint_protocol(self) -> None:
        self.assertEqual(EffectClass.READ_ONLY.value, "read-only")
        self.assertEqual(EffectClass.EXTERNAL_MESSAGE.value, "external-message")
        self.assertEqual(EffectClass.PAID.value, "paid")

    def test_circuit_open_is_a_persistable_task_state(self) -> None:
        self.assertEqual(TaskState.CIRCUIT_OPEN.value, "circuit-open")


if __name__ == "__main__":
    unittest.main()
