#!/usr/bin/env python3
"""收敛 agent —— 把 agent_eval 里到期的可验证断言回填成判定结果。

为什么需要这个文件
------------------
诊断 agent 每跑一次就往 agent_eval 写一批「可验证断言」：
  「#93 R30 在接下来 3 个 run 中仍会以相同结论出现」
  「#99 目标 ACOS 不变的前提下，本组合到 11-29 滚动 ACOS 仍高于 0.70」
这些断言是给 agent 自己打分的 —— 它说的话后来对不对。
但在此之前没有任何代码去回填，104 条断言全部停在 verdict='pending'，
等于「打分表」建好了却从不阅卷，诊断 agent 的准确率永远无法度量。

这个文件就是阅卷器。它只做三件事：
  1. 找出 verifiable_at <= 今天、且 verdict 仍为 'pending' 的断言；
  2. 能用 SQL 算出来的，直接用 SQL 算 —— 不问模型；
  3. 算不出来的，把「断言原文 + 到期日之后的事实」喂给模型，让它判。

铁律
----
* **只写 agent_eval 的 verdict/actual/checked_at 三列，以及自己的 converge_run。**
  不写 decision_log、不写 policy、不写任何 fact 表、不调广告后台。
  agent 的自我评分绝不能反过来污染被评分的对象。
* **窗口没走完 → 保持 pending，一条都不写。**
  这里最容易犯的错是「数据只到 11-17，断言窗口到 11-29，就用已有 2 天数据硬判」。
  那不是收敛，那是自欺 —— 一条本来正确的断言会被记成错误，
  一条本来错误的断言会被记成正确，两种都会污染 agent 的准确率统计。
  唯一例外是**单调可判**的情形（见 _MONOTONE）。
* **「数据没追平」不等于 unverifiable。** 这是 2026-11-19 修掉的第一个真 bug：
  launchd 07:15 跑的时候 ingest 只覆盖到 D-1，而 _parse_window 对没写日期区间的
  method 返回 (None, verifiable_at)、写了区间的也常把终点写到期日当天 ——
  于是「到期日当天判」必然 partial，旧代码判 unverifiable，而幂等 UPDATE 带
  `verdict='pending'`，这一条就永久钉死成 unverifiable，再也不会重判。
  正确语义只有两个：
    unverifiable = 数据齐了、这条断言本身无法用现有事实判定（永久结论）；
    pending      = 数据还没到 / 这轮没轮到（临时状态，下一轮自动重判）。
  所以覆盖不足时一律返回 pending，等 ingest 追平后自然转成 correct/wrong/unverifiable。
* **幂等**：UPDATE 的 WHERE 里带 `verdict = 'pending'`。
  同一天重跑、并发重跑，都不会把已经判过的东西改来改去。
  判定一旦落库就是历史事实，要改只能人工改。

数据现实（2026-11-18 实测，写在这里是因为它决定了 90% 的行为）
--------------------------------------------------------------
* 三张 fact 表的最新 report_date 都是 2026-11-17。
* 104 条断言的 verifiable_at 分布在 2026-11-19 ~ 2026-12-01，**今天到期数为 0**。
  所以 `--dry-run` 默认是空跑；要看到真实判定必须用 `--as-of`。
* 断言窗口绝大多数落在 11-16 之后，即「到期日到了、但数据还没追到」。
  这批在真实运行里会**保持 pending 到下一天**（数据止于 D-1，而窗口终点 >= D），
  不是 unverifiable —— 见上面铁律第三条。也就是说每条断言的判定日约等于
  verifiable_at + 1 天，这是数据物理决定的，不是延迟。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

# 两个 path 插入，顺序不能反：
#   parents[1] = adsdesk/，让 `from agents import context, gateway` 能解析；
#   parents[2]/engine 让 `import facts` 能解析（facts.py 内部还 import scope，同目录）。
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "engine"))

from agents import context, gateway  # noqa: E402

try:  # facts 只读，这里只借用二项检验，绝不调用任何写函数
    import facts  # noqa: E402
except Exception as _exc:  # pragma: no cover - 引擎目录被改动时的兜底
    facts = None
    _FACTS_ERROR = str(_exc)

CST = ZoneInfo("Asia/Shanghai")
PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "converge_v1.md"
PROMPT_VER = hashlib.sha1(PROMPT_PATH.read_bytes()).hexdigest()[:8] if PROMPT_PATH.exists() else "00000000"

# 一次最多喂给模型多少条 —— 模型是按条计费的，也是按条超时的。
LLM_BATCH_LIMIT = 8

# 单次模型调用的 max_tokens。必须显式传，且必须远小于 gateway 默认的 32000：
# 实测上游按 max_tokens 计配额，32000 直接被 429 拒（同一 prompt 降到 2000 立刻通）。
# 这条 prompt 的产出只有一个 {"verdict","reason"}，6000 足够，
# 而模型带思维链，给 200 又会被 reasoning 吃光导致 finish=length。
LLM_MAX_TOKENS = 6000

# 429 的退避重试。为什么必须在 converge 里做而不是靠 gateway 的 retries：
# gateway 只对网络层异常（URLError/TimeoutError/OSError）重试，
# HTTPError 一律包成 GatewayError 直接抛，429 正好走这条路。
# 而实测 429 是突发性的 —— 同一批 6 条断言，上一轮 5 条成功、这一轮 5 条全 429。
# 不做退避的话，生产里一次抖动就会把几十条断言写成 unverifiable，
# 而 unverifiable 是「永久写入」的（幂等规则：已填过的不再改），错就钉死了。
LLM_RETRY_ON_429 = 3
LLM_BACKOFF_SECONDS = (4, 12, 30)

# 模型降级链。实测同一天里：
#   auto/glm-5.3                → 对任何 payload 都 400（上游挂了，连 {"ok":1} 都拒）
#   auto/deepseek-v4.1-flash    → 429
#   auto/kimi-k3.1-pro          → 502
#   auto/deepseek-v4.1-flash-1017 / group/smart-split → 正常
# 单模型硬编码等于把回填器绑在某一个上游的存活上，所以按顺序试，
# 第一个成功即用。顺序按「便宜且稳定」排，glm-5.3 放最后因为它当前最不稳。
LLM_MODEL_CHAIN = (
    "auto/deepseek-v4.1-flash-1017",
    "group/smart-split",
    "auto/glm-5.3",
)

# 两次模型调用之间的最小间隔。429 明显与请求速率相关，串行慢一点比批量重试便宜。
LLM_THROTTLE_SECONDS = 1.5

# 本轮真正应答过的模型名。降级链会让实际模型与 --model 不一致，
# converge_run.model 必须写实际值 —— 否则事后看日志会以为「glm-5.3 判的」，
# 拿同一个模型重放却复现不出结论。
_MODELS_USED: list[str] = []

# 卡在 running 超过这么久就认为进程死了。按墙钟判，不按进程判：
# 这个脚本会被 launchd 反复拉起，进程号每次都变，用进程判 stale 必然误杀。
STALE_RUN_MINUTES = 30


# ------------------------------------------------------------------ 建表

def ensure_schema(conn: sqlite3.Connection) -> None:
    """建 converge_run，并回收上次卡死的运行记录。

    converge_run 跟 agent_run 是两张表，不是一个 job 取值。
    理由是两者的列完全不同：agent_run 关心 token / prompt_ver / input_digest，
    收敛关心的是「这次判了多少条、对了多少、错了多少」。
    硬塞进 agent_run 只能靠 output_json 装计数，查询时得 json_extract，
    而这张表恰恰是要被反复聚合统计（看 agent 准确率走势）的，不值得。
    """
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS converge_run (
            converge_run_id INTEGER PRIMARY KEY,
            started_at      TEXT NOT NULL,
            finished_at     TEXT,
            status          TEXT NOT NULL DEFAULT 'running',
            n_due           INTEGER NOT NULL DEFAULT 0,
            n_filled        INTEGER NOT NULL DEFAULT 0,
            n_correct       INTEGER NOT NULL DEFAULT 0,
            n_wrong         INTEGER NOT NULL DEFAULT 0,
            n_unverifiable  INTEGER NOT NULL DEFAULT 0,
            n_deferred      INTEGER NOT NULL DEFAULT 0,
            n_objection_due     INTEGER NOT NULL DEFAULT 0,
            n_objection_judged  INTEGER NOT NULL DEFAULT 0,
            model           TEXT,
            error           TEXT
        );
        CREATE INDEX IF NOT EXISTS ix_converge_run_started
            ON converge_run(started_at DESC);
        """
    )
    # n_deferred 是后加的：早期 converge_run 只有「到期/回填」两个数，
    # 到期 1 条、回填 0 条看起来像卡住，其实是「数据没追平、下轮重判」。
    # SQLite 的 ALTER TABLE 没有 IF NOT EXISTS，只能自己查 pragma 再补列。
    cols = {r[1] for r in conn.execute("PRAGMA table_info(converge_run)")}
    if "n_deferred" not in cols:
        conn.execute("ALTER TABLE converge_run ADD COLUMN n_deferred INTEGER NOT NULL DEFAULT 0")
    # 两列 objection 计数单独记，不并进 n_due / n_filled。
    # 理由：agent_eval 的 n_due 是「断言到期数」，objection 的到期数是另一回事，
    # 混在一起之后「今天到底判了几条质疑」这个数就永远查不出来，
    # 而质疑命中率正是主人决定要不要继续听 agent 的唯一依据。
    if "n_objection_due" not in cols:
        conn.execute("ALTER TABLE converge_run ADD COLUMN n_objection_due INTEGER NOT NULL DEFAULT 0")
    if "n_objection_judged" not in cols:
        conn.execute("ALTER TABLE converge_run ADD COLUMN n_objection_judged INTEGER NOT NULL DEFAULT 0")
    conn.commit()
    _reap_stale(conn)


def _reap_stale(conn: sqlite3.Connection, minutes: int = STALE_RUN_MINUTES) -> int:
    """把卡在 running 的记录置 failed，返回回收条数。

    不做这件事的后果：进程被 kill 后这条记录永远 running，
    以后所有「上次跑到哪」的判断全部失真。
    """
    cutoff = (datetime.now(CST) - timedelta(minutes=minutes)).isoformat(timespec="seconds")
    cur = conn.execute(
        "update converge_run set status='failed', finished_at=?, "
        "error=coalesce(error,'进程中断，无收尾记录') "
        "where status='running' and started_at < ?",
        (datetime.now(CST).isoformat(timespec="seconds"), cutoff),
    )
    if cur.rowcount:
        conn.commit()
    return cur.rowcount


# ------------------------------------------------------------------ 数据覆盖

def _coverage(conn: sqlite3.Connection) -> tuple[str | None, str | None]:
    """三张 fact 表合起来的实际数据日期区间。

    为什么要取三张表的并集而不是各自记：
    断言可能横跨 campaign / keyword / placement 三个粒度，
    判定时只关心「这一天到底有没有事实」，不关心是哪张表给的。
    只要有一张表覆盖到，就不该因为另一张表缺而判 unverifiable。
    """
    row = conn.execute(
        """
        select min(d) as d_min, max(d) as d_max from (
            select min(report_date) as d from fact_campaign_daily
            union all select max(report_date) from fact_campaign_daily
            union all select min(report_date) from fact_keyword_daily
            union all select max(report_date) from fact_keyword_daily
            union all select min(report_date) from fact_placement_daily
            union all select max(report_date) from fact_placement_daily
        ) where d is not null
        """
    ).fetchone()
    if not row or not row["d_min"]:
        return None, None
    return row["d_min"], row["d_max"]


# ------------------------------------------------------------------ 窗口与实体解析
#
# 为什么靠正则解析 method 文本，而不是让模型先抽参数：
#   模型抽参数会引入随机性 —— 同一个 method 两次抽出不同的 campaign_id，
#   判定就不可复现了。断言是历史事实，判定必须可复现。
#   正则抽不出来的，交给模型整条判（而不是抽一半再算），
#   这样至少「抽不出来 → 模型判」这条路是可解释的。

_FULL_RANGE = re.compile(
    r"(20\d{2}-\d{2}-\d{2})\s*(?:\.\.|[~～]|至|到|—|–|-{1,2}>?)\s*(20\d{2}-\d{2}-\d{2})"
)
_SHORT_RANGE = re.compile(
    r"(?<![\d\-])(\d{2}-\d{2})\s*(?:\.\.|[~～]|至|到)\s*(\d{2}-\d{2})(?![\d\-])"
)
_CAMPAIGN_ID = re.compile(r"campaign_id\s*[=:]\s*'?(\d{6,})'?")
_CAMPAIGN_BARE = re.compile(r"campaign\s+(\d{6,})")
_KEYWORD_ID = re.compile(r"keyword_id\s*[=:]\s*'?(\d{6,})'?")
_NAME_LIKE = re.compile(r"campaign_name\s+like\s+'([^']+)'")
_RULE_ID = re.compile(r"rule_id\s*=\s*'?(R\d+)'?")
_ENTITY_ID = re.compile(r"entity_id\s*[=:]\s*'?(\d{6,})'?")
# 裸数字 ID：11 位以上，Amazon campaign_id 的实际长度；配合库里白名单校验用，
# 所以这里放宽到 8 位也不会误伤（日期、金额都过不了白名单）。
_BARE_ID = re.compile(r"(?<![\d.])(\d{8,})(?![\d.])")


def _parse_window(text: str, due: str) -> tuple[str | None, str] | None:
    """从 method 文本里抽断言窗口，抽不到就按「累计到到期日」补。

    为什么抽不到不能返回 None：None 会被下游当成「窗口无限/无需覆盖检查」，
    于是 `#50 到 11-22 累计 orders = 0` 这种没写区间的断言，会拿 11-01~11-17
    的局部数据当成完整窗口，看到 0 单就判 correct —— 而 11-18 到 11-22 还可能出单。
    这是最隐蔽的一类错判：数据越少越容易判对。

    所以默认窗口是 (None, verifiable_at)：起点交给数据覆盖起点，
    终点就是断言自己承诺的那个日子。返回的起点可能是 None，由 _clip 补。

    MM-DD 形式要补年份。补哪一年？用 verifiable_at 的年份：
    断言不可能引用一个比它到期日还晚的年份，而跨年的窗口
    （比如 12-28~01-04）在现有数据里不存在，不做处理比猜错好。
    """
    m = _FULL_RANGE.search(text)
    if m:
        return m.group(1), m.group(2)
    m = _SHORT_RANGE.search(text)
    if m:
        year = due[:4] if due else str(datetime.now(CST).year)
        try:
            return f"{year}-{m.group(1)}", f"{year}-{m.group(2)}"
        except Exception:
            pass
    if due:
        return None, due
    return None


