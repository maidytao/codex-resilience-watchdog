from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
import re
import sqlite3

from .models import EvidenceClass, Observation


TURN_ID_PATTERN = re.compile(r"\bturn_id[=:](?:Some\(\")?([A-Za-z0-9_-]+)")
CALL_ID_PATTERN = re.compile(r"\bcall_id[=:]([A-Za-z0-9_-]+)")


@dataclass(frozen=True)
class ObservationBatch:
    observations: tuple[Observation, ...]
    last_rowid: int
    rows_scanned: int
    database_status: str
    read_only: bool


class CodexLogObserver:
    def __init__(self, database: Path) -> None:
        self.database = database

    def _connect_read_only(self) -> sqlite3.Connection:
        uri = f"file:{self.database.resolve().as_posix()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=2)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        return connection

    def verify_query_only(self) -> bool:
        if not self.database.exists():
            return False
        connection = self._connect_read_only()
        try:
            row = connection.execute("PRAGMA query_only").fetchone()
            return bool(row and row[0] == 1)
        finally:
            connection.close()

    def observe(
        self,
        session_id: str,
        since_rowid: int,
        limit: int = 1000,
    ) -> ObservationBatch:
        if not self.database.exists():
            return ObservationBatch((), since_rowid, 0, "missing", True)

        bounded_limit = max(1, min(limit, 1000))
        connection = self._connect_read_only()
        try:
            rows = connection.execute(
                """
                SELECT id, ts, ts_nanos, target, feedback_log_body,
                       thread_id, process_uuid
                FROM logs
                WHERE id > ? AND thread_id = ?
                ORDER BY id ASC
                LIMIT ?
                """,
                (since_rowid, session_id, bounded_limit),
            ).fetchall()
        finally:
            connection.close()

        observations: list[Observation] = []
        last_rowid = since_rowid
        for row in rows:
            last_rowid = int(row["id"])
            observation = self._map_row(row)
            if observation is not None:
                observations.append(observation)

        return ObservationBatch(
            observations=tuple(observations),
            last_rowid=last_rowid,
            rows_scanned=len(rows),
            database_status="ok",
            read_only=True,
        )

    @staticmethod
    def _map_row(row: sqlite3.Row) -> Observation | None:
        target = str(row["target"] or "")
        body = str(row["feedback_log_body"] or "")
        normalized = body.lower()

        kind: str | None = None
        evidence = EvidenceClass.POSITIVE_PROGRESS
        if target == "codex_core::stream_events_utils" and "tool call" in normalized:
            kind = "tool_started"
        elif target == "codex_core::tools::parallel" and "completed" in normalized:
            kind = "tool_completed"
        elif target in {
            "codex_api::sse::responses",
            "codex_core::stream_events_utils",
        } and ("responses event" in normalized or "response." in normalized):
            kind = "stream_event"
        elif target == "codex_core::session::turn" and (
            "post sampling token usage" in normalized
            or "turn completed" in normalized
        ):
            kind = "backend_terminal"
            evidence = EvidenceClass.POSITIVE_TERMINAL
        elif "endpoint=\"/responses\"" in body:
            kind = "model_request"
        elif "aborted" in normalized and "turn_id" in normalized:
            kind = "backend_aborted"
            evidence = EvidenceClass.POSITIVE_TERMINAL

        if kind is None:
            return None

        seconds = int(row["ts"] or 0)
        nanos = int(row["ts_nanos"] or 0)
        observed_at = datetime.fromtimestamp(
            seconds + (nanos / 1_000_000_000), tz=UTC
        )
        turn_match = TURN_ID_PATTERN.search(body)
        call_match = CALL_ID_PATTERN.search(body)
        details = {
            "row_id": int(row["id"]),
            "target": target,
            "thread_id": row["thread_id"],
            "process_uuid": row["process_uuid"],
        }
        if turn_match:
            details["turn_id"] = turn_match.group(1)
        if call_match:
            details["call_id"] = call_match.group(1)
        return Observation(
            kind=kind,
            evidence_class=evidence,
            observed_at=observed_at,
            details=details,
        )
