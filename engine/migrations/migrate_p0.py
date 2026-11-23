#!/usr/bin/env python3
"""P0: migrate ads.sqlite3 in place. Idempotent. Does not touch fact_*."""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path("/Users/baoyanan/自动化广告平台")
DB = Path("/Users/baoyanan/自动化广告平台/ingest/data/ads.sqlite3")
SQL_FILE = ROOT / "engine" / "migrations" / "2026-11-16-p0-ledger.sql"

NEW_COLUMNS = [
    ("checked_at", "TEXT"),
    ("checked_by", "TEXT"),
    ("reject_reason", "TEXT"),
    ("reject_reason_code", "TEXT"),
]

CHECKED_AT = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%dT%H:%M:%S+08:00")
REJECT_REASON = (
    "P0 清账：无奏折、无台账时的回放产物，不逐条补审。"
    "家洛 2026-11-16 拍定 reject_reason_code=legacy_pre_ledger。"
)


def column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


def main() -> int:
    if not DB.exists():
        print(f"missing db: {DB}", file=sys.stderr)
        return 1
    sql = SQL_FILE.read_text(encoding="utf-8")
    conn = sqlite3.connect(DB, timeout=10)
    conn.execute("PRAGMA busy_timeout=8000")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        conn.execute("BEGIN")
        existing = column_names(conn, "decision_log")
        for name, typ in NEW_COLUMNS:
            if name not in existing:
                conn.execute(f"ALTER TABLE decision_log ADD COLUMN {name} {typ}")
                print(f"added column decision_log.{name}")
            else:
                print(f"skip column decision_log.{name}")
        conn.executescript(sql)
        cur = conn.execute(
            "SELECT COUNT(*) FROM decision_log WHERE status = 'proposed'"
        )
        n_proposed = cur.fetchone()[0]
        cur = conn.execute(
            "UPDATE decision_log "
            "SET status = 'expired', "
            "    checked_at = ?, "
            "    checked_by = '家洛', "
            "    reject_reason = ?, "
            "    reject_reason_code = 'legacy_pre_ledger', "
            "    expires_at = ? "
            "WHERE status = 'proposed'",
            (CHECKED_AT, REJECT_REASON, CHECKED_AT),
        )
        print(f"expired proposed rows: {cur.rowcount} (saw {n_proposed} before update)")
        leftover = conn.execute(
            "SELECT status, COUNT(*) FROM decision_log GROUP BY status"
        ).fetchall()
        print("decision_log status:", leftover)
        open_n = conn.execute("SELECT COUNT(*) FROM v_open_decisions").fetchone()[0]
        print(f"v_open_decisions: {open_n}")
        tables = [
            "policy",
            "param_override",
            "observation_task",
            "experiment",
            "reason_hit",
            "reason_excluded",
            "agent_assumption",
            "competitor_watchlist",
            "event_inbox",
        ]
        for t in tables:
            n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            print(f"table {t}: ok, rows={n}")
        cols = column_names(conn, "decision_log")
        missing = [c for c, _ in NEW_COLUMNS if c not in cols]
        if missing:
            raise RuntimeError(f"missing columns: {missing}")
        if leftover != [("expired", n_proposed)] and leftover != [("expired", 78)]:
            # allow extra non-proposed statuses if any appear
            statuses = {s: c for s, c in leftover}
            if statuses.get("proposed", 0) != 0:
                raise RuntimeError(f"proposed leftover: {leftover}")
        conn.execute("COMMIT")
        print(f"committed at {CHECKED_AT}")
        return 0
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