def _parse_entities(conn: sqlite3.Connection, text: str) -> dict:
    """抽断言指向的对象。优先级由 _aggregate 决定，这里只负责尽量多地抽。

    光靠 `campaign_id=` / `campaign 123` 这两种带前缀的写法会漏掉一半断言：
    实测 #105 的 claim 是「237761921757002 在 11-02~11-15 的 $94.47 花费里…」，
    实体是裸数字，method 里只写「该活动」。漏抽的后果不是判错，而是模型
    拿不到该活动的位次报表，只能回「无法判定」—— 白白浪费一次调用。
    所以补一条裸数字扫描，但用库里真实 campaign_id 做白名单校验：
    不加校验的话，「11-02~11-15」里的数字、金额都会被当成 ID。
    """
    out: dict = {
        "campaign_ids": [],
        "keyword_id": None,
        "name_like": None,
        "portfolio": None,
        "rule_id": None,
        "entity_id": None,
    }
    for m in _CAMPAIGN_ID.finditer(text):
        out["campaign_ids"].append(m.group(1))
    for m in _CAMPAIGN_BARE.finditer(text):
        if m.group(1) not in out["campaign_ids"]:
            out["campaign_ids"].append(m.group(1))
    # 裸数字：只认库里确实存在的 campaign_id，其余一律不认。
    for m in _BARE_ID.finditer(text):
        cid = m.group(1)
        if cid in out["campaign_ids"]:
            continue
        hit = conn.execute(
            "select 1 from fact_campaign_daily where campaign_id=? limit 1", (cid,)
        ).fetchone()
        if hit:
            out["campaign_ids"].append(cid)
    m = _KEYWORD_ID.search(text)
    if m:
        out["keyword_id"] = m.group(1)
    m = _NAME_LIKE.search(text)
    if m:
        out["name_like"] = m.group(1)
    m = _RULE_ID.search(text)
    if m:
        out["rule_id"] = m.group(1)
    m = _ENTITY_ID.search(text)
    if m:
        out["entity_id"] = m.group(1)

    # 组合名：库里只有这几个真实取值，直接逐个找子串，
    # 比写一条「组合 X」的正则稳 —— 文本里「组合」「投放组合」「本组合」都有。
    for row in conn.execute(
        "select distinct portfolio_name from fact_campaign_daily where coalesce(portfolio_name,'') <> ''"
    ):
        name = row["portfolio_name"]
        if name and name in text:
            out["portfolio"] = name
            break
    return out


# ------------------------------------------------------------------ 聚合

def _clip(win: tuple[str | None, str] | None, cov: tuple[str | None, str | None]):
    """把断言窗口裁剪到实际数据覆盖范围。

    为什么裁而不是直接拒：
    裁完能算出「已覆盖部分」的值，这个值有两个用处 ——
    ① 单调谓词可以据此提前定论；② 写进 actual 让 unverifiable 有据可查。

    起点为 None 表示断言没写起点（「累计到 11-22」），此时用数据覆盖起点补。
    终点始终是断言承诺的日子，绝不因为数据不够就往前缩 —— 缩了就等于
    偷偷把断言改短，判出来的 correct 是假的。
    """
    cmax = cov[1]
    if win is None:
        return cov[0], cmax, None
    s, e = win
    if s is None:
        s = cov[0]
    return s, (min(e, cmax) if cmax else e), e


def _aggregate(conn: sqlite3.Connection, ents: dict, win, cov) -> dict | None:
    """按实体优先级取一个事实汇总。返回 None 表示实体解析不出来。"""
    s, e, _ = _clip(win, cov)
    if not s or not e or s > e:
        return None
    params = [s, e]
    if ents["keyword_id"]:
        sql = (
            "select coalesce(sum(cost),0) cost, coalesce(sum(clicks),0) clicks, "
            "coalesce(sum(ads_orders),0) orders, coalesce(sum(ads_sales),0) sales, "
            "coalesce(sum(impressions),0) impressions "
            "from fact_keyword_daily where keyword_id=? and report_date between ? and ?"
        )
        params = [ents["keyword_id"], s, e]
        label = f"keyword_id={ents['keyword_id']}"
    elif ents["name_like"]:
        sql = (
            "select coalesce(sum(cost),0) cost, coalesce(sum(clicks),0) clicks, "
            "coalesce(sum(ads_orders),0) orders, coalesce(sum(ads_sales),0) sales, "
            "coalesce(sum(impressions),0) impressions "
            "from fact_campaign_daily where campaign_name like ? and report_date between ? and ?"
        )
        params = [ents["name_like"], s, e]
        label = f"campaign_name like '{ents['name_like']}'"
    elif ents["campaign_ids"]:
        marks = ",".join("?" * len(ents["campaign_ids"]))
        sql = (
            "select coalesce(sum(cost),0) cost, coalesce(sum(clicks),0) clicks, "
            "coalesce(sum(ads_orders),0) orders, coalesce(sum(ads_sales),0) sales, "
            "coalesce(sum(impressions),0) impressions "
            f"from fact_campaign_daily where campaign_id in ({marks}) "
            "and report_date between ? and ?"
        )
        params = [*ents["campaign_ids"], s, e]
        label = "campaign_id=" + ",".join(ents["campaign_ids"])
    elif ents["portfolio"]:
        sql = (
            "select coalesce(sum(cost),0) cost, coalesce(sum(clicks),0) clicks, "
            "coalesce(sum(ads_orders),0) orders, coalesce(sum(ads_sales),0) sales, "
            "coalesce(sum(impressions),0) impressions "
            "from fact_campaign_daily where portfolio_name=? and report_date between ? and ?"
        )
        params = [ents["portfolio"], s, e]
        label = f"portfolio={ents['portfolio']}"
    else:
        return None

    row = conn.execute(sql, params).fetchone()
    if row is None:
        return None
    d = {k: float(row[k] or 0) for k in ("cost", "clicks", "orders", "sales", "impressions")}
    d["label"] = label
    d["window"] = (s, e)
    d["cpc"] = d["cost"] / d["clicks"] if d["clicks"] else 0.0
    d["cvr"] = d["orders"] / d["clicks"] if d["clicks"] else 0.0
    # ACOS 用 cost/sales 现算，绝不读 acos 列 ——
    # 库里无单时 campaign.acos=0.0、searchterm.acos=null，直接比必然判反。
    d["acos"] = (d["cost"] / d["sales"]) if d["sales"] > 0 else (float("inf") if d["cost"] > 0 else 0.0)
    return d


# ------------------------------------------------------------------ 谓词解析

_OP_MAP = {
    "≥": "ge", ">=": "ge", "=>": "ge",
    "≤": "le", "<=": "le", "=<": "le",
    ">": "gt", "<": "lt", "=": "eq", "==": "eq",
}
_METRIC_ALIAS = {
    "cost": "cost", "花费": "cost", "花费合计": "cost", "新增 cost": "cost",
    "sales": "sales", "销售额": "sales",
    "orders": "orders", "订单": "orders", "累计订单": "orders", "订单合计": "orders",
    "clicks": "clicks", "点击": "clicks", "累计点击": "clicks", "累计 clicks": "clicks",
    "impressions": "impressions", "曝光": "impressions",
    "cpc": "cpc", "CPC": "cpc",
    "cvr": "cvr", "CVR": "cvr",
    "acos": "acos", "ACOS": "acos",
}
_CLAUSE = re.compile(
    r"(新增\s*cost|新增\s*clicks|花费合计|花费|cost|销售额|sales|订单合计|累计订单|累计点击|累计\s*clicks|"
    r"orders|clicks|点击|曝光|impressions|CPC|cpc|CVR|cvr|ACOS|acos)"
    r"\s*(≥|≤|>=|<=|=>|=<|>|<|==|=)\s*\$?\s*([\d.]+)\s*(%?)"
)
_INTERVAL = re.compile(
    r"(累计\s*clicks|clicks|点击|cost|花费|orders|订单)\s*∈\s*\[\s*([\d.]+)\s*,\s*([\d.]+)\s*\]"
)

# 指标之间的算术式：`placement.clicks − campaign.clicks` / `orders / clicks`。
# 这类 expected 不能按单指标拆 —— 拆了就变成「拿 A 的绝对值去比 B 的阈值」。
# 实测两条都踩过：
#   #56 `placement.clicks − campaign.clicks ∈ [40, 120]` → 被读成 `clicks ∈ [40,120]`
#   #41 `orders / clicks < 0.010`                      → 被读成 `clicks < 0.01`
# 后者尤其危险：它把「转化率低于 1%」判成「点击数低于 0.01 击」，
# 而任何一个有数据的实体都会让这个条件为假，于是判出 wrong ——
# 一条本来该判 correct 的断言被静默判反，准确率被污染且事后无法分辨。
# docstring 里早写了「宁可返回空列表让模型接手」，只是正则没覆盖这种写法。
_ARITH = re.compile(
    r"(?:clicks|cost|orders|sales|impressions|点击|花费|订单|销售额)"
    r"\s*(?:[−\-–—]|/|÷)\s*"
    r"(?:[a-z_]+\.)?(?:clicks|cost|orders|sales|impressions|点击|花费|订单|销售额)",
    re.I,
)


def _cmp(value: float, op: str, target: float) -> bool:
    if op == "ge":
        return value >= target
    if op == "le":
        return value <= target
    if op == "gt":
        return value > target
    if op == "lt":
        return value < target
    return abs(value - target) < 1e-9


def _parse_clauses(expected: str) -> list[tuple[str, str, float]]:
    """把 expected 拆成若干 (metric, op, threshold) 合取项。

    支持两种写法：`cost ≥ 90 且 orders ≤ 2`、`clicks ∈ [180, 330] 且 orders ≤ 1`。
    拆不干净（比如 `≥5 条含 '155%'，其中改写为「1.55 倍」的为 0 条`）时
    宁可返回空列表让模型接手，也不要拼出一个半对的判定 ——
    半对的判定比不判更糟，它会静默污染准确率。
    """
    # 指标之间的算术式一律不拆。见 _ARITH 的注释：#41 的
    # `orders / clicks < 0.010` 会被读成 `clicks < 0.01` 并静默判 wrong。
    # 交给模型，它至少能看懂「转化率」。
    if _ARITH.search(expected):
        return []
    out: list[tuple[str, str, float]] = []
    for m in _INTERVAL.finditer(expected):
        metric = _METRIC_ALIAS.get(re.sub(r"\s+", " ", m.group(1)).strip())
        if not metric:
            return []
        out.append((metric, "ge", float(m.group(2))))
        out.append((metric, "le", float(m.group(3))))
    for m in _CLAUSE.finditer(expected):
        raw = re.sub(r"\s+", " ", m.group(1)).strip()
        metric = _METRIC_ALIAS.get(raw)
        if not metric:
            return []
        op = _OP_MAP.get(m.group(2))
        if not op:
            return []
        val = float(m.group(3))
        if m.group(4) == "%":
            val = val / 100.0
        # CVR 写成 0.010 或 1% 两种都见过，统一成小数
        if metric == "cvr" and val > 1.0:
            val = val / 100.0
        out.append((metric, op, val))
    return out


# ------------------------------------------------------------------ 判定器族
#
# 每个判定器返回 (verdict, actual) 或 None（表示「不是我这族」）。
# 顺序有意义：先跑最具体、最不可能误判的，跑不出结果才轮到下一个。
# 全都返回 None 时，才降级到模型。

# 单调递增的加法量：这些指标在一个窗口内只会变大，不会变小。
# 单调性是本文件唯一允许「窗口未走完就下结论」的依据，因为它可证不可反转：
#   orders 已经是 1 → 到窗口末尾不可能退回 0，`orders = 0` 当场判 wrong；
#   cost 已经是 130 → 到窗口末尾不会退回 120 以下，`cost ≥ 120` 当场判 correct。
# 比值（CPC/CVR/ACOS/占比）没有这个性质 —— 分子分母都在涨，
# 现在 1.8 的 CPC 完全可能在窗口末尾摊到 1.25。所以比值永远等窗口走完。
_MONOTONE = {"cost", "clicks", "orders", "sales", "impressions"}


def _gate(state: str, win, cov, clauses, agg) -> tuple[str, str] | None:
    """窗口覆盖不足时的统一裁决。

    这是整个文件里最要紧的一个函数。它挡住的错误是：
    「数据只到 11-17，断言说 11-29 前 ACOS ≥ 0.70，看到 11-16~11-17 只有 2 天
      且 ACOS 无穷大 → 判 correct」。
    两周里 CVR 翻三倍是极小概率，但不是零；一旦发生，这条 correct 就是错的，
    而 agent 的准确率会因此虚高。宁可这轮不判。

    反过来也一样要紧：`orders = 0` 在数据只到 11-17、已经看到 1 单时，
    不需要等 11-22 —— 订单不会退回去，当场就能判 wrong。
    这两种情形必须分开，否则要么虚高要么虚低。

    判据是「越过阈值后结论是否已锁死」：
      ge/gt 越过 → 只会更大，永久真；eq/le/lt 越过 → 回不去了，永久假。
    两种情况都能定论，所以只要全部合取项都越过阈值就放行。

    覆盖不足时返回 ('pending', 原因)，**不是 unverifiable**：
    unverifiable 是永久结论（落库即钉死，幂等 UPDATE 不再改），
    而「数据没追平」是临时状态 —— 明天 ingest 跑完就该重判。
    把临时状态写成永久结论，会让兑现率被系统性压低一天，
    且事后无法从库里的数据分辨「真判不出」和「那天数据还没到」。
    """
    if state == "covered":
        return None
    if state == "future":
        return ("pending", f"断言窗口 {_win_text(win)} 尚无任何事实数据（数据止于 {cov[1]}），本轮不判")
    if clauses and all(metric in _MONOTONE for metric, _, _ in clauses):
        crossed_all = True
        for metric, op, thr in clauses:
            val = agg.get(metric, 0.0)
            crossed = (val >= thr) if op == "ge" else (val > thr)
            if not crossed:
                crossed_all = False  # 有一项没越过，整条合取就还不能定
                break
        if crossed_all:
            return None
    return (
        "pending",
        f"断言窗口 {_win_text(win)} 未走完（数据止于 {cov[1]}），"
        f"已覆盖部分不足以定论，本轮不判，等 ingest 追平后自动重判",
    )


def _ev_cpc(conn, row, exp, win, ents, cov, state):
    """CPC 阈值：`CPC ≥ 1.30` / `日均 CPC ≥ $1.20` / `区间 CPC ≥ 1.20`。"""
    if "CPC" not in exp and "cpc" not in exp:
        return None
    clauses = _parse_clauses(exp)
    clauses = [c for c in clauses if c[0] == "cpc"]
    if not clauses:
        return None
    agg = _aggregate(conn, ents, win, cov)
    if agg is None:
        return None
    blocked = _gate(state, win, cov, None, agg)  # CPC 是比值，不单调
    if blocked:
        return blocked
    ok = all(_cmp(agg["cpc"], op, thr) for _, op, thr in clauses)
    return (
        "correct" if ok else "wrong",
        f"{agg['label']} {agg['window'][0]}~{agg['window'][1]} "
        f"cost={agg['cost']:.2f} clicks={agg['clicks']:.0f} → CPC={agg['cpc']:.4f}",
    )


def _ev_acos(conn, row, exp, win, ents, cov, state):
    """ACOS 阈值。注意 sales=0 时 ACOS 视为 +∞，不是 0。"""
    if "ACOS" not in exp and "acos" not in exp:
        return None
    clauses = [c for c in _parse_clauses(exp) if c[0] == "acos"]
    if not clauses:
        return None
    agg = _aggregate(conn, ents, win, cov)
    if agg is None:
        return None
    blocked = _gate(state, win, cov, None, agg)
    if blocked:
        return blocked
    ok = all(_cmp(agg["acos"], op, thr) for _, op, thr in clauses)
    shown = "∞(无销售额)" if agg["acos"] == float("inf") else f"{agg['acos']:.4f}"
    return (
        "correct" if ok else "wrong",
        f"{agg['label']} {agg['window'][0]}~{agg['window'][1]} "
        f"cost={agg['cost']:.2f} sales={agg['sales']:.2f} → ACOS={shown}",
    )


