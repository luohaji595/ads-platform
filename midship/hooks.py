"""给 ingest / engine 收尾调用。失败只记账，不回滚已经拉完或算完的数据。"""

from __future__ import annotations

import sqlite3
import sys
import traceback
from typing import Any


def _ok(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        print(f"[中台] {fn.__name__} 失败（上游任务不回滚）：{e}", file=sys.stderr, flush=True)
        traceback.print_exc()
        return {"ok": False, "error": str(e)}


def emit_ingest_done(
    conn: sqlite3.Connection,
    *,
    days: list[str],
    results: list[dict],
    market_id: int,
    occurred_at: str | None = None,
) -> list[dict]:
    from .inbox import write_event
    from .consume import consume

    written = []
    failed = [r for r in results if r.get("error")]
    flag = "部分失败" if failed else "完成"
    for day in days:
        summary = f"ingest {flag}：市场 {market_id}，业务日 {day}，{len(results)} 个数据集"
        rec = write_event(
            conn,
            writer="ingest",
            source="ads-ingest",
            type="ingest.done",
            summary=summary,
            business_day=day,
            occurred_at=occurred_at,
            payload={
                "business_day": day,
                "market_id": market_id,
                "results": results,
                "failed": len(failed),
            },
        )
        written.append(rec)
    consumed = consume(conn)
    return {"writes": written, "consumed": consumed}


def emit_engine_done(
    conn: sqlite3.Connection,
    *,
    run_id: int | None,
    window_start: str,
    window_end: str,
    front: int,
    questions: int,
    report_path: str | None,
    note: str | None = None,
    occurred_at: str | None = None,
) -> dict:
    from .inbox import write_event
    from .consume import consume

    extra = f"run{run_id}" if run_id is not None else "dry"
    summary = (
        f"engine 跑完：窗口 {window_start}~{window_end}，"
        f"正文 {front} 条，问题 {questions} 题，run #{run_id}"
    )
    rec = write_event(
        conn,
        writer="engine",
        source="ads-engine",
        type="engine.done",
        summary=summary,
        business_day=window_end,
        extra_key=extra,
        backlink=report_path,
        occurred_at=occurred_at,
        payload={
            "business_day": window_end,
            "window_start": window_start,
            "window_end": window_end,
            "run_id": run_id,
            "front": front,
            "questions": questions,
            "report_path": report_path,
            "note": note,
        },
    )
    consumed = consume(conn)
    return {"write": rec, "consumed": consumed}


def safe_ingest_done(conn, **kwargs) -> Any:
    return _ok(emit_ingest_done, conn, **kwargs)


def safe_engine_done(conn, **kwargs) -> Any:
    return _ok(emit_engine_done, conn, **kwargs)
