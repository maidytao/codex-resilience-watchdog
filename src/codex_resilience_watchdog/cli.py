from __future__ import annotations

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
import threading
from typing import Any, Sequence

from . import __version__
from .backends import CodexCliBackend, CodexProcessBackend
from .models import EffectClass, TaskState
from .notify import AuditLogger, WindowsNotifier
from .observer import CodexLogObserver
from .paths import WatchdogPaths
from .reconcile import ResultReconciler
from .recovery import RecoveryController
from .service import DaemonLock, WatchdogService, load_enabled, set_enabled
from .store import InvalidTransition, StateStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex-resilience-watchdog")
    parser.add_argument("--home", help="Override CODEX_HOME")
    parser.add_argument("--json", action="store_true", dest="as_json")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("status")

    arm = commands.add_parser("arm")
    arm.add_argument("--task", required=True)
    arm.add_argument("--session", required=True)
    arm.add_argument("--class", required=True, dest="task_class")
    arm.add_argument("--threshold", required=True, type=int)

    heartbeat = commands.add_parser("heartbeat")
    heartbeat.add_argument("--task", required=True)
    heartbeat.add_argument("--evidence", required=True)

    checkpoint = commands.add_parser("checkpoint")
    checkpoint.add_argument("--task", required=True)
    checkpoint.add_argument("--step", required=True)
    checkpoint.add_argument("--effect", required=True, choices=[item.value for item in EffectClass])
    checkpoint.add_argument("--repeatable", action="store_true")
    checkpoint.add_argument("--input-digest", required=True)
    checkpoint.add_argument("--probe", choices=["file-exists", "file-sha256", "backend-terminal"])
    checkpoint.add_argument("--target")
    checkpoint.add_argument("--expected")

    complete = commands.add_parser("complete")
    complete.add_argument("--task", required=True)

    incidents = commands.add_parser("incidents")
    incidents.add_argument("--task")
    incidents.add_argument("--limit", type=int, default=20)

    recover = commands.add_parser("recover")
    recover.add_argument("--task", required=True)
    recover.add_argument("--restart", action="store_true")

    reset = commands.add_parser("reset-circuit")
    reset.add_argument("--task", required=True)
    reset.add_argument("--reason", default="operator authorization")

    commands.add_parser("enable")
    commands.add_parser("disable")
    daemon = commands.add_parser("daemon")
    daemon.add_argument("--once", action="store_true")
    commands.add_parser("install")
    commands.add_parser("uninstall")
    return parser


def _paths(home: str | None) -> WatchdogPaths:
    environment = dict(os.environ)
    if home:
        environment["CODEX_HOME"] = home
    return WatchdogPaths.from_environment(environment, Path.home())


def _runtime(paths: WatchdogPaths):
    store = StateStore(paths.database)
    store.initialize()
    observer = CodexLogObserver(paths.codex_home / "logs_2.sqlite")
    cli_backend = CodexCliBackend()
    process_backend = CodexProcessBackend()
    recovery = RecoveryController(
        store=store,
        observer=observer,
        reconciler=ResultReconciler(),
        cli_backend=cli_backend,
        process_backend=process_backend,
        manifests_dir=paths.manifests_dir,
    )
    service = WatchdogService(
        store=store,
        observer=observer,
        recovery_controller=recovery,
        notifier=WindowsNotifier(),
        restart_probe=lambda: process_backend.is_unresponsive(None),
        audit_logger=AuditLogger(paths.logs_dir / "watchdog.log"),
    )
    return store, recovery, service


def _task_json(task) -> dict[str, Any]:
    data = asdict(task)
    data["state"] = task.state.value
    return data


def _emit(payload: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, default=str))
    else:
        for key, value in payload.items():
            print(f"{key}: {value}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    paths = _paths(args.home)
    store, recovery, service = _runtime(paths)
    config_path = paths.data_dir / "config.json"
    try:
        if args.command == "status":
            capabilities = recovery.cli_backend.capabilities()
            payload = {
                "version": __version__,
                "service": {
                    "enabled": load_enabled(config_path),
                    "cli_compatible": capabilities.compatible,
                    "cli_version": capabilities.version,
                },
                "tasks": [_task_json(task) for task in store.list_tasks(limit=50)],
            }
        elif args.command == "arm":
            payload = _task_json(
                store.arm_task(
                    args.task,
                    args.session,
                    args.task_class,
                    args.threshold,
                )
            )
        elif args.command == "heartbeat":
            task = store.get_task(args.task)
            if task.state is TaskState.ARMED:
                task = store.transition(args.task, TaskState.RUNNING, args.evidence)
            store.record_heartbeat(args.task, args.evidence)
            payload = _task_json(store.get_task(args.task))
        elif args.command == "checkpoint":
            checkpoint = store.record_checkpoint(
                task_id=args.task,
                step_id=args.step,
                effect=EffectClass(args.effect),
                repeatable=args.repeatable,
                input_digest=args.input_digest,
                probe_kind=args.probe,
                probe_target=args.target,
                expected_value=args.expected,
            )
            payload = asdict(checkpoint)
            payload["effect"] = checkpoint.effect.value
        elif args.command == "complete":
            payload = _task_json(
                store.transition(args.task, TaskState.COMPLETED, "explicit completion")
            )
        elif args.command == "incidents":
            payload = {
                "incidents": [
                    asdict(item)
                    for item in store.list_incidents(args.task, args.limit)
                ]
            }
        elif args.command == "recover":
            payload = asdict(
                recovery.recover(args.task, restart_requested=args.restart)
            )
        elif args.command == "reset-circuit":
            payload = _task_json(store.reset_circuit(args.task, args.reason))
        elif args.command in {"enable", "disable"}:
            enabled = args.command == "enable"
            set_enabled(config_path, enabled)
            payload = {"enabled": enabled}
        elif args.command == "daemon":
            if args.once:
                payload = asdict(service.poll_once())
            elif not load_enabled(config_path):
                payload = {"started": False, "reason": "disabled"}
            else:
                lock = DaemonLock(paths.data_dir / "daemon.lock")
                if not lock.acquire():
                    payload = {"started": False, "reason": "already-running"}
                else:
                    try:
                        return service.run(
                            threading.Event(),
                            enabled_check=lambda: load_enabled(config_path),
                        )
                    finally:
                        lock.release()
        elif args.command in {"install", "uninstall"}:
            payload = {
                "action": args.command,
                "reason": "use the signed project PowerShell script",
            }
        else:
            raise ValueError(f"unsupported command: {args.command}")
    except KeyError:
        _emit({"error": "task-not-found"}, args.as_json)
        return 2
    except (ValueError, InvalidTransition) as error:
        _emit({"error": type(error).__name__, "reason": str(error)}, args.as_json)
        return 2

    _emit(payload, args.as_json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
