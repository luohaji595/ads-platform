"""中台最小验收：去重、消费、同一业务日只出一张待送达、无规则则忽略。"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

INBOX_DDL = """
CREATE TABLE event_inbox (
    event_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    writer        TEXT NOT NULL,
    source        TEXT NOT NULL,
    type          TEXT NOT NULL,
    object_id     TEXT NOT NULL DEFAULT 'B0GLYW51HY',
    summary       TEXT NOT NULL,
    backlink      TEXT,
    occurred_at   TEXT NOT NULL,
    received_at   TEXT NOT NULL,
    dedupe_key    TEXT NOT NULL,
    payload_json  TEXT,
    status        TEXT NOT NULL DEFAULT 'pending',
    consumed_at   TEXT,
    UNIQUE(dedupe_key)
);
"""


def _db():
    fd, path = tempfile.mkstemp(suffix=".sqlite3")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(INBOX_DDL)
    conn.commit()
    return conn, path


class MidshipTests(unittest.TestCase):
    def setUp(self):
        self.conn, self.path = _db()

    def tearDown(self):
        self.conn.close()
        os.remove(self.path)

    def test_dedupe_same_key(self):
        from midship.inbox import write_event
        a = write_event(self.conn, writer="ingest", source="ads-ingest",
                        type="ingest.done", summary="first", business_day="2026-11-15")
        b = write_event(self.conn, writer="ingest", source="ads-ingest",
                        type="ingest.done", summary="second", business_day="2026-11-15")
        self.assertTrue(a["inserted"])
        self.assertFalse(b["inserted"])
        self.assertEqual(a["event_id"], b["event_id"])
        n = self.conn.execute("SELECT COUNT(*) FROM event_inbox").fetchone()[0]
        self.assertEqual(n, 1)

    def test_consume_ingest_and_engine(self):
        from midship.inbox import write_event
        from midship.consume import consume
        write_event(self.conn, writer="ingest", source="ads-ingest",
                    type="ingest.done", summary="data in", business_day="2026-11-15")
        write_event(self.conn, writer="engine", source="ads-engine",
                    type="engine.done", summary="ran", business_day="2026-11-13",
                    extra_key="run4", payload={"window_end": "2026-11-13", "front": 5},
                    backlink="/tmp/a.md")
        write_event(self.conn, writer="engine", source="ads-engine",
                    type="engine.done", summary="ran again", business_day="2026-11-13",
                    extra_key="run5", payload={"window_end": "2026-11-13", "front": 0},
                    backlink="/tmp/b.md")
        results = consume(self.conn)
        self.assertEqual(len(results), 3)
        statuses = {r["event_id"]: r["status"] for r in results}
        # first engine recorded + outbox, second engine deduped
        engine_rows = [r for r in results if r.get("type") == "engine.done"]
        self.assertEqual({r["status"] for r in engine_rows}, {"recorded", "deduped"})
        pending = self.conn.execute(
            "SELECT COUNT(*) FROM event_inbox WHERE status='pending'"
        ).fetchone()[0]
        self.assertEqual(pending, 0)
        outbox = self.conn.execute("SELECT * FROM tell_outbox").fetchall()
        self.assertEqual(len(outbox), 1)
        self.assertEqual(outbox[0]["business_day"], "2026-11-13")
        self.assertEqual(outbox[0]["action"], "pack_memorial")
        self.assertEqual(outbox[0]["status"], "pending")

    def test_unknown_type_ignored(self):
        from midship.inbox import write_event
        from midship.consume import consume
        write_event(self.conn, writer="captain", source="test",
                    type="unknown.ping", summary="x", business_day="2026-11-16")
        results = consume(self.conn)
        self.assertEqual(results[0]["status"], "ignored")
        status = self.conn.execute("SELECT status FROM event_inbox").fetchone()[0]
        self.assertEqual(status, "ignored")


if __name__ == "__main__":
    unittest.main()