def _ev_cvr(conn, row, exp, win, ents, cov, state):
    """CVR 阈值。同样不单调：分母 clicks 会涨。"""
    if "CVR" not in exp and "cvr" not in exp:
        return None
    clauses = [c for c in _parse_clauses(exp) if c[0] == "cvr"]
    if not clauses:
        return None
    agg = _aggregate(conn, ents, win, cov)
    if agg is None:
        return None
    blocked = _gate(state, win, cov, None, agg)
    if blocked:
        return blocked
    ok = all(_cmp(agg["cvr"], op, thr) for _, op, thr in clauses)
    return (
        "correct" if ok else "wrong",
        f"{agg['label']} {agg['window'][0]}~{agg['window'][1]} "
        f"clicks={agg['clicks']:.0f} orders={agg['orders']:.0f} → CVR={agg['cvr']:.4f}",
    )


def _ev_numeric(conn, row, exp, win, ents, cov, state):
    """通用数值合取：`cost ≥ 90 且 orders ≤ 2` / `累计 clicks < 509` / `orders = 0`。

    这一族覆盖了断言库里最多的一类 —— 都是「某实体在某个窗口的加法量 vs 阈值」。
    """
    clauses = _parse_clauses(exp)
    if not clauses:
        return None
    if any(c[0] in ("cpc", "cvr", "acos") for c in clauses):
        return None  # 比值交给各自那一族，避免两族都判同一条
    agg = _aggregate(conn, ents, win, cov)
    if agg is None:
        return None
    blocked = _gate(state, win, cov, clauses, agg)
    if blocked:
        return blocked
    ok = all(_cmp(agg[metric], op, thr) for metric, op, thr in clauses)
    detail = " ".join(f"{m}{_op_symbol(o)}{t:g}" for m, o, t in clauses)
    return (
        "correct" if ok else "wrong",
        f"{agg['label']} {agg['window'][0]}~{agg['window'][1]} "
        f"cost={agg['cost']:.2f} clicks={agg['clicks']:.0f} "
        f"orders={agg['orders']:.0f} sales={agg['sales']:.2f} "
        f"(判定 {detail})",
    )


def _op_symbol(op: str) -> str:
    return {"ge": "≥", "le": "≤", "gt": ">", "lt": "<", "eq": "="}.get(op, op)


_PLACEMENT_ALIAS = {
    "detail page": "Detail Page on-Amazon",
    "详情页": "Detail Page on-Amazon",
    "tos": "Top of Search on-Amazon",
    "top of search": "Top of Search on-Amazon",
    "搜索顶部": "Top of Search on-Amazon",
}

# 位次占比的阈值：`TOS 花费占比 ≥ 0.70` / `TOS 花费占比不低于 70%`。
# 不能交给 _parse_clauses —— 它要求「指标 紧跟 运算符」，而占比句里
# 「占比」两个字夹在中间（`花费` + `占比` + `≥`），_CLAUSE 匹配不上。
# 实测后果：_ev_placement_share 的 docstring 举的两个例子
# （`TOS 花费占比 ≥ 0.70`、`Detail Page on-Amazon 花费占比 ≥ 0.60`）
# 全都解析成空列表 → 函数直接 return None → 这个判定器从来没被触发过。
# #105（TOS 花费占比 ≥ 0.70）当时是被模型判的 correct，不是确定性判定。
# 这条正则单独认「占比」后面那个比较，并把「不低于/不高于」也算运算符。
_SHARE_CLAUSE = re.compile(
    r"占比\s*(?:不低于|不高于|不小于|不大于|≥|≤|>=|<=|=>|=<|>|<|==|=)\s*\$?\s*([\d.]+)\s*(%?)"
)


def _ev_placement_share(conn, row, exp, win, ents, cov, state):
    """位次花费占比：`Detail Page on-Amazon 花费占比 ≥ 0.60` / `TOS 花费占比 ≥ 0.70`。

    只在能定位到单个/单组 campaign 时才算 —— 位次占比是「同一 campaign 内」的概念，
    跨 campaign 求和再算占比没有业务含义（不同 campaign 的位次策略本来就不同）。
    """
    if "占比" not in exp:
        return None
    target = None
    low = exp.lower()
    for alias, placement in _PLACEMENT_ALIAS.items():
        if alias in low:
            target = placement
            break
    if target is None:
        return None
    clauses = [c for c in _parse_clauses(exp) if c[0] in ("cost", "clicks")]
    if not clauses:
        # `TOS 花费占比 ≥ 0.70` 这种写法 _parse_clauses 解析不出来（「占比」
        # 夹在指标与运算符之间），但它是位次占比最常见的一句 —— 单独认。
        m = _SHARE_CLAUSE.search(exp)
        if not m:
            return None
        val = float(m.group(1))
        if m.group(2) == "%":
            val = val / 100.0
        raw_op = re.search(r"占比\s*(不低于|不高于|不小于|不大于|≥|≤|>=|<=|=>|=<|>|<|==|=)",
                           exp)
        op = {"不低于": "ge", "不小于": "ge", "不高于": "le", "不大于": "le"}.get(
            raw_op.group(1) if raw_op else "", None) or _OP_MAP.get(
            raw_op.group(1) if raw_op else "")
        if not op:
            return None
        clauses = [("cost", op, val)]
    if not ents["campaign_ids"]:
        return None
    s, e, _ = _clip(win, cov)
    if not s or not e or s > e:
        return None
    marks = ",".join("?" * len(ents["campaign_ids"]))
    rows = conn.execute(
        f"select placement, coalesce(sum(cost),0) cost, coalesce(sum(clicks),0) clicks "
        f"from fact_placement_daily where campaign_id in ({marks}) "
        f"and report_date between ? and ? group by placement",
        [*ents["campaign_ids"], s, e],
    ).fetchall()
    if not rows:
        return None
    metric = clauses[0][0]
    total = sum(float(r[metric] or 0) for r in rows)
    if total <= 0:
        return ("unverifiable", f"{s}~{e} 该活动位次报告合计 {metric}=0，占比无定义")
    part = sum(float(r[metric] or 0) for r in rows if r["placement"] == target)
    share = part / total
    blocked = _gate(state, win, cov, None, {})
    if blocked:
        return blocked
    ok = all(_cmp(share, op, thr) for _, op, thr in clauses)
    return (
        "correct" if ok else "wrong",
        f"campaign {','.join(ents['campaign_ids'])} {s}~{e} "
        f"{target} {metric}={part:.2f} / 合计={total:.2f} → 占比={share:.4f}",
    )


_BINOM_PARAM = re.compile(
    r"clicks\s*=\s*(\d+).*?orders\s*=\s*(\d+).*?p0\s*=\s*([\d.]+)", re.S
)


def _ev_binom(conn, row, exp, win, ents, cov, state):
    """二项检验 p 值：`p < 0.05` / `p ≥ 0.05` / `p > 0.05`。

    两种情况：
      a) method 自带 clicks/orders/p0（如 eval 31「clicks=308 / orders=2 / p0=0.01766」）
         → 纯算术，完全不碰库，也不可能判错；
      b) 从表里按实体聚合出 clicks/orders，p0 取 method 里写死的账户 CVR，
         或取 memorial 最新 pack 的 account.cvr。
    双侧 p 用 facts._binom_cdf 现算，与 R03 用的是同一个函数 ——
    断言原话就是「用 R03 同一个二项检验函数」，用别的实现等于换了尺子。
    """
    if facts is None:
        return None
    if not re.search(r"\bp\s*[<≥>]", exp) and "p 值" not in exp and "p_value" not in exp:
        return None
    clauses = [c for c in _parse_clauses(exp) if c[0] not in ("cost", "clicks", "orders", "sales")]
    if not clauses:
        return None
    m = _BINOM_PARAM.search(row["method"])
    if m:
        n, k, p0 = int(m.group(1)), int(m.group(2)), float(m.group(3))
        label = "method 内联参数"
    else:
        agg = _aggregate(conn, ents, win, cov)
        if agg is None:
            return None
        p0 = _account_cvr(conn)
        if not p0:
            return None
        blocked = _gate(state, win, cov, None, agg)
        if blocked:
            return blocked
        n, k = int(agg["clicks"]), int(agg["orders"])
        label = f"{agg['label']} {agg['window'][0]}~{agg['window'][1]}"
    if n <= 0:
        return ("unverifiable", f"{label} clicks=0，二项检验无定义")
    p_val = min(1.0, 2.0 * facts._binom_cdf(k, n, p0))
    ok = all(_cmp(p_val, op, thr) for _, op, thr in clauses)
    return (
        "correct" if ok else "wrong",
        f"{label} clicks={n} orders={k} p0={p0} → 双侧 p={p_val:.4f}",
    )


def _account_cvr(conn: sqlite3.Connection) -> float | None:
    """账户基线 CVR，取 memorial 最新一包的 account.cvr。

    为什么不自己从 fact 表算：
    断言里的 p0（1.78% / 1.7428% / 1.74%）都是诊断当时从 memorial 读的，
    自己重算会得到略微不同的数（数据又进了一天），
    那样就不是「检验它当时说的话对不对」，而是「检验一个它没说过的话」。
    """
    row = conn.execute(
        "select json_extract(pack_json,'$.account.cvr') as cvr "
        "from memorial order by run_id desc limit 1"
    ).fetchone()
    if row and row["cvr"]:
        try:
            return float(row["cvr"])
        except Exception:
            return None
    return None


def _ev_decision_log(conn, row, exp, win, ents, cov, state):
    """decision_log 计数类断言。

    覆盖两种能确定解析的形态：
      a) rule_id + 窗口 + 「≥N 条新增 / checked_at 全为空 / action_type 全为 alert」
      b) entity_id + checked_at 窗口 + 「已执行记录 = 0 条」

    为什么这两类特别值得自动判：
    R30 占 decision_log 的 45/125，而它的核心特征就是「写了建议但 checked_at 全空」——
    这恰恰是诊断 agent 最需要被检验的行为（它是不是只会发告警不闭环）。
    这类断言措辞再花，落到 SQL 上就一个 count，不该花 token 问模型。
    """
    if "decision_log" not in row["method"]:
        return None

    # b) entity_id + checked_at
    if ents["entity_id"] and "checked_at" in row["method"] and win:
        cnt = conn.execute(
            "select count(*) n from decision_log where entity_id=? "
            "and checked_at is not null and substr(checked_at,1,10) between ? and ?",
            (ents["entity_id"], win[0], win[1]),
        ).fetchone()["n"]
        if "0 条" in exp:
            # 「已执行记录 = 0 条」只在窗口走完后才能判对：
            # 计数单调递增，已经数到 >=1 条就是永久 wrong（订单/记录不会消失），
            # 但数到 0 条只说明「到现在还没执行」，窗口里剩下的日子随时会冒出第 1 条。
            if cnt > 0:
                return (
                    "wrong",
                    f"decision_log entity_id={ents['entity_id']} "
                    f"checked_at∈{_win_text(win)} 已执行 {cnt} 条（≥1，结论已锁死）",
                )
            if state != "covered":
                return (
                    "pending",
                    f"断言窗口 {_win_text(win)} 未走完（数据止于 {cov[1]}），"
                    f"当前已执行 0 条但窗口内仍可能新增，本轮不判",
                )
            return (
                "correct",
                f"decision_log entity_id={ents['entity_id']} "
                f"checked_at∈{_win_text(win)} 已执行 0 条（窗口已走完）",
            )

    # a) rule_id + 窗口
    if not ents["rule_id"] or not win:
        return None
    s, e, _ = _clip(win, cov)
    if not s or not e:
        return None
    rows = conn.execute(
        "select action_type, spend_impact, checked_at from decision_log "
        "where rule_id=? and substr(decided_at,1,10) between ? and ?",
        (ents["rule_id"], s, e),
    ).fetchall()
    n = len(rows)
    n_checked = sum(1 for r in rows if r["checked_at"])
    n_spend = sum(1 for r in rows if (r["spend_impact"] or 0) > 0)
    n_alert = sum(1 for r in rows if r["action_type"] == "alert")

    verdicts: list[bool] = []
    notes: list[str] = []
    m = re.search(r"(≥|<=|≥)\s*(\d+)\s*条", exp)
    if m:
        verdicts.append(n >= int(m.group(2)))
        notes.append(f"条数 {n}")
    if "checked_at 全为空" in exp or "checked_at 全部为空" in exp:
        verdicts.append(n_checked == 0)
        notes.append(f"checked_at 非空 {n_checked} 条")
    if "checked_at 非空" in exp and "= 0" in exp:
        verdicts.append(n_checked == 0)
        notes.append(f"checked_at 非空 {n_checked} 条")
    if "spend_impact 全为 0" in exp or "spend_impact>0 的条目为 0" in exp:
        verdicts.append(n_spend == 0)
        notes.append(f"spend_impact>0 {n_spend} 条")
    if "action_type 全部为 alert" in exp or "action_type='alert'" in exp:
        verdicts.append(n > 0 and n_alert == n)
        notes.append(f"alert {n_alert}/{n} 条")

    if not verdicts:
        return None
    # 窗口没走完且结论依赖「未来还会新增多少条」→ 这轮不判（pending，下一轮重判）
    if state != "covered":
        return (
            "pending",
            f"断言窗口 {_win_text(win)} 未走完（数据止于 {cov[1]}），"
            f"decision_log 计数仍会变化；当前 {'; '.join(notes)}",
        )
    return (
        "correct" if all(verdicts) else "wrong",
        f"decision_log rule_id={ents['rule_id']} {s}~{e}: " + "; ".join(notes),
    )


def _next_run_pack(conn, row, cov) -> dict:
    """「下一轮 run」型断言的事实来源。

    这类断言（实测 9 条：`#56`/`#15`/`#91`/`#92`/`#129`/`#136`/`#139`/`#124`/`#130`）
    说的是「**下一次引擎跑出来的东西**还会不会长这样」，而不是「某段日期内的广告数据是多少」。
    它们的 expected 里根本没有可聚合的指标（`placement.clicks − campaign.clicks ∈ [40,120]`、
    `front=0 且 observing≥4`、`仍包含该段落`），所以：

      · `_parse_clauses` 拆不出来 → 七个确定性判定器全部无解；
      · `_fact_pack` 只给账户/组合/活动/位次/决策流的聚合量 → 模型也没法判，
        只能回 unverifiable。

    而 unverifiable 一旦落库就被幂等 UPDATE 钉死（`#56` 就是这么被钉的）。
    所以必须单独把「断言之后的第一份 memorial」喂进去 —— 它才是这类断言的
    唯一正确事实来源。

    返回空 dict 表示「还没有更晚的 run」，调用方据此保持 pending 而不是判 unverifiable。
    """
    # 关键：这里要比的是 agent_run.run_id（它绑定的那次引擎 run），
    # 不是 agent_run_id —— 两个 id 属于不同命名空间，混用会查到风马牛不相及的 memorial。
    # 实测 #56 的 agent_run_id=17，但它绑的是 memorial run 25；
    # 拿 17 去查 `run_id > 17` 会捞到 run 18（2026-11-14），比断言本身还早。
    bound = conn.execute("select run_id from agent_run where agent_run_id = ?",
                         (row["agent_run_id"],)).fetchone()
    if not bound or bound["run_id"] is None:
        return {}
    later = conn.execute(
        "select run_id, business_day, pack_json from memorial where run_id > ? "
        "order by run_id limit 1", (bound["run_id"],)).fetchone()
    if not later:
        return {}
    try:
        pk = json.loads(later["pack_json"] or "{}")
    except Exception:
        return {}
    out: dict = {
        "说明": "下面是断言到期后引擎实际跑出的那一版结果（memorial 原文，未加工）。",
        "memorial_run_id": later["run_id"],
        "business_day": later["business_day"],
    }
    for key in ("window_start", "window_end", "front", "observing", "overflow",
                "below", "questions", "conflicts", "alerts", "suppressed",
                "layer_evidence", "cpc_bias", "target_reachable", "saved"):
        if key in pk:
            out[key] = pk[key]
    # 计数类断言（`front=0 且 observing≥4`）最常踩的坑是拿到列表还得自己数，
    # 顺手把长度摆出来，省掉模型一次算术。
    for key in ("front", "observing", "overflow", "below", "questions",
                "conflicts", "alerts", "suppressed", "layer_evidence"):
        if isinstance(out.get(key), list):
            out[f"{key}_条数"] = len(out[key])
    return out


