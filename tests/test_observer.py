from __future__ import annotations

import sqlite3

from codex_resilience_watchdog.models import EvidenceClass
from codex_resilience_watchdog.observer import CodexLogObserver
from tests.helpers import TemporaryHomeTestCase


class CodexLogObserverTest(TemporaryHomeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.database = self.codex_home / "logs_2.sqlite"
        connection = sqlite3.connect(self.database)
        connection.execute(
            """
            CREATE TABLE logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                ts_nanos INTEGER NOT NULL,
                level TEXT NOT NULL,
                target TEXT NOT NULL,
                feedback_log_body TEXT,
                module_path TEXT,
                file TEXT,
                line INTEGER,
                thread_id TEXT,
                process_uuid TEXT,
                estimated_bytes INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO logs (
                ts, ts_nanos, level, target, feedback_log_body,
                thread_id, process_uuid
            ) VALUES (?, 0, 'INFO', ?, ?, ?, 'process-1')
            """,
            [
                (
                    1,
                    "codex_core::stream_events_utils",
                    "tool call started turn_id=turn-1 call_id=call-1",
                    "session-1",
                ),
                (
                    2,
                    "codex_core::tools::parallel",
                    "tool call completed turn_id=turn-1 call_id=call-1",
                    "session-1",
                ),
                (
                    3,
                    "codex_api::sse::responses",
                    "unhandled responses event: response.output_text.delta turn_id=turn-1",
                    "session-1",
                ),
                (
                    4,
                    "codex_core::session::turn",
                    "post sampling token usage turn_id=turn-1",
                    "session-1",
                ),
                (
                    5,
                    "codex_core::session::turn",
                    "post sampling token usage turn_id=turn-other",
                    "session-other",
                ),
            ],
        )
        connection.commit()
        connection.close()
        self.observer = CodexLogObserver(self.database)

    def test_observer_opens_database_read_only(self) -> None:
        batch = self.observer.observe("session-1", since_rowid=0)

        self.assertTrue(batch.read_only)
        self.assertTrue(self.observer.verify_query_only())

    def test_observer_maps_positive_events_and_bounds_session(self) -> None:
        batch = self.observer.observe("session-1", since_rowid=0)

        self.assertEqual(
            [item.kind for item in batch.observations],
            ["tool_started", "tool_completed", "stream_event", "backend_terminal"],
        )
        self.assertEqual(batch.last_rowid, 4)
        self.assertEqual(
            batch.observations[-1].evidence_class,
            EvidenceClass.POSITIVE_TERMINAL,
        )

    def test_cursor_skips_already_seen_rows(self) -> None:
        batch = self.observer.observe("session-1", since_rowid=2)

        self.assertEqual(
            [item.kind for item in batch.observations],
            ["stream_event", "backend_terminal"],
        )

    def test_missing_database_is_reported_without_creation(self) -> None:
        missing = self.codex_home / "missing.sqlite"

        batch = CodexLogObserver(missing).observe("session-1", 0)

        self.assertEqual(batch.database_status, "missing")
        self.assertFalse(missing.exists())

    def test_limit_is_hard_capped_at_one_thousand(self) -> None:
        connection = sqlite3.connect(self.database)
        connection.executemany(
            """
            INSERT INTO logs (
                ts, ts_nanos, level, target, feedback_log_body,
                thread_id, process_uuid
            ) VALUES (?, 0, 'INFO', 'codex_api::sse::responses',
                      'unhandled responses event: delta turn_id=turn-1',
                      'session-1', 'process-1')
            """,
            [(index,) for index in range(10, 1110)],
        )
        connection.commit()
        connection.close()

        batch = self.observer.observe("session-1", 0, limit=5000)

        self.assertEqual(batch.rows_scanned, 1000)


if __name__ == "__main__":
    import unittest

    unittest.main()
