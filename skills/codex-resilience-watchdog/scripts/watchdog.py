from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


def codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path.home() / ".codex").resolve()


def main(arguments: list[str] | None = None) -> int:
    home = codex_home()
    runtime = home / "watchdog" / "app" / "runtime_entry.py"
    if not runtime.is_file():
        print(
            f"Watchdog runtime is not installed at {runtime}",
            file=sys.stderr,
        )
        return 2

    command = [
        sys.executable,
        str(runtime),
        "--home",
        str(home),
        *(arguments if arguments is not None else sys.argv[1:]),
    ]
    return subprocess.run(command, shell=False, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())