# 「重拉补数」型断言：同一个 report_date 的两次读数之比。
#
# 实测两条：#74「T+3 clicks ≥ 1.2 × T+1 clicks」、
# #77「后一次取值 ≥ 前一次 × 1.3（若两次相等则本条判错）」。
# 这类断言以前一律 unverifiable，因为 fact_*_daily 是 upsert 覆盖，
# 重拉一次就把上一次读数抹了 —— 库里只剩最后一次，没有「两次」可比。
# 2026-11-20 起有 fact_read_snapshot 专门记这件事（只在读数变化时追加一行）。
_REREAD_T = re.compile(
    r"T\+(\d+)\s*clicks?\s*(?:≥|>=|>)\s*([\d.]+)\s*[×x*]\s*T\+(\d+)\s*clicks?",
    re.I,
)
_REREAD_SEQ = re.compile(
    r"后一次[^。；;]{0,12}?(?:≥|>=|>)\s*前一次\s*[×x*]\s*([\d.]+)", re.I
)
# claim 里的「11-16」：月-日，年份从 verifiable_at 推。
_MD = re.compile(r"(?<!\d)(\d{1,2})-(\d{1,2})(?!\d)")


def _reread_day(row) -> str | None:
    """从 claim/method 里认出「哪一天被重读」。认不出返回 None。"""
    due = (row["verifiable_at"] or "")[:4]
    if not due.isdigit():
        return None
    for text in (row["claim"] or "", row["method"] or ""):
        for m in _MD.finditer(text):
            mo, dd = int(m.group(1)), int(m.group(2))
            if 1 <= mo <= 12 and 1 <= dd <= 31:
                # 「11-16」在 claim 开头，是月-日；「1.2」「0.010」这类不会被
                # _MD 匹配到（它们用点号），所以这里不用额外排除。
                return f"{due}-{mo:02d}-{dd:02d}"
    return None


def _ev_reread(conn, row, exp, win, ents, cov, state):
    """判「重拉有没有把数补上来」。

    与 _ev_next_run 的区别：它要的是**同一个 report_date 的两次读数**，
    证据在 fact_read_snapshot，不在 memorial。

    三条保持 pending 的情形（都不能判 unverifiable —— 那会被幂等 UPDATE 钉死）：
      · 断言指向的那天在快照表里一行都没有
      · 只有一次读数（还没重拉过）
      · 多次读数的 observed_at 全在同一天（那是一次写入的重复，不是跨日重读）
    """
    m = _REREAD_T.search(exp)
    seq = None
    if not m:
        seq = _REREAD_SEQ.search(exp)
    if not m and not seq:
        return None
    # 断言带具体实体时本判定器不接 —— 快照表按天汇总，没有活动/关键词粒度，
    # 拿账户合计去答「某个活动的点击数」会答错。
    if ents.get("campaign_ids") or ents.get("keyword_ids"):
        return None
    day = _reread_day(row)
    if not day:
        return None
    rows = conn.execute(
        "select observed_at, clicks, note, run_id from fact_read_snapshot "
        "where dataset='campaign' and report_date=? order by observed_at, snapshot_id",
        (day,),
    ).fetchall()
    if not rows:
        return (
            "pending",
            f"{day} 在读数快照里没有任何一行。快照从 2026-11-20 才开始记，"
            f"这天之前的重拉读数已经随 upsert 覆盖丢失，无法还原两次读数之比",
        )
    if len(rows) == 1:
        return (
            "pending",
            f"{day} 目前只有 1 次读数（{rows[0]['observed_at']}，clicks="
            f"{rows[0]['clicks']}），还没发生重拉；等下一次 ingest 重拉这天再判",
        )
    days = {str(r["observed_at"])[:10] for r in rows}
    # 同一天多次读数只对 T+n 型断言是无效证据 —— 它承诺的是「第 1 天读一次、
    # 第 3 天再读一次」，两次都发生在同一天说明重拉还没真正跨日。
    # 但对 #77 那种「后一次 ≥ 前一次 × 1.3」的写法，断言本身没承诺跨天，
    # 同一天内先读到 400、下午重拉读到 520 就是有效的两次读数，不该挡。
    if len(days) == 1 and m:
        return (
            "pending",
            f"{day} 有 {len(rows)} 行读数但 observed_at 全在 {next(iter(days))}，"
            f"是同一天内的重读而非跨日重拉，T+n 型断言不能拿它比",
        )
    first, last = rows[0], rows[-1]
    a = float(first["clicks"] or 0)
    b = float(last["clicks"] or 0)
    if a <= 0:
        return (
            "unverifiable",
            f"{day} 首次读数 clicks=0，比值无定义（{first['observed_at']} → {last['observed_at']}）",
        )
    ratio = b / a
    if m:
        mult = float(m.group(2))
        want_ge = True
        label = f"后一次 ≥ {mult:g} × 前一次"
    else:
        mult = float(seq.group(1))
        want_ge = True
        label = f"后一次 ≥ {mult:g} × 前一次"
    ok = ratio >= mult if want_ge else ratio <= mult
    trace = " → ".join(f"{r['observed_at'][:10]} {r['clicks']}" for r in rows[:6])
    return (
        "correct" if ok else "wrong",
        f"{day} clicks 读数：{trace}；比值 {ratio:.4f}，要求 {label}"
        f" → {'成立' if ok else '不成立'}",
    )


# 「下一轮 run」型断言的两种可机械判的形式。其余写法（`仍包含该段落`、
# `note 含「未低于 20%」`）只能交模型，但至少现在模型手里有 memorial 原文了。
_NEXT_LAYER_DIFF = re.compile(
    r"([a-z_]+)\.clicks\s*(?:[−\-–—])\s*([a-z_]+)\.clicks\s*∈\s*\[\s*([\d.]+)\s*,\s*([\d.]+)\s*\]",
    re.I,
)
_DATE_RANGE = re.compile(r"20\d{2}-\d{2}-\d{2}\s*[~\-–—至]\s*20\d{2}-\d{2}-\d{2}")
_NEXT_COUNT = re.compile(
    r"(front|observing|overflow|below|suppressed|alerts|conflicts|questions)"
    r"\s*(?:=|==|≥|≤|>=|<=|>|<)\s*(\d+)"
)


def _ev_next_run(conn, row, exp, win, ents, cov, state):
    """判「下一次引擎跑出来的东西还会不会长这样」。

    与其它判定器的根本区别：它不看日期窗口，看的是**断言之后第一次引擎运行**。
    所以 `_gate` 那套「窗口没走完就 defer」在这里不适用 —— 只要更晚的 memorial
    已经存在，事实就是齐的；不存在就保持 pending（不能判 unverifiable，
    那会被幂等 UPDATE 钉死，`#56` 就是这么被钉的）。

    只认两种能机械判的写法，其余 return None 交给模型：
      · `placement.clicks − campaign.clicks ∈ [40, 120]`（层间差）
      · `front=0 且 observing≥4`（memorial 各桶条数）
    """
    # method 里写了明确日期区间的，不是「下一轮」型 —— 它承诺的是那段时间内的数据。
    # 实测 #15：「查 decision_log 在 2026-11-15~2026-11-21 内 entity 含 cable machine
    # 的决策条数」，expected 却是「0 条决策，suppressed 中同名记录 ≥2 条」。
    # 按 expected 的字面它像 next_run（要数 suppressed 条数），按 method 它是日期型。
    # 以 method 为准：日期型断言该由 _window_state 决定什么时候判，
    # 抢过来按「下一份 memorial」判会把窗口外的运行也算进去。
    if _DATE_RANGE.search(row["method"] or ""):
        return None
    m = _NEXT_LAYER_DIFF.search(exp)
    cnt = _NEXT_COUNT.findall(exp)
    if not m and not cnt:
        return None
    nxt = _next_run_pack(conn, row, cov)
    if not nxt:
        return (
            "pending",
            "断言之后还没有更新的引擎运行（memorial 里没有 run_id 更大的行），"
            "这类断言只能等下一轮跑完再判；本轮不写库，避免被幂等 UPDATE 钉成 unverifiable",
        )
    rid = nxt.get("memorial_run_id")
    if m:
        left, right = m.group(1).lower(), m.group(2).lower()
        lo, hi = float(m.group(3)), float(m.group(4))
        lay = {str(x.get("layer") or "").lower(): x for x in (nxt.get("layer_evidence") or [])}
        if left not in lay or right not in lay:
            return (
                "unverifiable",
                f"memorial run {rid} 的 layer_evidence 里没有 {left}/{right} 层"
                f"（实际只有 {sorted(lay)}），这版 pack 早于分层证据上线，无法核算差值",
            )
        a = int(lay[left].get("clicks") or 0)
        b = int(lay[right].get("clicks") or 0)
        diff = a - b
        ok = lo <= diff <= hi
        return (
            "correct" if ok else "wrong",
            f"memorial run {rid}（{nxt.get('business_day')}）："
            f"{left}.clicks={a} − {right}.clicks={b} = {diff}，"
            f"要求落在 [{lo:.0f}, {hi:.0f}] → {'成立' if ok else '不成立'}",
        )
    # 条数型：逐个条件都要满足
    notes: list[str] = []
    all_ok = True
    for field, want in cnt:
        got = nxt.get(f"{field}_条数")
        if got is None:
            return (
                "unverifiable",
                f"memorial run {rid} 的 pack 里没有 {field} 这一项（这版 pack 还没带该字段）",
            )
        if got != int(want):
            all_ok = False
        notes.append(f"{field}={got}（要求 {want}）")
    return (
        "correct" if all_ok else "wrong",
        f"memorial run {rid}（{nxt.get('business_day')}）：" + "；".join(notes),
    )


_DET = [
    ("next_run", _ev_next_run),
    ("reread", _ev_reread),
    ("binom", _ev_binom),
    ("placement_share", _ev_placement_share),
    ("decision_log", _ev_decision_log),
    ("acos", _ev_acos),
    ("cvr", _ev_cvr),
    ("cpc", _ev_cpc),
    ("numeric", _ev_numeric),
]


def deterministic_verdict(conn, row, cov) -> tuple[str, str, str] | None:
    """跑一遍确定性判定器。返回 (verdict, actual, evaluator) 或 None。"""
    exp = row["expected"] or ""
    win = _parse_window(row["method"] or "", row["verifiable_at"] or "")
    ents = _parse_entities(conn, f"{row['method'] or ''} {row['claim'] or ''}")
    state = _window_state(win, cov)
    for name, fn in _DET:
        try:
            out = fn(conn, row, exp, win, ents, cov, state)
        except Exception as exc:  # 单个判定器炸了不该拖垮整轮
            print(f"  ! 判定器 {name} 在 eval #{row['eval_id']} 抛错：{exc}")
            continue
        if out:
            return out[0], out[1], name
    return None


def _win_text(win) -> str:
    """把窗口渲染成人能读的样子。起点为 None 表示「从数据起点开始累计」。"""
    if win is None:
        return "（未解析）"
    s, e = win
    return f"{s or '起始'}~{e}"


def _window_state(win, cov) -> str:
    """判断断言窗口相对数据覆盖的状态。

    'none' 这个返回值已经取消：窗口永远至少是 (覆盖起点, verifiable_at)，
    因为「没写窗口」不等于「不需要等」—— 断言承诺的是到期日那天的事实，
    在到期日之前它随时可能被推翻。把 none 当成 covered 是数据越少越判对的根源。
    """
    if win is None:
        return "future"
    cmax = cov[1]
    if not cmax:
        return "future"
    if win[1] <= cmax:
        return "covered"
    if win[0] and win[0] > cmax:
        return "future"
    return "partial"


# ------------------------------------------------------------------ 模型兜底

