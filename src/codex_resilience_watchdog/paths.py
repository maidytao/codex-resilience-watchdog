from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True)
class WatchdogPaths:
    codex_home: Path
    skill_dir: Path
    data_dir: Path
    database: Path
    logs_dir: Path
    manifests_dir: Path
    backups_dir: Path
    app_dir: Path

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str],
        user_profile: Path,
    ) -> "WatchdogPaths":
        configured_home = environment.get("CODEX_HOME")
        codex_home = Path(configured_home) if configured_home else user_profile / ".codex"
        if not codex_home.is_absolute():
            raise ValueError("CODEX_HOME must be an absolute path")

        codex_home = codex_home.resolve()
        data_dir = codex_home / "watchdog"
        return cls(
            codex_home=codex_home,
            skill_dir=codex_home / "skills" / "codex-resilience-watchdog",
            data_dir=data_dir,
            database=data_dir / "resilience.db",
            logs_dir=data_dir / "logs",
            manifests_dir=data_dir / "recovery-manifests",
            backups_dir=data_dir / "backups",
            app_dir=data_dir / "app",
        )

    def assert_owned(self, candidate: Path) -> Path:
        resolved = candidate.resolve()
        roots = (self.skill_dir.resolve(), self.data_dir.resolve())
        if not any(resolved == root or resolved.is_relative_to(root) for root in roots):
            raise ValueError(f"path is outside watchdog-owned roots: {resolved}")
        return resolved
