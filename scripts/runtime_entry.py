from __future__ import annotations

from pathlib import Path
import sys


APP_DIR = Path(__file__).resolve().parent
SRC_DIR = APP_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from codex_resilience_watchdog.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