def _fact_pack(conn, row, cov, win, ents) -> dict:
    """给模型准备「到期日之后的事实」。

    这里刻意只给聚合量、不给原始行：原始行会瞬间把 prompt 顶到几万 token，
    而模型要回答的是「这个结论对不对」，不是「帮我把这份报表读完」。
    给的维度覆盖它可能需要的全部：账户、组合、活动、位次、决策流。
    """
    pack: dict = {
        "断言": {
            "eval_id": row["eval_id"],
            "claim": row["claim"],
            "expected": row["expected"],
            "method": row["method"],
            "verifiable_at": row["verifiable_at"],
        },
        "数据覆盖": {"起": cov[0], "止": cov[1]},
        "断言窗口": list(win) if win else None,
    }
    # 「下一轮 run」型断言只能靠这一节判 —— 见 _next_run_pack 的说明。
    nxt = _next_run_pack(conn, row, cov)
    if nxt:
        pack["断言之后的第一次运行"] = nxt
    # 重读轨迹：断言里提到「重拉」「两次读数」「T+n」时，把 fact_read_snapshot
    # 里那几天的读数序列摆给模型看。_ev_reread 只认两种能机械判的写法，
    # 其余（「比本窗口内记录的值高至少 30%」这种不带倍数的）得靠模型读轨迹。
    _day = _reread_day(row)
    if _day:
        _reads = conn.execute(
            "select observed_at, clicks, impressions, cost, ads_orders, note, run_id "
            "from fact_read_snapshot where dataset='campaign' and report_date=? "
            "order by observed_at, snapshot_id", (_day,)).fetchall()
        if _reads:
            pack["这一天的读数轨迹"] = {
                "report_date": _day,
                "读数次数": len(_reads),
                "说明": "fact_read_snapshot 只在读数变化时追加一行；"
                        "「重拉过但数没变」要看 run_log 同一天有几次 status=ok 的 ingest",
                "读数": [dict(r) for r in _reads],
            }
    s, e, _ = _clip(win, cov)
    if not s or not e or s > e:
        return pack

    acc = conn.execute(
        "select coalesce(sum(cost),0) cost, coalesce(sum(clicks),0) clicks, "
        "coalesce(sum(ads_orders),0) orders, coalesce(sum(ads_sales),0) sales "
        "from fact_campaign_daily where report_date between ? and ?",
        (s, e),
    ).fetchone()
    clicks = float(acc["clicks"] or 0)
    sales = float(acc["sales"] or 0)
    pack["账户"] = {
        "窗口": [s, e],
        "cost": round(float(acc["cost"] or 0), 2),
        "clicks": int(clicks),
        "orders": int(acc["orders"] or 0),
        "sales": round(sales, 2),
        "cvr": round(float(acc["orders"] or 0) / clicks, 6) if clicks else None,
        "cpc": round(float(acc["cost"] or 0) / clicks, 4) if clicks else None,
        "acos": round(float(acc["cost"] or 0) / sales, 4) if sales else None,
    }
    if ents.get("portfolio"):
        p = _aggregate(conn, {"portfolio": ents["portfolio"], "campaign_ids": [],
                              "keyword_id": None, "name_like": None}, win, cov)
        if p:
            pack["组合"] = {
                "name": ents["portfolio"],
                "cost": round(p["cost"], 2), "clicks": int(p["clicks"]),
                "orders": int(p["orders"]), "sales": round(p["sales"], 2),
                "cpc": round(p["cpc"], 4), "cvr": round(p["cvr"], 6),
                "acos": None if p["acos"] == float("inf") else round(p["acos"], 4),
            }
            rows = conn.execute(
                "select campaign_id, campaign_name, coalesce(sum(cost),0) cost, "
                "coalesce(sum(clicks),0) clicks, coalesce(sum(ads_orders),0) orders, "
                "coalesce(sum(ads_sales),0) sales from fact_campaign_daily "
                "where portfolio_name=? and report_date between ? and ? "
                "group by campaign_id, campaign_name order by cost desc",
                (ents["portfolio"], s, e),
            ).fetchall()
            pack["组合内活动"] = [
                {
                    "campaign_id": r["campaign_id"], "name": r["campaign_name"],
                    "cost": round(float(r["cost"]), 2), "clicks": int(r["clicks"]),
                    "orders": int(r["orders"]), "sales": round(float(r["sales"]), 2),
                }
                for r in rows
            ]
            pl = conn.execute(
                "select placement, coalesce(sum(cost),0) cost, coalesce(sum(clicks),0) clicks, "
                "coalesce(sum(ads_orders),0) orders from fact_placement_daily "
                "where campaign_id in (select campaign_id from fact_campaign_daily "
                "where portfolio_name=?) and report_date between ? and ? group by placement",
                (ents["portfolio"], s, e),
            ).fetchall()
            pack["组合位次"] = [
                {"placement": r["placement"], "cost": round(float(r["cost"]), 2),
                 "clicks": int(r["clicks"]), "orders": int(r["orders"])}
                for r in pl
            ]
    # 断言点名的活动：必须单独给到「活动汇总 + 该活动的位次拆分 + 关键词拆分」。
    # 为什么不能只给账户/组合层：断言里写的是「该活动 TOS 花费占比」，
    # 模型拿到账户汇总根本没法算占比，只能回一句「无法判定」——
    # 那不是模型不行，是我们没给对数据。这一节就是补这个洞。
    if ents.get("campaign_ids"):
        marks = ",".join("?" * len(ents["campaign_ids"]))
        crows = conn.execute(
            f"select campaign_id, campaign_name, coalesce(sum(cost),0) cost, "
            f"coalesce(sum(clicks),0) clicks, coalesce(sum(ads_orders),0) orders, "
            f"coalesce(sum(ads_sales),0) sales from fact_campaign_daily "
            f"where campaign_id in ({marks}) and report_date between ? and ? "
            f"group by campaign_id, campaign_name order by cost desc",
            [*ents["campaign_ids"], s, e],
        ).fetchall()
        pack["断言涉及活动"] = [
            {
                "campaign_id": r["campaign_id"], "name": r["campaign_name"],
                "cost": round(float(r["cost"]), 2), "clicks": int(r["clicks"]),
                "orders": int(r["orders"]), "sales": round(float(r["sales"]), 2),
                "cpc": round(float(r["cost"]) / float(r["clicks"]), 4) if r["clicks"] else None,
                "cvr": round(float(r["orders"]) / float(r["clicks"]), 6) if r["clicks"] else None,
                "acos": round(float(r["cost"]) / float(r["sales"]), 4) if float(r["sales"]) > 0 else None,
            }
            for r in crows
        ]
        prows = conn.execute(
            f"select campaign_id, placement, coalesce(sum(cost),0) cost, "
            f"coalesce(sum(clicks),0) clicks, coalesce(sum(ads_orders),0) orders "
            f"from fact_placement_daily where campaign_id in ({marks}) "
            f"and report_date between ? and ? group by campaign_id, placement "
            f"order by campaign_id, cost desc",
            [*ents["campaign_ids"], s, e],
        ).fetchall()
        pack["活动位次拆分"] = [
            {
                "campaign_id": r["campaign_id"], "placement": r["placement"],
                "cost": round(float(r["cost"]), 2), "clicks": int(r["clicks"]),
                "orders": int(r["orders"]),
            }
            for r in prows
        ]
    if ents.get("keyword_id"):
        k = conn.execute(
            "select keyword_id, keyword_text, coalesce(sum(cost),0) cost, "
            "coalesce(sum(clicks),0) clicks, coalesce(sum(ads_orders),0) orders, "
            "coalesce(sum(ads_sales),0) sales from fact_keyword_daily "
            "where keyword_id=? and report_date between ? and ?",
            (ents["keyword_id"], s, e),
        ).fetchone()
        if k:
            pack["断言涉及关键词"] = {
                "keyword_id": k["keyword_id"], "text": k["keyword_text"],
                "cost": round(float(k["cost"]), 2), "clicks": int(k["clicks"]),
                "orders": int(k["orders"]), "sales": round(float(k["sales"]), 2),
            }
    if ents.get("name_like"):
        nrows = conn.execute(
            "select campaign_id, campaign_name, coalesce(sum(cost),0) cost, "
            "coalesce(sum(clicks),0) clicks, coalesce(sum(ads_orders),0) orders "
            "from fact_campaign_daily where campaign_name like ? "
            "and report_date between ? and ? group by campaign_id, campaign_name",
            (ents["name_like"], s, e),
        ).fetchall()
        pack["断言涉及活动(名称匹配)"] = [
            {"campaign_id": r["campaign_id"], "name": r["campaign_name"],
             "cost": round(float(r["cost"]), 2), "clicks": int(r["clicks"]),
             "orders": int(r["orders"])}
            for r in nrows
        ]

    dec = conn.execute(
        "select rule_id, action_type, count(*) n, "
        "sum(case when checked_at is not null then 1 else 0 end) n_checked, "
        "sum(case when coalesce(spend_impact,0) > 0 then 1 else 0 end) n_spend "
        "from decision_log where substr(decided_at,1,10) between ? and ? "
        "group by rule_id, action_type order by n desc",
        (s, e),
    ).fetchall()
    pack["决策流"] = [
        {"rule_id": r["rule_id"], "action_type": r["action_type"], "条数": r["n"],
         "checked_at非空": r["n_checked"], "spend_impact>0": r["n_spend"]}
        for r in dec
    ]
    # 按实体再切一刀：断言经常点名「某个活动/某个组合这几天的决策」，
    # 只给 rule_id 维度的话模型没法把条数落到实体上（实测它就因此判了 unverifiable）。
    # 只取前 30 个实体，避免组合级断言把 prompt 撑爆。
    ent_rows = conn.execute(
        "select entity_type, entity_id, entity_label, count(*) n, "
        "sum(case when checked_at is not null then 1 else 0 end) n_checked "
        "from decision_log where substr(decided_at,1,10) between ? and ? "
        "group by entity_type, entity_id, entity_label order by n desc limit 30",
        (s, e),
    ).fetchall()
    pack["决策流(按实体)"] = [
        {"entity_type": r["entity_type"], "entity_id": r["entity_id"],
         "label": r["entity_label"], "条数": r["n"], "checked_at非空": r["n_checked"]}
        for r in ent_rows
    ]
    return pack


def _model_label(requested: str) -> str:
    """converge_run.model 该记什么：实际应答过的模型，而不是用户请求的那个。

    降级链存在时这两者经常不同（请求 glm-5.3，实际 deepseek 应答）。
    记 requested 会让人以为「用 glm-5.3 能复现」，而拿 glm-5.3 重放只会 400。
    多个模型都答过就全部列出，用逗号分隔。
    """
    if not _MODELS_USED:
        return requested
    seen = list(dict.fromkeys(_MODELS_USED))
    return ",".join(seen)


def _chat_with_backoff(messages: list[dict], *, model: str) -> dict:
    """按降级链调模型，每个模型内部对 429 退避重试。

    为什么不在 gateway 里改：gateway 是别人在用的公共模块，
    它的 retries 只管网络层异常、HTTP 错误一律直抛，这是它对所有调用方的一致语义。
    限流与模型不可用是「本调用方」要处理的问题，退避节奏应该由调用方按自己的批量决定，
    塞进 gateway 会让所有调用方一起变慢。所以放在这里。

    降级顺序：model 参数（若显式指定）优先，然后是 LLM_MODEL_CHAIN。
    显式指定也保留降级 —— 用户指定的模型挂了不该让整轮回填空转。
    全部失败才抛出，由 llm_verdict 转成「未判定」。
    """
    chain = ([model] if model else []) + [m for m in LLM_MODEL_CHAIN if m != model]
    errors: list[str] = []
    for name in chain:
        last: Exception | None = None
        for attempt in range(LLM_RETRY_ON_429 + 1):
            try:
                out = gateway.chat(messages, model=name, max_tokens=LLM_MAX_TOKENS)
                # 记下真正应答的模型：降级链会让实际模型与 --model 不一致，
                # converge_run.model 必须写实际值，否则事后没法复现判定口径。
                _MODELS_USED.append(out.get("model") or name)
                return out
            except gateway.GatewayError as exc:
                msg = str(exc)
                # 只有 429 值得在同一个模型上重试：它是「慢一点就好」。
                # 400（上游挂了/参数被拒）和 502（网关自己出错）重试同模型都是浪费，
                # 直接跳到链上下一个模型 —— 实测 glm-5.3 400、kimi 502 时，
                # 换 deepseek 立刻就通。
                if "429" not in msg and "Too Many Requests" not in msg:
                    last = exc
                    break
                last = exc
                if attempt < LLM_RETRY_ON_429:
                    time.sleep(LLM_BACKOFF_SECONDS[min(attempt, len(LLM_BACKOFF_SECONDS) - 1)])
        errors.append(f"{name}: {last}")
    raise gateway.GatewayError("全部模型不可用 —— " + "；".join(errors))


class _ModelDown(RuntimeError):
    """模型侧整体不可用（不是断言判不了）。

    这个异常存在的唯一理由是：**不能把「没调到模型」写进 verdict**。
    网关抖一下就把断言记成 unverifiable，而幂等规则规定已填过的不再改，
    那条断言就被永久钉在错误结论上 —— 明明它只是没被读一眼。
    所以模型不可用时保持 pending，下一轮自然重试。
    """


def llm_verdict(conn, row, cov, win, ents, model) -> tuple[str, str]:
    """让模型判一条语义断言。返回 (verdict, reason)。

    模型调用失败抛 _ModelDown 而不是返回 unverifiable：
    这是「没判」和「判了判不出」的区别，前者必须保持 pending 等重试。
    模型正常返回但说「事实不足」才是真正的 unverifiable，那要落库。
    """
    system = PROMPT_PATH.read_text(encoding="utf-8") if PROMPT_PATH.exists() else (
        "你是广告投放的诊断复核员。给出严格 JSON："
        '{"verdict":"correct|wrong|unverifiable","reason":"..."}'
    )
    pack = _fact_pack(conn, row, cov, win, ents)
    user = (
        "下面是待复核的断言，以及它到期日之后我能查到的事实。\n"
        "只判这条断言本身是否成立；事实不足以定论就填 unverifiable。\n\n"
        + json.dumps(pack, ensure_ascii=False, indent=2)
    )
    try:
        out = _chat_with_backoff(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            model=model,
        )
    except Exception as exc:
        raise _ModelDown(f"模型调用失败：{exc}") from exc

    parsed = out.get("parsed") or {}
    verdict = str(parsed.get("verdict", "")).strip().lower()
    reason = str(parsed.get("reason", "")).strip()
    if verdict not in ("correct", "wrong", "unverifiable"):
        # 模型给了不合规的值 —— 这是它的问题，不是断言的问题。
        # 记 unverifiable 并原样保留它的输出，方便以后看它到底说了什么。
        return "unverifiable", f"模型输出不合规（verdict={verdict!r}）：{reason[:400]}"
    return verdict, reason[:800]


# ------------------------------------------------------------------ 判质疑
#
# 这一段和上面判 agent_eval 是**两件事**，不能合并，理由是 verdict 与 outcome 的分工：
#   verdict  = agent 写下这条反对时的「态度」：oppose（我反对）/ doubt（我怀疑）。
#              它是主张，永远不重写 —— 重写了就查不出「它当初到底说了什么」。
#   outcome  = 事后裁判给这条反对的「判决」：pending / correct / wrong / unverifiable。
#              它是事实，由数据说话，跟 agent 当时有多笃定无关。
# 合成一列的历史代价已经付过了：26 条反对全是 doubt，没有一条被判过对错，
# 于是「连续 7 轮打同一条规则」这种最值钱的信号读不出来 ——
# 分不清「它天天喊所以大概率真对」和「它天天喊所以大概率在幻觉」。
#
# 取值域刻意复用 agent_eval 的 pending|correct|wrong|unverifiable：
# 两套词表会让「质疑命中率」和「断言命中率」永远没法横向比。
#
# 三道闸门照抄 agent_eval 那一侧，一条都不放松：
#   ① verifiable_at 没到 → 根本不进队列（查询条件已经挡住）
#   ② 窗口没走完（_window_state != 'covered'）→ 保持 pending，记 deferred
#   ③ 确定性判定器读不懂 → 只有窗口已 covered 才敢判 unverifiable
# 第③条的方向很重要：把「数据没追平」写成 unverifiable，幂等 UPDATE 会把它永久钉死，
# 临时状态就被写成了永久结论 —— 这正是 agent_eval 那边 #56 踩过的坑。

_OBJ_FIELDS = ("claim", "verifiable_at", "method", "expected")


