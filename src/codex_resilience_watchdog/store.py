from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
from pathlib import Path
import sqlite3
from typing import Iterator

from .models import EffectClass, TaskState


class InvalidTransition(ValueError):
    pass


@dataclass(frozen=True)
class TaskRecord:
    task_id: str
    session_id: str
    task_class: str
    threshold_seconds: int
    state: TaskState
    generation: int
    recovery_count: int
    restart_count: int
    same_fingerprint_count: int
    last_fault_fingerprint: str | None
    circuit_reason: str | None
    last_log_rowid: int
    last_progress_at: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class CheckpointRecord:
    checkpoint_id: int
    task_id: str
    step_id: str
    effect: EffectClass
    repeatable: bool
    input_digest: str
    idempotency_key: str
    probe_kind: str | None
    probe_target: str | None
    expected_value: str | None
    result_status: str
    created_at: str


@dataclass(frozen=True)
class IncidentRecord:
    incident_id: int
    task_id: str | None
    kind: str
    reason: str
    created_at: str


ALLOWED_TRANSITIONS: dict[TaskState, set[TaskState]] = {
    TaskState.ARMED: {
        TaskState.RUNNING,
        TaskState.SUSPECT,
        TaskState.FAILED,
        TaskState.CIRCUIT_OPEN,
    },
    TaskState.RUNNING: {
        TaskState.SUSPECT,
        TaskState.COMPLETED,
        TaskState.FAILED,
        TaskState.CIRCUIT_OPEN,
    },
    TaskState.SUSPECT: {
        TaskState.RECONCILING,
        TaskState.RUNNING,
        TaskState.COMPLETED,
        TaskState.CIRCUIT_OPEN,
    },
    TaskState.RECONCILING: {
        TaskState.RUNNING,
        TaskState.RECOVERY_READY,
        TaskState.PENDING_CONFIRMATION,
        TaskState.COMPLETED,
        TaskState.FAILED,
        TaskState.CIRCUIT_OPEN,
    },
    TaskState.RECOVERY_READY: {
        TaskState.RESTARTING,
        TaskState.RESUMING,
        TaskState.PENDING_CONFIRMATION,
        TaskState.CIRCUIT_OPEN,
        TaskState.FAILED,
    },
    TaskState.RESTARTING: {
        TaskState.RECOVERY_READY,
        TaskState.RESUMING,
        TaskState.PENDING_CONFIRMATION,
        TaskState.CIRCUIT_OPEN,
        TaskState.FAILED,
    },
    TaskState.RESUMING: {
        TaskState.RUNNING,
        TaskState.SUSPECT,
        TaskState.PENDING_CONFIRMATION,
        TaskState.COMPLETED,
        TaskState.FAILED,
        TaskState.CIRCUIT_OPEN,
    },
    TaskState.PENDING_CONFIRMATION: {
        TaskState.RECONCILING,
        TaskState.RECOVERY_READY,
        TaskState.COMPLETED,
        TaskState.FAILED,
        TaskState.CIRCUIT_OPEN,
    },
    TaskState.COMPLETED: set(),
    TaskState.FAILED: set(),
    TaskState.CIRCUIT_OPEN: set(),
}


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _timestamp(value: datetime | None = None) -> str:
    return (value or _utc_now()).astimezone(UTC).isoformat()


