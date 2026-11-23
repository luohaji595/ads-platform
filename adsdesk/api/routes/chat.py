"""对话接口：把一轮追问发出去，再把历史读回来。

这个路由**不做任何取数、不拼 prompt** —— 那些都在 ``agents/converse.py`` 里。
CLI 和 HTTP 走同一条路，否则主人命令行里问到的和界面里问到的会不一样，
到那天就没人知道该信哪个。

handler 故意写成同步 ``def``：FastAPI 会把它丢进线程池。一轮对话实测 100~160 秒，
写成 ``async def`` 会把 uvicorn 的事件循环钉死在那一刻 —— 其它 tab 会一起打不开。
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

# 显式加一次 adsdesk/ 而不是等 main.py 那边的副作用：
# 让这个模块能被单独 import（selftest、脚本、REPL）而不必先起 app。
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from agents import converse  # noqa: E402

from api.db import read_conn  # noqa: E402

router = APIRouter()

# kind → HTTP 状态。同样是「没成」，写错输入该 400，上游没答上来该 5xx；
# 前端只看状态就能决定是改输入还是稍后再试。不靠匹配错误文案判断，因为改一句中文就会变成 500。
_STATUS = {"input": 400, "gateway": 502, "model": 502}


class SayIn(BaseModel):
    text: str = Field(..., min_length=1, max_length=4000)
    # 指定用哪一天的奏折作答；不给就用最新一份。没有奏折的日子会往前取最近一份。
    as_of: str | None = None
    model: str | None = None
    session_id: str | None = None


def _empty_cite() -> dict:
    return {
        "run_id": None,
        "prompt_ver": None,
        "cited_tables": [],
        "cited_decision_ids": [],
        "null_decision": False,
    }


def _parse_cite(raw: Any) -> dict:
    """把 cited_tables 列拆成「溯源 + 引用到的表」。

    这一列历史上可能只有裸 list（早期写的），也可能是我现在写的对象。
    两种都得读得出来，否则翻到老记录会 500 —— 而老记录恰恰是最该被翻的。
    """
    if not raw:
        return {"run_id": None, "prompt_ver": None, "cited_tables": []}
    try:
        val = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        return _empty_cite()
    if isinstance(val, list):
        return {**_empty_cite(), "cited_tables": val}
    if isinstance(val, dict):
        tables = val.get("cited_tables") or []
        ids = val.get("cited_decision_ids") or []
        return {
            "run_id": val.get("run_id"),
            "prompt_ver": val.get("prompt_ver"),
            "cited_tables": tables if isinstance(tables, list) else [],
            "cited_decision_ids": ids if isinstance(ids, list) else [],
            "null_decision": bool(val.get("null_decision")),
        }
    return _empty_cite()


@router.post("/say")
def say(body: SayIn):
    """同步跑一轮对话。几十秒是正常的，返回体里带 elapsed_ms 让人心里有数。"""
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="消息是空的")
    if body.as_of:
        try:
            date.fromisoformat(body.as_of)
        except ValueError:
            raise HTTPException(
                status_code=400, detail=f"as_of 要 YYYY-MM-DD，收到 {body.as_of!r}"
            ) from None

    res = converse.run_once(
        text=text,
        session_id=body.session_id or converse.DEFAULT_SESSION,
        model=body.model,
        as_of=body.as_of,
    )

    if not res.get("ok"):
        kind = res.get("kind")
        # 写库失败但回答拿得到：把话交出去，同时明说没落库。
        # 把它压成 500 会让用户白等两分钟又什么都没看到，而答案是真实存在的。
        if kind == "write" and res.get("reply"):
            return {
                "ok": False,
                "written": False,
                "warning": res.get("error"),
                "reply": res["reply"],
                "model": res.get("model"),
                "elapsed_ms": res.get("elapsed_ms"),
                "anchors": converse.cite_anchors(res["reply"]),
            }
        raise HTTPException(
            status_code=_STATUS.get(kind, 500),
            detail=res.get("error") or "对话失败",
        )

    return {
        "ok": True,
        "turn_id": res["turn_id"],
        "user_turn_id": res["user_turn_id"],
        "reply": res["reply"],
        "model": res["model"],
        "tokens": res["tokens"],
        "elapsed_ms": res["elapsed_ms"],
        "cited_tables": res["cited_tables"],
        "cited_decision_ids": res["cited_decision_ids"],
        "run_id": res["run_id"],
        "business_day": res["business_day"],
    }


@router.get("/history")
def history(
    limit: int = Query(50, ge=1, le=200),
    session_id: str = Query(converse.DEFAULT_SESSION, min_length=1, max_length=64),
):
    """按时间正序返回最近 limit 条。

    先倒序取再翻回来：正序 limit 拿到的是最早那几条，聊久了界面会一直停在开头，
    新说的话永远看不见。
    """
    with read_conn() as conn:
        rows = conn.execute(
            "select turn_id, role, content, cited_tables, created_at from chat_turn "
            "where session_id = ? order by turn_id desc limit ?",
            (session_id, limit),
        ).fetchall()

    turns = []
    for r in reversed(rows):
        turns.append(
            {
                "turn_id": r["turn_id"],
                "role": r["role"],
                "content": r["content"],
                "created_at": r["created_at"],
                "cite": _parse_cite(r["cited_tables"]),
            }
        )
    return {"ok": True, "session_id": session_id, "turns": turns}