def _judge_one_objection(conn, obj, cov) -> tuple[str, str, str]:
    """判一条 objection。返回 (outcome, actual, judged_by)。

    一条反对里可能有多条 predicts（实测 5 条里有 2 条），判定口径是「全部兑现才算对」：
    只要有一条被证伪，这条反对就算 wrong —— 反对是一种整体主张，
    它说「这条决策有问题」，里面任何一条理由塌了，这个主张就不再成立。
    反过来，只要有一条还在等数据，整条就保持 pending，不能拿剩下的先结案。
    """
    try:
        preds = json.loads(obj["predicts_json"] or "[]")
    except Exception:
        preds = []
    if not isinstance(preds, list) or not preds:
        # 有到期日却没有可判的预测 —— 这类永远判不了，但也不能编个结论，
        # 保持 pending 并在 actual 里写明原因，让人一眼看出是「数据缺」而不是「忘了判」。
        return "pending", "无 predicts 可判（历史行当初没有这一列，无法补造判据）", "rule:none"

    verdicts: list[str] = []
    notes: list[str] = []
    who: list[str] = []
    for p in preds:
        if not isinstance(p, dict):
            continue
        view = {k: str(p.get(k) or "") for k in _OBJ_FIELDS}
        view["eval_id"] = obj["objection_id"]  # 只用于日志里定位，不是真 eval_id
        hit = deterministic_verdict(conn, view, cov)
        if hit:
            v, actual, name = hit
            if v == "pending":
                # 判定器自己说要等（_gate / _ev_decision_log 的覆盖闸门）
                return "pending", f"[预测 {len(verdicts) + 1}] {actual}", f"rule:{name}"
            verdicts.append(v)
            notes.append(f"[{len(verdicts)}] {actual}")
            who.append(name)
            continue
        # 没有确定性判定器认领这条预测。
        win = _parse_window(view["method"], view["verifiable_at"])
        state = _window_state(win, cov)
        if state != "covered":
            # 数据没追平 —— 这是临时状态，必须保持 pending 等下一轮
            return (
                "pending",
                f"[预测 {len(verdicts) + 1}] 窗口 {_win_text(win)} 未走完（数据止于 {cov[1]}），"
                f"且没有确定性判定器能解析这条预测，本轮不判",
                "rule:gate",
            )
        # 窗口已走完、规则仍读不懂 —— 规则不会自己变聪明，这是永久结论：
        # 判不了（unverifiable），而不是「还没判」（pending）。
        verdicts.append("unverifiable")
        notes.append(f"[{len(verdicts)}] 窗口 {_win_text(win)} 已走完，无判定器可解析：{view['expected'][:120]}")
        who.append("none")

    if not verdicts:
        return "pending", "predicts 里没有可用的条目", "rule:none"
    if "wrong" in verdicts:
        outcome = "wrong"
    elif all(v == "correct" for v in verdicts):
        outcome = "correct"
    else:
        outcome = "unverifiable"
    return outcome, "; ".join(notes), "rule:" + "+".join(dict.fromkeys(who))


def judge_objections(conn, *, as_of: str, limit: int | None = None,
                     dry_run: bool = False) -> dict:
    """判一轮到期的 objection，返回统计。

    只挑 `outcome='pending' AND verifiable_at <= as_of` 的行：
    verifiable_at IS NULL 的历史行永远进不来，这是故意的 ——
    那些行当初没落 predicts，任何「补判」都是在编造判据。
    """
    cov = _coverage(conn)
    sql = (
        "select objection_id, agent_run_id, decision_id, verdict, predicts_json, "
        "verifiable_at, outcome from agent_objection "
        "where outcome='pending' and verifiable_at is not null and verifiable_at <= ? "
        "order by verifiable_at, objection_id"
    )
    params: list = [as_of]
    if limit:
        sql += " limit ?"
        params.append(int(limit))
    due = conn.execute(sql, params).fetchall()

    stats = {"n_objection_due": len(due), "n_objection_judged": 0,
             "n_objection_deferred": 0, "n_objection_skipped": 0}
    rows_out: list[dict] = []
    now = datetime.now(CST).isoformat(timespec="seconds")

    for obj in due:
        try:
            outcome, actual, judged_by = _judge_one_objection(conn, obj, cov)
        except Exception as exc:  # 单条炸了不该拖垮整轮，也不能写库
            print(f"  ! 判 objection #{obj['objection_id']} 抛错：{exc}")
            stats["n_objection_skipped"] += 1
            continue

        if outcome == "pending":
            # 保持 pending，一条都不写库 —— 下一轮自动重判。
            deferred = judged_by not in ("rule:none",)
            stats["n_objection_skipped"] += 1
            if deferred:
                stats["n_objection_deferred"] += 1
            rows_out.append({"objection_id": obj["objection_id"], "outcome": outcome,
                             "judged_by": judged_by, "actual": actual,
                             "verdict": obj["verdict"], "due": obj["verifiable_at"]})
            tag = "本轮不判（数据未追平，下轮自动重判）" if deferred else "跳过（保持 pending）"
            print(f"  O#{obj['objection_id']:<4} due={obj['verifiable_at']}  [{judged_by}]  → {tag}")
            print(f"        actual: {actual}")
            continue

        stats["n_objection_judged"] += 1
        rows_out.append({"objection_id": obj["objection_id"], "outcome": outcome,
                         "judged_by": judged_by, "actual": actual,
                         "verdict": obj["verdict"], "due": obj["verifiable_at"]})
        print(f"  O#{obj['objection_id']:<4} due={obj['verifiable_at']}  [{judged_by}]  → {outcome}")
        print(f"        actual: {actual}")

        if not dry_run:
            # 幂等：WHERE 带 outcome='pending'。同一天重跑或两个进程并发，
            # 只有一个能把这条从 pending 改走；改不动就把计数退回来，
            # 否则 converge_run 记的「判了几条」会比库里实际多。
            cur = conn.execute(
                "update agent_objection set outcome=?, actual=?, checked_at=?, judged_by=? "
                "where objection_id=? and outcome='pending'",
                (outcome, actual, now, judged_by, obj["objection_id"]),
            )
            if cur.rowcount == 0:
                stats["n_objection_judged"] -= 1

    return stats


# ------------------------------------------------------------------ 默认假设
#
# §8.5 的 assumption→policy 分支：人跳过一道题 = 按 agent 的默认动作走，
# 同时登记一条 agent_assumption 等复核日拿数据验。这一支之前完全没实现 ——
# assume() 只负责写行，没有任何代码在复核日回来读它，于是「整题跳过」
# 实际等于「永久跳过」：默认动作到底对不对，永远不会有人知道。
#
# 为什么判定必须由代码算、不能交给模型：
# 这一支的产出会往 policy 里写草案，而 policy 是「人已经表过态」的地方
# （agents/context.py 与 routes/meta.py 都读它）。让模型决定写不写策略，
# 等于让它在没有复核的情况下替人表态；而这里的判据（点击够不够判定线、
# 方向和基线比是否显著）本来就是纯算术，用 SQL + 二项检验算得比模型稳，
# 而且可复现 —— 同一份数据重跑必须得到同一个结论。
#
# 跳过一道题意味着「按默认动作走」，所以「数据往哪个方向走才推翻这个默认」
# 是这道题自带的语义，不是事后能推断的东西。写成表而不是让模型猜，理由同上：
#   Q01 关键词 停/降 → 默认「先观察继续跑」→ 只有显著更差才推翻（该停）
#   Q03 加预算       → 默认「先不加」      → 只有显著更好才推翻（该加）
#   Q04 位次加价     → 默认「先不改」      → 同上
#   Q05 探索实验     → 默认「继续攒证据」  → 显著更好说明有戏，推翻「不用试」
#   Q08 目标 ACOS    → 默认「不放宽目标」  → CVR 显著更差说明目标更难达到，该放宽
# 表里没有的题号一律 unverifiable：不知道该往哪个方向读的数据，写出来的策略
# 提案只能是猜的，而猜错的草案一旦被人一键批准，就会反过来约束后面几轮诊断。
_REFUTE_ON = {
    "Q01": "worse",
    "Q03": "better",
    "Q04": "better",
    "Q05": "better",
    "Q08": "worse",
}

# object_id 可能是 ASIN（整题跳过会挂在 scope.SCOPE_ASIN 上），而三张 fact 表
# 都没有 ASIN 列，只能靠形状认。认错的下场是把一个 campaign_id 当成 ASIN
# 去汇总全店 —— 所以这里用 Amazon ASIN 的严格形状（B0 + 8 位）。
_ASIN_LIKE = re.compile(r"^B0[A-Z0-9]{8}$")

_AGG_SQL = (
    "select coalesce(sum(clicks),0) clicks, coalesce(sum(ads_orders),0) orders, "
    "count(distinct report_date) days from {table} where {where} "
    "and report_date between ? and ?"
)
_SUM_SQL = (
    "select coalesce(sum(clicks),0) clicks, coalesce(sum(ads_orders),0) orders "
    "from {table} where {where} and report_date between ? and ?"
)


def _assume_target(conn: sqlite3.Connection, object_id: str) -> dict | None:
    """把 agent_assumption.object_id 翻成一个可聚合的实体 + 一个对照组。

    为什么一定要顺手配对照组：§8.5 把 strong 定义成「样本够 且 有对照 且
    效应方向与对照相反」。没有对照时，一个词这周转化好完全可能只是全店都旺，
    把它记成「这个假设成立」就是把行情当成了结论。所以能配对照就一定配上，
    配不上就老实降级成 medium，而不是假装自己验过了。

    认不出来返回 None（而不是退化成全店汇总）：把查不到的 object_id 拿去
    汇总全店数据，会得出一个和这个对象毫无关系的结论，比不判坏得多。
    """
    oid = str(object_id or "").strip()
    if not oid:
        return None

    # 1) keyword_id（report.py 出 Q01 时 entity_id 就是它）
    row = conn.execute(
        "select keyword_id, keyword_text, campaign_id from fact_keyword_daily "
        "where keyword_id=? limit 1", (oid,)
    ).fetchone()
    if row:
        ctrl_where = "campaign_id=? and keyword_id<>?"
        ctrl_params: list = [row["campaign_id"], oid]
        ctrl_label = f"同活动 {row['campaign_id']} 的其它关键词"
        if not row["campaign_id"]:
            ctrl_where, ctrl_params, ctrl_label = None, [], None
        return {
            "kind": "keyword",
            "label": f"keyword_id={oid}（{row['keyword_text'] or '?'}）",
            "table": "fact_keyword_daily",
            "sql": _AGG_SQL.format(table="fact_keyword_daily", where="keyword_id=?"),
            "params": [oid],
            "ctrl_sql": (_SUM_SQL.format(table="fact_keyword_daily", where=ctrl_where)
                         if ctrl_where else None),
            "ctrl_params": ctrl_params,
            "ctrl_label": ctrl_label,
        }

    # 2) campaign_id（Q03 加预算 / Q04 位次加价）
    row = conn.execute(
        "select campaign_id, campaign_name, portfolio_name from fact_campaign_daily "
        "where campaign_id=? limit 1", (oid,)
    ).fetchone()
    if row:
        ctrl_where = "portfolio_name=? and campaign_id<>?"
        ctrl_params = [row["portfolio_name"], oid]
        ctrl_label = f"同组合「{row['portfolio_name']}」的其它活动"
        if not (row["portfolio_name"] or "").strip():
            ctrl_where, ctrl_params, ctrl_label = None, [], None
        return {
            "kind": "campaign",
            "label": f"campaign_id={oid}（{row['campaign_name'] or '?'}）",
            "table": "fact_campaign_daily",
            "sql": _AGG_SQL.format(table="fact_campaign_daily", where="campaign_id=?"),
            "params": [oid],
            "ctrl_sql": (_SUM_SQL.format(table="fact_campaign_daily", where=ctrl_where)
                         if ctrl_where else None),
            "ctrl_params": ctrl_params,
            "ctrl_label": ctrl_label,
        }

    # 3) 关键词原文（§8.5 的示例 object_id 写的就是 "lat pulldown"）
    row = conn.execute(
        "select keyword_id, keyword_text, campaign_id from fact_keyword_daily "
        "where keyword_text=? order by report_date desc limit 1", (oid,)
    ).fetchone()
    if row:
        ctrl_where = "campaign_id=? and keyword_text<>?"
        ctrl_params = [row["campaign_id"], row["keyword_text"]]
        ctrl_label = f"同活动 {row['campaign_id']} 的其它关键词"
        if not row["campaign_id"]:
            ctrl_where, ctrl_params, ctrl_label = None, [], None
        return {
            "kind": "keyword_text",
            "label": f"keyword_text='{oid}'",
            "table": "fact_keyword_daily",
            "sql": _AGG_SQL.format(table="fact_keyword_daily", where="keyword_text=?"),
            "params": [oid],
            "ctrl_sql": (_SUM_SQL.format(table="fact_keyword_daily", where=ctrl_where)
                         if ctrl_where else None),
            "ctrl_params": ctrl_params,
            "ctrl_label": ctrl_label,
        }

    # 4) 整题跳过挂在 ASIN 上（Q08 之类）→ 全店汇总，没有对照组
    if _ASIN_LIKE.match(oid):
        return {
            "kind": "account",
            "label": f"全店（object_id={oid}）",
            "table": "fact_campaign_daily",
            "sql": _AGG_SQL.format(table="fact_campaign_daily", where="1=1"),
            "params": [],
            "ctrl_sql": None,
            "ctrl_params": [],
            "ctrl_label": None,
        }
    return None


def _assume_observed(conn: sqlite3.Connection, tgt: dict,
                     start: str, end: str) -> dict:
    """复核窗口内这个对象的实际数据。

    days 记的是**真有数据的天数**（count distinct report_date），不是窗口长度：
    窗口 14 天但只有 9 天有报表，说明这个对象中间断投过，
    拿窗口天数当样本量会让人以为样本比实际更厚。
    """
    row = conn.execute(tgt["sql"], [*tgt["params"], start, end]).fetchone()
    clicks = int(row["clicks"] or 0) if row else 0
    orders = int(row["orders"] or 0) if row else 0
    days = int(row["days"] or 0) if row else 0
    return {"clicks": clicks, "orders": orders,
            "cvr": round(orders / clicks, 4) if clicks else 0.0,
            "days": days}


def _assume_control(conn: sqlite3.Connection, tgt: dict,
                    start: str, end: str) -> dict | None:
    if not tgt.get("ctrl_sql"):
        return None
    row = conn.execute(tgt["ctrl_sql"], [*tgt["ctrl_params"], start, end]).fetchone()
    if not row:
        return None
    clicks = int(row["clicks"] or 0)
    orders = int(row["orders"] or 0)
    if clicks <= 0:
        return None
    return {"clicks": clicks, "orders": orders, "label": tgt["ctrl_label"]}


def _assume_predicts(tgt: dict, as_of: str, obs: dict) -> list[dict]:
    """按 §8.5 的 predicts 形状，给这条假设留一条下一轮能验的断言。

    为什么每条都要留：默认假设判成 confirmed 之后，如果没人再回来看，
    它就变成一条永久的「我们当初赌对了」。留一条 14 天后的可验证断言，
    才能在下一次收敛里发现「当初对，现在不一定还对」。
    """
    try:
        nxt = (date.fromisoformat(as_of) + timedelta(days=14)).isoformat()
    except (TypeError, ValueError):
        return []
    return [{
        "claim": f"再观察 14 天（到 {nxt}）orders 仍 ≤ {int(obs['orders'])}",
        "verifiable_at": nxt,
        "method": f"{tgt['table']} 累计",
        "expected": f"orders <= {int(obs['orders'])}",
    }]