class StateStore:
    def __init__(self, database: Path) -> None:
        self.database = database

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.database.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    task_class TEXT NOT NULL,
                    threshold_seconds INTEGER NOT NULL CHECK (threshold_seconds > 0),
                    state TEXT NOT NULL,
                    generation INTEGER NOT NULL DEFAULT 0,
                    recovery_count INTEGER NOT NULL DEFAULT 0,
                    restart_count INTEGER NOT NULL DEFAULT 0,
                    same_fingerprint_count INTEGER NOT NULL DEFAULT 0,
                    last_fault_fingerprint TEXT,
                    circuit_reason TEXT,
                    last_log_rowid INTEGER NOT NULL DEFAULT 0,
                    last_progress_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS checkpoints (
                    checkpoint_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                    step_id TEXT NOT NULL,
                    effect TEXT NOT NULL,
                    repeatable INTEGER NOT NULL,
                    input_digest TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    probe_kind TEXT,
                    probe_target TEXT,
                    expected_value TEXT,
                    result_status TEXT NOT NULL DEFAULT 'unknown',
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS heartbeats (
                    heartbeat_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
                    evidence TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS leases (
                    task_id TEXT PRIMARY KEY REFERENCES tasks(task_id) ON DELETE CASCADE,
                    owner TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS incidents (
                    incident_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT REFERENCES tasks(task_id) ON DELETE SET NULL,
                    kind TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            task_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(tasks)")
            }
            if "last_log_rowid" not in task_columns:
                connection.execute(
                    "ALTER TABLE tasks ADD COLUMN last_log_rowid INTEGER NOT NULL DEFAULT 0"
                )

    def arm_task(
        self,
        task_id: str,
        session_id: str,
        task_class: str,
        threshold_seconds: int,
    ) -> TaskRecord:
        if not task_id or not session_id:
            raise ValueError("task_id and session_id are required")
        if threshold_seconds <= 0:
            raise ValueError("threshold_seconds must be positive")
        now = _timestamp()
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO tasks (
                        task_id, session_id, task_class, threshold_seconds,
                        state, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        task_id,
                        session_id,
                        task_class,
                        threshold_seconds,
                        TaskState.ARMED.value,
                        now,
                        now,
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise ValueError(f"task already exists: {task_id}") from error
        return self.get_task(task_id)

    def get_task(self, task_id: str) -> TaskRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
        if row is None:
            raise KeyError(task_id)
        return self._task_from_row(row)

    def list_tasks(self, limit: int = 50) -> list[TaskRecord]:
        bounded = max(1, min(limit, 50))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM tasks ORDER BY updated_at DESC LIMIT ?", (bounded,)
            ).fetchall()
        return [self._task_from_row(row) for row in rows]

    def transition(self, task_id: str, new_state: TaskState, reason: str) -> TaskRecord:
        current = self.get_task(task_id)
        if new_state not in ALLOWED_TRANSITIONS[current.state]:
            raise InvalidTransition(f"{current.state.value} -> {new_state.value}")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE tasks SET state = ?, updated_at = ? WHERE task_id = ?",
                (new_state.value, _timestamp(), task_id),
            )
            connection.execute(
                "INSERT INTO incidents (task_id, kind, reason, created_at) VALUES (?, ?, ?, ?)",
                (task_id, "state_transition", reason, _timestamp()),
            )
        return self.get_task(task_id)

    def open_circuit(self, task_id: str, reason: str) -> TaskRecord:
        current = self.get_task(task_id)
        if current.state in {TaskState.COMPLETED, TaskState.FAILED}:
            raise InvalidTransition(f"cannot open circuit from {current.state.value}")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE tasks
                SET state = ?, circuit_reason = ?, updated_at = ?
                WHERE task_id = ?
                """,
                (TaskState.CIRCUIT_OPEN.value, reason, _timestamp(), task_id),
            )
            connection.execute(
                "INSERT INTO incidents (task_id, kind, reason, created_at) VALUES (?, ?, ?, ?)",
                (task_id, "circuit_opened", reason, _timestamp()),
            )
        return self.get_task(task_id)

    def reset_circuit(self, task_id: str, reason: str) -> TaskRecord:
        current = self.get_task(task_id)
        if current.state is not TaskState.CIRCUIT_OPEN:
            raise InvalidTransition("only an open circuit can be reset")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE tasks
                SET state = ?, circuit_reason = NULL, updated_at = ?
                WHERE task_id = ?
                """,
                (TaskState.PENDING_CONFIRMATION.value, _timestamp(), task_id),
            )
            connection.execute(
                "INSERT INTO incidents (task_id, kind, reason, created_at) VALUES (?, ?, ?, ?)",
                (task_id, "circuit_reset", reason, _timestamp()),
            )
        return self.get_task(task_id)

    def increment_recovery(self, task_id: str, fingerprint: str) -> TaskRecord:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT last_fault_fingerprint FROM tasks WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None:
                raise KeyError(task_id)
            same_count_expression = (
                "same_fingerprint_count + 1"
                if row["last_fault_fingerprint"] == fingerprint
                else "1"
            )
            connection.execute(
                f"""
                UPDATE tasks
                SET generation = generation + 1,
                    recovery_count = recovery_count + 1,
                    same_fingerprint_count = {same_count_expression},
                    last_fault_fingerprint = ?,
                    updated_at = ?
                WHERE task_id = ?
                """,
                (fingerprint, _timestamp(), task_id),
            )
        return self.get_task(task_id)

    def increment_restart(self, task_id: str) -> TaskRecord:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                """
                UPDATE tasks
                SET restart_count = restart_count + 1, updated_at = ?
                WHERE task_id = ?
                """,
                (_timestamp(), task_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(task_id)
        return self.get_task(task_id)

    def record_heartbeat(self, task_id: str, evidence: str) -> None:
        now = _timestamp()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO heartbeats (task_id, evidence, created_at) VALUES (?, ?, ?)",
                (task_id, evidence, now),
            )
            connection.execute(
                "UPDATE tasks SET last_progress_at = ?, updated_at = ? WHERE task_id = ?",
                (now, now, task_id),
            )

    def update_log_cursor(self, task_id: str, last_log_rowid: int) -> None:
        if last_log_rowid < 0:
            raise ValueError("last_log_rowid cannot be negative")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE tasks
                SET last_log_rowid = CASE
                    WHEN last_log_rowid < ? THEN ? ELSE last_log_rowid END,
                    updated_at = ?
                WHERE task_id = ?
                """,
                (last_log_rowid, last_log_rowid, _timestamp(), task_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(task_id)

    def record_checkpoint(
        self,
        task_id: str,
        step_id: str,
        effect: EffectClass,
        repeatable: bool,
        input_digest: str,
        probe_kind: str | None = None,
        probe_target: str | None = None,
        expected_value: str | None = None,
    ) -> CheckpointRecord:
        material = f"{task_id}:{step_id}:{effect.value}:{input_digest}"
        digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
        idempotency_key = f"{task_id}:{step_id}:{effect.value}:{digest}"
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO checkpoints (
                    task_id, step_id, effect, repeatable, input_digest,
                    idempotency_key, probe_kind, probe_target, expected_value,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    step_id,
                    effect.value,
                    int(repeatable),
                    input_digest,
                    idempotency_key,
                    probe_kind,
                    probe_target,
                    expected_value,
                    _timestamp(),
                ),
            )
            checkpoint_id = int(cursor.lastrowid)
        return self.get_checkpoint(checkpoint_id)

    def get_checkpoint(self, checkpoint_id: int) -> CheckpointRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM checkpoints WHERE checkpoint_id = ?", (checkpoint_id,)
            ).fetchone()
        if row is None:
            raise KeyError(checkpoint_id)
        return self._checkpoint_from_row(row)

    def latest_checkpoint(self, task_id: str) -> CheckpointRecord:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM checkpoints
                WHERE task_id = ?
                ORDER BY checkpoint_id DESC LIMIT 1
                """,
                (task_id,),
            ).fetchone()
        if row is None:
            raise KeyError(f"no checkpoint for {task_id}")
        return self._checkpoint_from_row(row)

    def acquire_lease(
        self,
        task_id: str,
        owner: str,
        now: datetime,
        ttl_seconds: int,
    ) -> bool:
        expires_at = now + timedelta(seconds=ttl_seconds)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT owner, expires_at FROM leases WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is not None:
                current_expiry = datetime.fromisoformat(row["expires_at"])
                if current_expiry > now and row["owner"] != owner:
                    return False
            connection.execute(
                """
                INSERT INTO leases (task_id, owner, expires_at)
                VALUES (?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    owner = excluded.owner,
                    expires_at = excluded.expires_at
                """,
                (task_id, owner, _timestamp(expires_at)),
            )
        return True

    def release_lease(self, task_id: str, owner: str) -> bool:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM leases WHERE task_id = ? AND owner = ?", (task_id, owner)
            )
        return cursor.rowcount == 1

    def record_incident(self, task_id: str | None, kind: str, reason: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO incidents (task_id, kind, reason, created_at) VALUES (?, ?, ?, ?)",
                (task_id, kind, reason, _timestamp()),
            )

    def list_incidents(
        self,
        task_id: str | None = None,
        limit: int = 50,
    ) -> list[IncidentRecord]:
        bounded = max(1, min(limit, 50))
        with self._connect() as connection:
            if task_id is None:
                rows = connection.execute(
                    "SELECT * FROM incidents ORDER BY incident_id DESC LIMIT ?",
                    (bounded,),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT * FROM incidents
                    WHERE task_id = ?
                    ORDER BY incident_id DESC LIMIT ?
                    """,
                    (task_id, bounded),
                ).fetchall()
        return [
            IncidentRecord(
                incident_id=row["incident_id"],
                task_id=row["task_id"],
                kind=row["kind"],
                reason=row["reason"],
                created_at=row["created_at"],
            )
            for row in rows
        ]

    @staticmethod
    def _task_from_row(row: sqlite3.Row) -> TaskRecord:
        return TaskRecord(
            task_id=row["task_id"],
            session_id=row["session_id"],
            task_class=row["task_class"],
            threshold_seconds=row["threshold_seconds"],
            state=TaskState(row["state"]),
            generation=row["generation"],
            recovery_count=row["recovery_count"],
            restart_count=row["restart_count"],
            same_fingerprint_count=row["same_fingerprint_count"],
            last_fault_fingerprint=row["last_fault_fingerprint"],
            circuit_reason=row["circuit_reason"],
            last_log_rowid=row["last_log_rowid"],
            last_progress_at=row["last_progress_at"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _checkpoint_from_row(row: sqlite3.Row) -> CheckpointRecord:
        return CheckpointRecord(
            checkpoint_id=row["checkpoint_id"],
            task_id=row["task_id"],
            step_id=row["step_id"],
            effect=EffectClass(row["effect"]),
            repeatable=bool(row["repeatable"]),
            input_digest=row["input_digest"],
            idempotency_key=row["idempotency_key"],
            probe_kind=row["probe_kind"],
            probe_target=row["probe_target"],
            expected_value=row["expected_value"],
            result_status=row["result_status"],
            created_at=row["created_at"],
        )
