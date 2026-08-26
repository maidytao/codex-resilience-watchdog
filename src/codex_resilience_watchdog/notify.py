from __future__ import annotations

from datetime import UTC, datetime
import json
from pathlib import Path
import subprocess
from typing import Any, Callable


class AuditLogger:
    def __init__(
        self,
        path: Path,
        *,
        max_bytes: int = 5 * 1024 * 1024,
        backup_count: int = 3,
    ) -> None:
        self.path = path
        self.max_bytes = max_bytes
        self.backup_count = backup_count

    def write(self, kind: str, reason: str, **fields: Any) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "kind": kind,
            "reason": reason,
            **fields,
        }
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        encoded_size = len(line.encode("utf-8"))
        if self.path.exists() and self.path.stat().st_size + encoded_size > self.max_bytes:
            self._rotate()
        with self.path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(line)

    def _rotate(self) -> None:
        oldest = self.path.with_name(f"{self.path.name}.{self.backup_count}")
        if oldest.exists():
            oldest.unlink()
        for index in range(self.backup_count - 1, 0, -1):
            source = self.path.with_name(f"{self.path.name}.{index}")
            target = self.path.with_name(f"{self.path.name}.{index + 1}")
            if source.exists():
                source.replace(target)
        if self.path.exists():
            self.path.replace(self.path.with_name(f"{self.path.name}.1"))


class WindowsNotifier:
    def __init__(self, launcher: Callable[..., Any] = subprocess.Popen) -> None:
        self.launcher = launcher

    def notify(self, title: str, message: str) -> bool:
        script = (
            "Add-Type -AssemblyName PresentationFramework;"
            "[System.Windows.MessageBox]::Show($args[1],$args[0]) | Out-Null"
        )
        try:
            self.launcher(
                ["powershell", "-NoProfile", "-Command", script, title, message],
                shell=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return True
        except OSError:
            return False