def _bump_skip_ratio(conn: sqlite3.Connection, object_id: str, question_code: str,
                     *, dry_run: bool = False) -> dict:
    """refuted 之后把「这个题号在这个对象上被跳过」的权重抬一档。

    为什么先探测列：§8.5 说提权写在 reason_hit.skip_ratio 上，但这一列在生产库里
    并不存在（实际列只有 hit_id / object_id / reason_code / scope / evidence_text /
    created_at / answer_type / decision_id）。这里绝不能自己 ALTER ——
    收敛 agent 在这条链上是「只读旁证」的角色，改表结构会让「谁动过库」
    变成一件查不清的事。列不存在就跳过并留一句 note，等表结构补上后这段自动生效。

    匹配用 (object_id, scope)：approve.py 写 reason_hit 时把题目 id 塞进 scope 列
    （见 adsdesk/api/routes/approve.py 的 insert），所以 scope 就是 question_code。
    """
    try:
        cols = {r[1] for r in conn.execute("pragma table_info(reason_hit)")}
    except sqlite3.Error as exc:
        return {"applied": False, "note": f"reason_hit 探测失败，提权跳过：{exc}"}
    if "skip_ratio" not in cols:
        return {"applied": False, "note": "reason_hit.skip_ratio 未建，提权跳过"}
    if dry_run:
        return {"applied": False, "note": "dry-run，提权未写"}
    cur = conn.execute(
        "update reason_hit set skip_ratio=min(1.0, coalesce(skip_ratio,0)+0.2) "
        "where object_id=? and scope=?", (str(object_id), str(question_code)))
    return {"applied": cur.rowcount > 0, "rows": cur.rowcount}


def judge_assumptions(conn, *, as_of: str, dry_run: bool = False,
                      limit: int | None = None,
                      out: list | None = None) -> dict:
    """判一轮到期的默认假设，返回统计。§8.5 的 assumption→policy 分支。

    为什么单独成函数、而不是并进 judge_objections：
    质疑（agent_objection）问的是「agent 说过的话后来对不对」，判完写回自己那张表；
    默认假设问的是「人跳过这道题、按默认动作走，这个决定站不站得住」，判完要往
    policy 里写草案。两条链的写入对象不同、闸门相同，混在一起就没法单独给假设
    排一条定时任务，也没法在假设这半边出问题时把它单独关掉。

    三个闸门与断言那半边完全一致：
      ① 覆盖闸门 —— 复核窗口没被数据追平 → 只记 deferred，一条都不写。
         判的是 CVR（比值），比值没有单调性（分子分母都在涨），
         所以 _gate 里「单调量越过阈值就提前放行」那条例外在这里永远不该触发。
      ② 幂等 —— UPDATE 带 `result is null`，同一天重跑只有一次能写进去。
      ③ dry_run —— 零写入，连 agent_assumption.result 都不改。

    out 给出时把每条的完整结果（§8.5 的 JSON 形状）追加进去，供 run_once
    塞进 output_json；不给就只打印。这样函数本身不用知道 output_json 长什么样。
    """
    cov = _coverage(conn)
    sql = (
        "select assumption_id, object_id, question_code, default_action, "
        "created_at, review_at from agent_assumption "
        "where result is null and review_at <= ? order by review_at, assumption_id"
    )
    params: list = [as_of]
    if limit:
        sql += " limit ?"
        params.append(int(limit))
    due = conn.execute(sql, params).fetchall()

    stats = {"n_assumption_due": len(due), "n_assumption_confirmed": 0,
             "n_assumption_refuted": 0, "n_assumption_unverifiable": 0,
             "n_assumption_deferred": 0, "n_policy_drafted": 0}
    rows_out: list[dict] = []
    now = datetime.now(CST).isoformat(timespec="seconds")
    baseline = _account_cvr(conn)

    for a in due:
        aid = a["assumption_id"]
        start = str(a["created_at"] or "")[:10]
        end = str(a["review_at"] or "")[:10]
        win = (start or None, end)
        q = str(a["question_code"] or "").strip()
        oid = str(a["object_id"] or "")
        row: dict = {
            "assumption_id": aid, "object_id": oid, "question_code": q,
            "default_action": a["default_action"], "created_at": a["created_at"],
            "review_at": a["review_at"],
            "observed": {"clicks": 0, "orders": 0, "cvr": 0.0, "days": 0},
            "result": None, "why": "", "policy_proposal": None, "predicts": [],
            "evidence_level": None,
        }

        # ① 覆盖闸门：数据没追平 → deferred，result 留 NULL 等下一轮。
        # 写成 unverifiable 会把临时状态钉成永久结论（本文件开头铁律第三条）。
        if _window_state(win, cov) != "covered":
            stats["n_assumption_deferred"] += 1
            row["result"] = "deferred"
            row["why"] = (f"复核窗口 {_win_text(win)} 未走完（数据止于 {cov[1]}），"
                          f"本轮不判，等 ingest 追平后自动重判")
            rows_out.append(row)
            print(f"  A#{aid:<4} review={end}  [gate]  → 本轮不判（数据未追平，下轮自动重判）")
            print(f"        why: {row['why']}")
            continue

        tgt = _assume_target(conn, oid)
        if tgt is None:
            result, why = "unverifiable", (
                f"object_id={oid} 在 fact_keyword_daily / fact_campaign_daily 里查不到，"
                f"取不到这个对象的数据")
        elif not baseline:
            result, why = "unverifiable", (
                "memorial 最新一包里没有 account.cvr 基线，算不出判定线，无法判断")
        else:
            obs = _assume_observed(conn, tgt, start, end)
            row["observed"] = obs
            if obs["clicks"] <= 0:
                result, why = "unverifiable", (
                    f"复核窗口 {_win_text(win)} 内该对象 0 击，样本为空，"
                    f"无法判断「{a['default_action']}」这个假设成立与否")
            else:
                sig = facts.significance(obs["clicks"], obs["orders"], baseline)
                refute_on = _REFUTE_ON.get(q)
                if refute_on is None:
                    result = "unverifiable"
                    why = (f"题号 {q or '(空)'} 没有登记「数据往哪个方向才推翻默认动作」，"
                           f"不知道该往哪读，交给人判")
                elif sig["verdict"] == "inconclusive":
                    need = sig["need_for_orders"]
                    result = "unverifiable"
                    why = (f"到复核日累计 {obs['clicks']} 击 {obs['orders']} 单，"
                           + (f"仍不足判定线 {need} 击，" if need else "仍不足判定线，")
                           + f"无法判断「{a['default_action']}」这个假设成立与否")
                else:
                    result = "refuted" if sig["verdict"] == refute_on else "confirmed"
                    direction = "更差" if sig["verdict"] == "worse" else "更好"
                    p = sig["p_worse"] if sig["verdict"] == "worse" else sig["p_better"]
                    why = (f"到复核日累计 {obs['clicks']} 击 {obs['orders']} 单，"
                           f"基线 {baseline:.2%} 应出 {sig['expected_orders']} 单；"
                           f"显著{direction}（p={p}），"
                           + ("默认动作被推翻" if result == "refuted" else "默认动作被支持"))

                # evidence_level：§8.5 三档。样本够 = 点击越过判定线；
                # 有对照 = 对照组方向与本对象相反（同一周全店都旺时，
                # 本对象「更好」很可能只是行情，不能算独立证据）。
                need = sig["need_for_orders"]
                enough = bool(need) and obs["clicks"] >= int(need)
                ctrl = _assume_control(conn, tgt, start, end)
                ctrl_opposite = False
                if ctrl:
                    csig = facts.significance(ctrl["clicks"], ctrl["orders"], baseline)
                    ctrl_opposite = (csig["statistically_conclusive"]
                                     and csig["verdict"] != sig["verdict"])
                    row["control"] = {"label": ctrl["label"], "clicks": ctrl["clicks"],
                                      "orders": ctrl["orders"], "verdict": csig["verdict"],
                                      "opposite": ctrl_opposite}
                if enough and ctrl_opposite:
                    row["evidence_level"] = "strong"
                elif enough or ctrl_opposite:
                    row["evidence_level"] = "medium"
                else:
                    row["evidence_level"] = "weak"
                row["threshold_clicks"] = need
                if row["evidence_level"] != "strong":
                    why += f"；证据 {row['evidence_level']}（" + "、".join(
                        [x for x in (("样本已过判定线" if enough else f"样本未过判定线 {need} 击"),
                                     ("有反向对照组" if ctrl_opposite
                                      else ("对照组同向或不可判" if ctrl else "无对照组")))
                         if x]) + "）"

        row["result"] = result
        row["why"] = why
        if tgt is not None:
            row["predicts"] = _assume_predicts(tgt, as_of, row["observed"])

        # ② 落库：confirmed / refuted 才写 policy 草案，unverifiable 只更新假设本身。
        if result in ("confirmed", "refuted"):
            stats["n_assumption_" + result] += 1
            ptype = f"assumption_{q.lower()}_{result}"
            scope = "account" if (tgt and tgt["kind"] == "account") else "object"
            proposal = {
                "policy_type": ptype,
                "scope": scope,
                "object_id": oid,
                "status": "draft",          # 必须等人到策略页点批准才 active
                "source": "agent_converge",
                "evidence_text": why[:500],
                "params_json": {
                    "assumption_id": aid,
                    "question_code": q,
                    "default_action": a["default_action"],
                    "observed": row["observed"],
                    "evidence_level": row["evidence_level"],
                    "threshold_clicks": row.get("threshold_clicks"),
                    "control": row.get("control"),
                    "predicts": row["predicts"],
                },
            }
            row["policy_proposal"] = proposal
            # weak 不进策略库 —— §9.3 的硬门。weak 即使 confirmed 也不写：
            # 样本没过判定线、又没有反向对照，这种结论写进策略库
            # 只会让「弱证据也能改投放」成为既成事实。
            #
            # 这道闸门放在 dry_run 判断**之前**：dry-run 的意义就是提前看见
            # 「真跑会写几条、哪些会被拦」，把闸门藏在 not dry_run 里面，
            # dry-run 就永远显示草案 0 条，看上去像整条链没接通。
            # 去重查询是纯 SELECT，dry-run 下读一下不违反零写入。
            if row["evidence_level"] == "weak":
                proposal["note"] = "evidence_level=weak，按 §9.3 不写策略库，只记在 output_json"
            else:
                dup = conn.execute(
                    "select policy_id from policy where object_id=? and policy_type=? "
                    "and source='agent_converge' limit 1", (oid, ptype)).fetchone()
                if dup:
                    proposal["note"] = f"同对象同题同结论的草案已存在（policy_id={dup['policy_id']}），跳过"
                elif dry_run:
                    proposal["note"] = "dry-run，未写库"
                    stats["n_policy_drafted"] += 1
                else:
                    cur = conn.execute(
                        "insert into policy (scope, object_id, policy_type, params_json, "
                        "source, evidence_text, status, created_at) "
                        "values (?,?,?,?,?,?,'draft',?)",
                        (scope, oid, ptype,
                         json.dumps(proposal["params_json"], ensure_ascii=False),
                         "agent_converge", why[:500], now))
                    proposal["policy_id"] = cur.lastrowid
                    stats["n_policy_drafted"] += 1

        elif result == "unverifiable":
            stats["n_assumption_unverifiable"] += 1

        # refuted 的第二个动作：提权，让这个题号下一轮更容易被问到。
        if result == "refuted":
            bump = _bump_skip_ratio(conn, oid, q, dry_run=dry_run)
            row["reason_hit_bump"] = bump
            if bump.get("note"):
                row["note"] = bump["note"]

        rows_out.append(row)
        # 标签只标闸门拦下的那条；unverifiable 却打 [gate] 会让人以为
        # 「数据没追平所以不判」，而实际上它已经被判过、结论是「判不了」。
        tag = "gate" if result == "deferred" else (row["evidence_level"] or "n/a")
        print(f"  A#{aid:<4} review={end}  [{tag}]  → {result}")
        print(f"        observed: {row['observed']}")
        print(f"        why: {why}")
        if row.get("note"):
            print(f"        note: {row['note']}")

        # ③ 幂等写回：WHERE 带 result is null，并发/重跑只有一个能写进去。
        if not dry_run:
            cur = conn.execute(
                "update agent_assumption set result=? where assumption_id=? and result is null",
                (result, aid))
            if cur.rowcount == 0:
                # 被别的进程抢先判了：把本轮的计数与草案一并退回，
                # 否则 converge_run 记的「判了几条」会比库里实际多。
                if result in ("confirmed", "refuted"):
                    stats["n_assumption_" + result] -= 1
                    # 草案也要退：两个进程可能同时越过上面的去重查询，
                    # 各写一条；抢输的那个如果不删，库里就留一条
                    # 没有对应假设结论的孤儿草案 —— 而 policy 是会被
                    # 人一键批准的地方，孤儿草案被批就等于凭空多一条策略。
                    # 用 lastrowid 精确定位自己插的那一条，不会误删对方的。
                    pid = (row.get("policy_proposal") or {}).get("policy_id")
                    if pid:
                        conn.execute("delete from policy where policy_id=? and status='draft'",
                                     (pid,))
                        row["policy_proposal"]["note"] = "并发抢输，草案已回退"
                        stats["n_policy_drafted"] -= 1

    if out is not None:
        out.extend(rows_out)
    return stats


# ------------------------------------------------------------------ 复活
#
# unverifiable 是永久结论 —— 写进去就被幂等 UPDATE 钉死，以后再也不判。
# 这个设计对「证据永远拿不到」是对的（比如那条要求对比两次重拉读数的断言，
# 在 fact_read_snapshot 建起来之前确实永远拿不到）。
# 但当我们**新修了一条证据通路**，那些「因为缺证据而 unverifiable」的断言
# 就该重新给一次机会 —— 否则修了等于没修。
#
# 实测 #74：2026-11-20 被判 unverifiable（actual 明写「没有任何按日或按次快照的
# clicks 数值」），当天下午 fact_read_snapshot 建好并回填了 11-01~11-19 基线，
# 它立刻就能判了（现在 pending，等重拉）。不放回来的话，这条断言永久停在
# 「证据不存在」这句已经不成立的话上。
#
# 只复活 actual 里明确写着「缺某样东西」且那样东西现在已经存在的，不做批量重置。

# (actual 里必须出现的片段, 该片段对应的证据源现在是否可用)
_REVIVE_RULES = [
    ("按日或按次快照", "select count(*) from fact_read_snapshot"),
    ("没有任何按日", "select count(*) from fact_read_snapshot"),
    ("两次读数", "select count(*) from fact_read_snapshot"),
]


def revive_unverifiable(conn: sqlite3.Connection, *, dry_run: bool = False) -> list[dict]:
    """把「因缺证据被判 unverifiable、而该证据源现在已有数据」的断言放回 pending。

    返回复活的行（含 eval_id / 命中片段 / 证据源行数）。不复活其它原因
    unverifiable 的 —— 那些证据可能真的永远拿不到，反复重判只是烧模型额度。
    """
    out: list[dict] = []
    rows = conn.execute(
        "select eval_id, actual from agent_eval where verdict='unverifiable' "
        "and actual is not null"
    ).fetchall()
    for r in rows:
        actual = str(r["actual"] or "")
        for frag, sql in _REVIVE_RULES:
            if frag not in actual:
                continue
            try:
                n = conn.execute(sql).fetchone()[0]
            except sqlite3.OperationalError:
                n = 0
            if not n:
                continue
            out.append({"eval_id": r["eval_id"], "fragment": frag, "source_rows": n,
                        "actual": actual[:200]})
            if not dry_run:
                # 只清判定结果，不动 claim/expected —— 那是 agent 的原话。
                # actual 留空而不是保留旧文：旧文说的是「没有快照」，现在有了，
                # 留着会误导下一轮读它的人（包括模型）。
                conn.execute(
                    "update agent_eval set verdict='pending', actual=NULL, checked_at=NULL "
                    "where eval_id=? and verdict='unverifiable'",
                    (r["eval_id"],),
                )
            break
    if not dry_run and out:
        conn.commit()
    return out


