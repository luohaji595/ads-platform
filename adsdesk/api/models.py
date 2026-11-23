"""Pydantic 请求/响应模型。

只放「工作台真正会传的字段」。宁可多写一层校验，也不让前端随便塞
任意 JSON 进库 —— 审批记录是唯一真相源，脏数据比没数据更难修。
"""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

# 打钩后的目标状态。注意这里没有 executed：
# 「批准」和「已改」是两件事，中间隔着用户手动操作后台。
ApproveStatus = Literal["approved", "rejected"]


class ApproveIn(BaseModel):
    decision_id: int = Field(..., ge=1)
    status: ApproveStatus
    # 打叉必须给原因码，打钩可空
    reject_reason_code: str | None = None
    reject_reason: str | None = None
    checked_by: str = Field(default="owner", max_length=64)
    # 证据不够（status='observing'）的条目要强制批准时：显式二次确认 + 必填理由。
    # 引擎的判断照旧是「证据不够」，这里只记录「人拍了板、理由是什么」。
    # 非 observing 的条目传 override=true 会被拒 —— 不然「强制」两个字就贬值了。
    override: bool = False
    override_reason: str | None = Field(default=None, max_length=1000)


class ApproveBatchIn(BaseModel):
    items: list[ApproveIn] = Field(..., min_length=1, max_length=50)


class ExecutedIn(BaseModel):
    """用户改完后台回来点「已改」。归因窗从这一刻开始算。"""

    decision_id: int = Field(..., ge=1)
    executed_value: str | None = None
    executed_by: str = Field(default="owner", max_length=64)
    # 归因窗长度（天）。按动作类型有默认值，前端可覆盖。
    attribution_days: int | None = Field(default=None, ge=1, le=60)


class QuestionAnswerIn(BaseModel):
    """问题打钩。一个 question 可勾多个选项（原因可并列）。"""

    question_id: str = Field(..., min_length=1, max_length=32)
    run_id: int = Field(..., ge=1)
    object_id: str | None = None
    selected: list[int] = Field(..., min_length=0, max_length=8)
    # 显式划掉：这些选项「以后别再问了」。必须和 selected 分开 ——
    # 未勾是「今天没表态」，划掉是「这个方向不要」，两者混在一起会让
    # 划掉的东西下次又冒出来，打钩就白打了。
    excluded: list[int] = Field(default_factory=list, max_length=8)
    free_text: str | None = Field(default=None, max_length=2000)
    skipped: bool = False
    answered_by: str = Field(default="owner", max_length=64)


class ExecOrderIn(BaseModel):
    ticket_id: str | None = None
    run_id: int | None = None
    decision_ids: list[int] = Field(..., min_length=1, max_length=100)
    note: str | None = Field(default=None, max_length=1000)


class Ok(BaseModel):
    ok: bool = True
    detail: Any = None