# ------------------------------------------------------------------ 主流程

def run_once(
    conn: sqlite3.Connection,
    *,
    as_of: str | None = None,
    model: str | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    use_llm: bool = True,
    judge_eval: bool = True,
    with_objections: bool = True,
    with_assumptions: bool = True,
) -> dict:
    """回填一轮。返回统计 dict，不抛异常。

    as_of 的存在理由：所有断言的 verifiable_at 都在明天之后，
    没有这个参数，这个脚本从写出来那天起就一直空跑，等于没写。
    生产上由 launchd 每天跑（不带 as_of），调试/演示时用它把时钟推到任意一天。

    judge_eval / with_objections / with_assumptions 三个开关是为了让
    `--judge-objections` / `--assumptions-only` 能只跑一条队：断言、质疑、
    默认假设是三条互相独立的队列，混在一个开关里就没法单独给某一条排定时任务，
    也没法在其中一条出问题时把它单独关掉。
    """
    ensure_schema(conn)
    today = as_of or datetime.now(CST).date().isoformat()
    model = model or gateway.DEFAULT_MODEL
    # 每轮开头清空，避免上一轮的模型名污染这一轮的记录。
    _MODELS_USED.clear()
    started = datetime.now(CST).isoformat(timespec="seconds")
    cov = _coverage(conn)

    # 先把「因缺证据被判 unverifiable、而该证据源现在已有数据」的断言放回 pending。
    # 必须在查 due 之前 —— 否则这一轮它们还看不见，等于白修了证据通路。
    revived = revive_unverifiable(conn, dry_run=dry_run) if judge_eval else []
    if revived:
        print(f"[收敛] 复活 {len(revived)} 条此前因缺证据被判 unverifiable 的断言："
              + "、".join(f"#{r['eval_id']}（{r['fragment']}，源 {r['source_rows']} 行）"
                          for r in revived))

    if judge_eval:
        sql = (
            "select eval_id, agent_run_id, claim, verifiable_at, method, expected, verdict "
            "from agent_eval where verdict='pending' and verifiable_at <= ? "
            "order by verifiable_at, eval_id"
        )
        params: list = [today]
        if limit:
            sql += " limit ?"
            params.append(int(limit))
        due = conn.execute(sql, params).fetchall()
    else:
        due = []
    n_due = len(due)

    print(f"[收敛] as_of={today} 数据覆盖={cov[0]}~{cov[1]} 到期断言={n_due} 条"
          + ("  (dry-run)" if dry_run else ""))

    stats = {"n_due": n_due, "n_filled": 0, "n_correct": 0, "n_wrong": 0,
             "n_unverifiable": 0, "n_skipped": 0, "n_deferred": 0,
             # 质疑那半边的 deferred / skipped 也先摆 0：
             # --assumptions-only 会关掉质疑分支，下游打印仍要能读到这两个 key。
             "n_objection_due": 0, "n_objection_judged": 0,
             "n_objection_deferred": 0, "n_objection_skipped": 0,
             # 假设那半边的计数先摆 0 再被 judge_assumptions 覆盖：
             # 关掉 --no-assumptions 时下游打印仍要能读到这几个 key，
             # 缺 key 会让「没跑」和「跑了 0 条」在日志里长得一样。
             "n_assumption_due": 0, "n_assumption_confirmed": 0,
             "n_assumption_refuted": 0, "n_assumption_unverifiable": 0,
             "n_assumption_deferred": 0, "n_policy_drafted": 0,
             "n_revived": len(revived)}
    llm_used = 0
    rows_out: list[dict] = []
    assumption_rows: list[dict] = []

    for row in due:
        win = _parse_window(row["method"] or "", row["verifiable_at"] or "")
        ents = _parse_entities(conn, f"{row['method'] or ''} {row['claim'] or ''}")
        hit = deterministic_verdict(conn, row, cov)
        if hit:
            verdict, actual, who = hit
        elif _window_state(win, cov) != "covered":
            # 窗口还没走完、确定性判定器也没能锁定结论 → 不能交给模型。
            # 事实包是按数据覆盖裁过的，模型面对「11-20 的窗口、只到 11-18 的事实」
            # 只能回 unverifiable；而 unverifiable 一旦落库就被幂等 UPDATE 钉死，
            # 这条断言从此再也不会被判 —— 临时状态被写成了永久结论。
            # 实测 #56（due 11-20，method 没写日期区间）就是这么被钉的。
            verdict, actual, who = (
                "pending",
                f"断言窗口 {_win_text(win)} 未走完（数据止于 {cov[1]}），"
                f"确定性判定器无法锁定结论，本轮不问模型，等 ingest 追平后自动重判",
                "gate",
            )
        elif use_llm and llm_used < LLM_BATCH_LIMIT:
            # 串行节流：429 与请求速率强相关，连续猛发只会连续被拒。
            # 每条之间空 1.5s，一整轮 8 条约多花 12s，比退避重试便宜得多。
            if llm_used:
                time.sleep(LLM_THROTTLE_SECONDS)
            try:
                verdict, actual = llm_verdict(conn, row, cov, win, ents, model)
            except _ModelDown as exc:
                # 模型整体不可用：整批剩下的都没戏，直接收工。
                # 关键：这些断言一条都不写库，保持 pending，下一轮自动重试。
                # 若这里写成 unverifiable，幂等规则会把它永久钉死。
                stats["error"] = str(exc)
                print(f"  ! {exc}")
                print(f"  ! 本轮剩余 {len(due) - len(rows_out)} 条断言保持 pending，等网关恢复后重试")
                break
            who = "llm"
            llm_used += 1
        elif use_llm:
            # 额度用尽：这条根本没试过。跳过、不写库，留给下一轮。
            # 写 unverifiable 会把「没试」永久记成「判不出」，是同一类错误。
            verdict, actual, who = "pending", f"本轮模型额度已用尽（上限 {LLM_BATCH_LIMIT} 条），未判定，下轮再判", "skipped"
        else:
            verdict, actual, who = "pending", "确定性判定器无法解析该断言（--no-llm 已关闭模型兜底），未判定", "skipped"

        if verdict == "pending":
            # pending 表示「这轮没判成」，不是判定结果 —— 一条都不写库。
            # 统计里单独记 n_skipped，让运维能区分「判完了」和「没轮到」。
            # n_deferred 再细分一层：who != 'skipped' 说明是判定器/覆盖闸门主动挂起
            # （数据没追平），而不是额度或 --no-llm 造成的没轮到。
            stats["n_skipped"] += 1
            deferred = who != "skipped"
            if deferred:
                stats["n_deferred"] += 1
            rows_out.append(
                {"eval_id": row["eval_id"], "verdict": verdict, "who": who,
                 "due": row["verifiable_at"], "actual": actual, "expected": row["expected"]}
            )
            tag = "本轮不判（数据未追平，下轮自动重判）" if deferred else "跳过（保持 pending）"
            print(f"  #{row['eval_id']:<4} due={row['verifiable_at']}  [{who}]  → {tag}")
            print(f"        expected: {row['expected']}")
            print(f"        actual  : {actual}")
            continue

        stats["n_filled"] += 1
        stats["n_" + ("correct" if verdict == "correct" else "wrong" if verdict == "wrong" else "unverifiable")] += 1
        rows_out.append(
            {"eval_id": row["eval_id"], "verdict": verdict, "who": who,
             "due": row["verifiable_at"], "actual": actual, "expected": row["expected"]}
        )

        print(f"  #{row['eval_id']:<4} due={row['verifiable_at']}  [{who}]  → {verdict}")
        print(f"        expected: {row['expected']}")
        print(f"        actual  : {actual}")

        if not dry_run:
            # WHERE 带 verdict='pending' 是幂等的全部秘密：
            # 同一天重跑、两个进程并发跑，都只有一个能把这条从 pending 改走。
            cur = conn.execute(
                "update agent_eval set verdict=?, actual=?, checked_at=? "
                "where eval_id=? and verdict='pending'",
                (verdict, actual, datetime.now(CST).isoformat(timespec="seconds"), row["eval_id"]),
            )
            if cur.rowcount == 0:
                stats["n_filled"] -= 1
                stats["n_" + ("correct" if verdict == "correct" else "wrong" if verdict == "wrong" else "unverifiable")] -= 1

    # 判质疑：走的是另一张表（agent_objection），另一套闸门复用同一批判定器。
    # 放在断言循环之后、dry_run 返回之前 —— dry-run 也要能看见质疑那半边的输出，
    # 否则「整条链通不通」在 dry-run 下就有一半是瞎的。
    if with_objections:
        print("[收敛] —— 判质疑（outcome）——")
        obj_stats = judge_objections(conn, as_of=today, limit=limit, dry_run=dry_run)
        stats.update(obj_stats)
        print(f"[收敛] 质疑：到期 {stats['n_objection_due']} 条，判定 {stats['n_objection_judged']} 条，"
              f"暂不判 {stats['n_objection_deferred']} 条")

    # 判默认假设：第三条队。到复核日的 agent_assumption 拿实际数据判一次，
    # confirmed / refuted 才往 policy 写草案（weak 除外），unverifiable 只改自己。
    # 同样放在 dry_run 返回之前：dry-run 要能看见这条链通不通，
    # 而它恰恰是唯一会往 policy 写东西的一支，最需要在 dry-run 下先看一眼。
    if with_assumptions:
        print("[收敛] —— 判默认假设（agent_assumption）——")
        asm_stats = judge_assumptions(conn, as_of=today, limit=limit,
                                      dry_run=dry_run, out=assumption_rows)
        stats.update(asm_stats)
        print(f"[收敛] 假设：到期 {stats['n_assumption_due']} 条，"
              f"confirmed {stats['n_assumption_confirmed']} / "
              f"refuted {stats['n_assumption_refuted']} / "
              f"unverifiable {stats['n_assumption_unverifiable']} / "
              f"暂不判 {stats['n_assumption_deferred']} 条，"
              f"写策略草案 {stats['n_policy_drafted']} 条")

    if dry_run:
        print(f"[收敛] dry-run：未写库。将填 {stats['n_filled']} 条"
              f"（correct {stats['n_correct']} / wrong {stats['n_wrong']} / "
              f"unverifiable {stats['n_unverifiable']}），"
              f"暂不判 {stats['n_deferred']} 条；"
              f"质疑将判 {stats['n_objection_judged']} 条，暂不判 {stats['n_objection_deferred']} 条；"
              f"假设将判 {stats['n_assumption_due'] - stats['n_assumption_deferred']} 条，"
              f"将写策略草案 {stats['n_policy_drafted']} 条")
        return {"ok": True, "dry_run": True, "as_of": today, **stats,
                "rows": rows_out, "assumptions": assumption_rows}

    conn.commit()
    finished = datetime.now(CST).isoformat(timespec="seconds")
    err = stats.get("error")
    # 模型整体不可用时 status 记 degraded 而不是 ok：
    # 这一轮确实没做完，运维看 converge_run 就该一眼看出来要重跑，
    # 而不是看到 ok 就以为所有到期断言都处理过了。
    status = "degraded" if err else "ok"
    conn.execute(
        "insert into converge_run (started_at, finished_at, status, n_due, n_filled, "
        "n_correct, n_wrong, n_unverifiable, n_deferred, "
        "n_objection_due, n_objection_judged, model, error) "
        "values (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (started, finished, status, stats["n_due"], stats["n_filled"], stats["n_correct"],
         stats["n_wrong"], stats["n_unverifiable"], stats["n_deferred"],
         stats["n_objection_due"], stats["n_objection_judged"], _model_label(model), err),
    )
    conn.commit()
    print(f"[收敛] 完成：到期 {stats['n_due']} 条，回填 {stats['n_filled']} 条"
          f"（correct {stats['n_correct']} / wrong {stats['n_wrong']} / "
          f"unverifiable {stats['n_unverifiable']}），暂不判 {stats['n_deferred']} 条"
          f"（数据未追平，保持 pending 等下一轮）；"
          f"质疑到期 {stats['n_objection_due']} 条，判定 {stats['n_objection_judged']} 条，"
          f"暂不判 {stats['n_objection_deferred']} 条；模型调用 {llm_used} 次"
          + (f"，状态 {status}" if err else ""))
    return {"ok": True, "as_of": today, **stats, "model": model,
            "rows": rows_out, "assumptions": assumption_rows}


def main() -> int:
    ap = argparse.ArgumentParser(
        description="把到期的可验证断言（agent_eval）和质疑（agent_objection）回填成判定结果"
    )
    ap.add_argument("--as-of", dest="as_of", default=None,
                    help="把「今天」覆盖成指定日期 YYYY-MM-DD（所有断言到期日都在未来，调试时用）")
    ap.add_argument("--model", default=None, help=f"模型名，默认 {gateway.DEFAULT_MODEL}")
    ap.add_argument("--limit", type=int, default=None, help="本轮最多处理多少条")
    ap.add_argument("--dry-run", action="store_true", help="只打印将填的判定，不写库")
    ap.add_argument("--no-llm", action="store_true", help="关闭模型兜底，只跑确定性判定器")
    # 质疑和断言是两条独立的队：给质疑单独一条定时任务时，
    # 不该顺带把 agent_eval 那半边也跑一遍（会白烧模型调用，
    # 还会让两边的时间窗互相干扰）。所以拆成两个开关。
    ap.add_argument("--judge-objections", dest="judge_objections_only", action="store_true",
                    help="只判 agent_objection 的质疑（outcome），不碰 agent_eval 的断言")
    ap.add_argument("--no-objections", dest="no_objections", action="store_true",
                    help="不判质疑，只跑断言回填")
    # 假设这半边是唯一会往 policy 写东西的一支，必须能单独关掉：
    # 表结构还在变（reason_hit.skip_ratio 就没建）时，先关掉它跑其余的，
    # 比让一条写错策略的草案混进策略页要便宜得多。
    ap.add_argument("--no-assumptions", dest="no_assumptions", action="store_true",
                    help="不判默认假设（agent_assumption），不写 policy 草案")
    ap.add_argument("--assumptions-only", dest="assumptions_only", action="store_true",
                    help="只判默认假设，不碰 agent_eval 断言与 agent_objection 质疑")
    ap.add_argument("--revive-only", dest="revive_only", action="store_true",
                    help="只把「因缺证据被判 unverifiable、而该证据源现在已有数据」的断言放回 pending，不做判定")
    args = ap.parse_args()

    conn = context.connect()
    if args.revive_only:
        try:
            got = revive_unverifiable(conn, dry_run=args.dry_run)
            for r in got:
                print(f"  #{r['eval_id']:<4} 命中「{r['fragment']}」，证据源现有 {r['source_rows']} 行"
                      + ("（dry-run，未写库）" if args.dry_run else " → 已放回 pending"))
            print(f"[收敛] 复活 {len(got)} 条")
        finally:
            conn.close()
        return 0
    try:
        res = run_once(
            conn,
            as_of=args.as_of,
            model=args.model,
            limit=args.limit,
            dry_run=args.dry_run,
            use_llm=not args.no_llm,
            judge_eval=not (args.judge_objections_only or args.assumptions_only),
            with_objections=not (args.no_objections or args.assumptions_only),
            with_assumptions=not args.no_assumptions,
        )
    finally:
        conn.close()

    if not res.get("ok"):
        print(f"[收敛] 失败：{res.get('error')}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
