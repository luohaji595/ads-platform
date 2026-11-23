"""
配置快照与变更检测（L0 层）

积加是唯一事实源，这里不做任何手工记录。全部数据从 ingest 抓来的报表里推出来。

为什么要快照，而不是直接看积加的报表：
    积加的报表告诉你「今天这个关键词出价是 $1.00」，
    但不告诉你「昨天是多少」。没有昨天，就没有「变了没有」，
    也就没法回答「我上周降了 bid，后来怎样了」。
    所以每次抓取存一份配置快照，靠 diff 得到变更流水。


一、为什么身份是「观测时刻」而不是「日期」（2026-11-18 修）
------------------------------------------------------------
旧实现把 snap_date 当作唯一键，而 snap_date 取的是报表日期。结果：
  - 同一天被重拉几次（T+1 那次 + --recheck-days 3 的滚动重拉），
    后一次会把前一次的快照原地覆盖 —— 前一个观测点直接消失；
  - 于是「日内改过出价」这件事在库里没有任何痕迹。

真实事故（这就是这次要修的 bug）：
    campaign 175723193023274 的 `lat pulldown`（keyword 151878211767486）
    在 2026-11-14 把 bid 从 1.0 改成 0.8。
    config_change_log 里没有这条记录，因为 11-14 那天没有快照点，
    而 diff 只比较「相邻两次快照」——11-13 → 11-15 那次比较里，
    bid 全程是 1.0，中途的 1.0 → 0.8 被完全掩盖。

    根因不是「快照太少」，而是「快照的键选错了」：
    用报表日期当键，同一天的多次观测必然互相覆盖，
    观测点数量天然被压成「一天一个」，再多次抓取也留不下痕迹。

所以现在改成：
    config_snapshot 的主键 = (snap_ts, snap_date, entity_type, entity_id, field)
    snap_ts  = 这批配置被真正观测到的时刻（UTC，归一化到秒）
    snap_date = 这批配置取自哪一天的报表行（沿用旧口径，不破坏历史数据）
    diff 不再只比「最近两次」，而是把所有「相邻观测点」都比一遍。

二、为什么取 MAX(ingested_at) 作为观测时刻
------------------------------------------------------------
Amazon 报表语义：`bid` / `daily_budget` 是**拉取时的当前值**，
一次拉取会把同一个当前值盖到这次拉到的每一行报表日上。
实测佐证：2026-11-18 10:00~10:03 那轮重拉覆盖 11-14~11-17，
这四天的 bid 全部变成 0.8；而 11-14 14:38~14:49 回补的 11-01~11-13
全部是 1.0。也就是说「一次拉取 = 一个观测点」。

因此：
  - 观测时刻 = 这批数据最后一次被写进库的时刻 = MAX(ingested_at)；
    用 MAX 而不是 MIN，因为决定「现在值是多少」的是最后一次写入。
  - 只快照最新的 report_date 就够：同一次拉取里所有报表日携带同一套配置，
    多看几天只是把同一份配置重复存 N 遍（旧实现存了 7685 行，其中大量重复）。
  - 时刻从数据本身推导，不用 now()：这样引擎重跑不会造出假观测点，
    take_snapshot / diff_snapshots 天然幂等。

三、仍然做不到的事（诚实记录，不伪造）
------------------------------------------------------------
fact_*_daily 是 upsert-in-place，没有历史版本。所以「11-14 那天的观测」
已经被 11-18 10:00 的重拉覆盖掉了，无法回溯。
本次修复只能保证「从今以后每次抓取都留下一个观测点」，
不能把 11-14 的漏记补回成 11-14 的记录。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

# 要追踪的字段。加字段要谨慎：每多一个字段，快照体积线性增长，
# 而且只有「后台能改、改了会影响结果」的字段才值得追踪。
TRACKED = {
    "campaign": ["daily_budget", "campaign_state", "targeting_type"],
    "keyword": ["bid", "serving_status"],
    "placement": ["bid_adjustment", "bid_strategy"],
}

# 各实体对应的报表数据集（也是取观测时刻的来源表）
DATASET_OF = {
    "campaign": "campaign",
    "keyword": "keyword",
    "placement": "placement",
}

TABLE_OF = {
    "campaign": "fact_campaign_daily",
    "keyword": "fact_keyword_daily",
    "placement": "fact_placement_daily",
}

# 迁移前的旧行没有真实观测时刻，用「报表日 + T00:00:00Z」占位，
# 并把 legacy 标成 1，让 diff 知道「这个点只有日期粒度，不要拿它当新观测」。
LEGACY_MARK = 1


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_z(raw: str | None) -> str | None:
    """把库里各种来历的时间串统一成 UTC 的 ...Z 形式。

    为什么必须统一：ingest 写的是本地时间裸串（datetime.now().isoformat()，
    例如 2026-11-18T10:11:05，实际是 CST），而 run_log / detected_at 用的是
    UTC。两种混在一起排序会错位，观测点的先后就乱了。
    """
    if not raw:
        return None
    s = str(raw).strip()
    try:
        if s.endswith("Z"):
            dt = datetime.fromisoformat(s[:-1] + "+00:00")
        else:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                # 裸时间串按本地时间解释：ingest 就是这么写的
                dt = dt.astimezone()
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None


# ---------------------------------------------------------------- 表结构迁移

def ensure_snapshot_schema(conn: sqlite3.Connection) -> bool:
    """把 config_snapshot 从「按日期唯一」升级成「按观测时刻唯一」。

    SQLite 改不了主键，只能重建表。旧行原样搬过去，只补 snap_ts / legacy：
      snap_ts = snap_date || 'T00:00:00Z'（占位：旧行只有日期粒度）
      legacy  = 1

    幂等：已经有 snap_ts 列就直接返回。
    返回 True 表示这次真的迁移了。

    注：engine/schema_v2.sql 里的 DDL 还是旧的。那边用 CREATE TABLE IF NOT EXISTS，
    已有库不受影响；全新库第一次跑 take_snapshot 时会被这里自动升级成新结构。
    """
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(config_snapshot)")}
    if not cols:
        return False           # 表还不存在，交给 schema_v2.sql 建
    if "snap_ts" in cols:
        return False           # 已经是新结构

    conn.execute("ALTER TABLE config_snapshot RENAME TO _config_snapshot_legacy")
    conn.execute(
        """
        CREATE TABLE config_snapshot (
            snap_ts       TEXT    NOT NULL,   -- 观测时刻（UTC，归一化到秒）——真正的身份
            snap_date     TEXT    NOT NULL,   -- 这批配置取自哪一天的报表行（旧口径）
            legacy        INTEGER NOT NULL DEFAULT 0,  -- 1 = 迁移前的旧行，只有日期粒度
            entity_type   TEXT    NOT NULL,
            entity_id     TEXT    NOT NULL,
            market_id     INTEGER,
            campaign_id   TEXT,
            ad_group_id   TEXT,
            entity_label  TEXT,
            field         TEXT    NOT NULL,
            value         TEXT,
            captured_at   TEXT    NOT NULL,
            PRIMARY KEY (snap_ts, snap_date, entity_type, entity_id, field)
        )
        """
    )
    conn.execute(
        """
        INSERT INTO config_snapshot
            (snap_ts, snap_date, legacy, entity_type, entity_id, market_id,
             campaign_id, ad_group_id, entity_label, field, value, captured_at)
        SELECT snap_date || 'T00:00:00Z', snap_date, 1,
               entity_type, entity_id, market_id,
               campaign_id, ad_group_id, entity_label, field, value, captured_at
        FROM _config_snapshot_legacy
        """
    )
    # 先 DROP 旧表（连带它名下的 ix_snap_entity），再建索引，否则索引名冲突
    conn.execute("DROP TABLE _config_snapshot_legacy")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_snap_entity "
        "ON config_snapshot(entity_type, entity_id, snap_date)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_snap_ts ON config_snapshot(snap_ts)"
    )
    conn.commit()
    _ensure_diff_state(conn)
    return True


def _ensure_diff_state(conn: sqlite3.Connection) -> None:
    """建一张「哪些观测点对已经比过」的小表。

    为什么需要它：差异记录里零变更的对本来就没有行，靠「config_change_log
    里查得到这对」判断「算过了」会失效 —— 零变更的对每轮都会被重算一遍。
    现在只有十几个点，重算是浪费；一年后是几百上千个点，每轮引擎都要做
    上千次 JOIN。用一张状态表记下扫描进度，零变更的对也只算一次。

    单独建表而不是往 config_snapshot 里塞哨兵行：哨兵会污染快照的行数和
    entity 统计，也会被 diff 的 JOIN 扫到，得不偿失。
    """
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS config_diff_state (
            from_snap    TEXT    NOT NULL,   -- 起点观测时刻
            to_snap      TEXT    NOT NULL,   -- 终点观测时刻
            changes      INTEGER NOT NULL,   -- 这一对算出来几处差异（0 也要记）
            reliable     INTEGER NOT NULL,
            scanned_at   TEXT    NOT NULL,   -- 第一次算这一对的时刻
            PRIMARY KEY (from_snap, to_snap)
        )
        """
    )
    conn.commit()


# ---------------------------------------------------------------- 观测时刻

def _observation_stamps(conn: sqlite3.Connection, report_date: str) -> dict[str, str]:
    """这批报表里，各数据集最后一次被写进库的时刻（UTC ...Z）。

    ingested_at 是行级字段，一次拉取里同一个 report_date 的行共享同一个值，
    所以 MAX 就是「这批数据最近一次被刷新的时刻」= 观测时刻。
    """
    stamps: dict[str, str] = {}
    for et, table in TABLE_OF.items():
        row = conn.execute(
            f"SELECT MAX(ingested_at) AS t FROM {table} WHERE report_date = ?",
            (report_date,),
        ).fetchone()
        z = _utc_z(row["t"]) if row else None
        if z:
            stamps[et] = z
    return stamps


def _captured_at(conn: sqlite3.Connection, dataset: str, report_date: str) -> str:
    """这批 report_date 的数据，最早是哪一次 ingest 抓进来的（沿用旧口径）。

    现在只用来填 captured_at（给人看的原始抓取时间）和判定 reliable，
    观测点的身份已经改用 snap_ts，不再依赖它。

    注意 ingest 记录区间的口径不统一：范围回补的 run 记 (11-01, 11-13)，
    单日补拉的 run 记 (11-13, 11-13)。所以这里用 >= 兼容两种口径，
    取「最早可能观测到这批数据的时刻」。宁可早判（更保守地判为不可比较），
    也不能把同一时刻抓的两份数据当成两个观测点。
    """
    row = conn.execute(
        """
        SELECT MIN(started_at) AS t
        FROM run_log
        WHERE dataset = ?
          AND status = 'ok'
          AND start_date <= ?
          AND end_date   >= ?
        """,
        (dataset, report_date, report_date),
    ).fetchone()
    if row and row["t"]:
        return row["t"]
    # 查不到就说明这批数据来历不明，标成 unknown 而不是假装知道
    return "unknown"


# ---------------------------------------------------------------- 快照

def take_snapshot(conn: sqlite3.Connection, snap_date: str | None = None) -> dict:
    """把当前配置状态写成**一个观测点**。

    幂等：观测时刻从数据本身推导（MAX(ingested_at)），同一批数据重复跑
    只会命中「这个点已经存在」，不会新增行，也不会刷新 captured_at。

    返回 rows = 这个观测点里的行数（重跑时也返回同一个数，方便对比两次运行）。
    """
    ensure_snapshot_schema(conn)

    if snap_date is None:
        row = conn.execute(
            """
            SELECT MAX(d) AS d FROM (
                SELECT MAX(report_date) AS d FROM fact_campaign_daily
                UNION ALL SELECT MAX(report_date) FROM fact_keyword_daily
                UNION ALL SELECT MAX(report_date) FROM fact_placement_daily
            )
            """
        ).fetchone()
        snap_date = row["d"] if row else None
    if not snap_date:
        return {"snap_date": None, "snap_ts": None, "rows": 0, "note": "库里没有数据"}

    stamps = _observation_stamps(conn, snap_date)
    if not stamps:
        return {"snap_date": snap_date, "snap_ts": None, "rows": 0,
                "note": f"{snap_date} 没有事实行"}
    # 一次 ingest 会按 dataset 顺序跑，各数据集写入时刻不同（实测相差几分钟）。
    # 取最大值作为这一个观测点的时刻：它就是「这批数据最后落定的那一刻」。
    snap_ts = max(stamps.values())

    already = conn.execute(
        "SELECT COUNT(*) AS n FROM config_snapshot WHERE snap_ts = ?", (snap_ts,)
    ).fetchone()["n"]
    if already:
        # 这批数据已经快照过。不能重新写一遍 —— 重写会把 captured_at 刷成新值，
        # 让「同一个观测点」看起来像两个不同时刻，reliable 判定就失真了。
        return {"snap_date": snap_date, "snap_ts": snap_ts, "rows": already,
                "written": 0, "note": "这批观测已经快照过，跳过"}

    # (entity_type, entity_id, market_id, campaign_id, ad_group_id, label, field, value)
    rows: list[tuple] = []

    # --- campaign
    for r in conn.execute(
        "SELECT * FROM fact_campaign_daily WHERE report_date = ?", (snap_date,)
    ):
        for field in TRACKED["campaign"]:
            rows.append((
                "campaign", r["campaign_id"], r["market_id"],
                r["campaign_id"], None, r["campaign_name"], field,
                None if r[field] is None else str(r[field]),
            ))

    # --- keyword（关键词没有独立的 ad_group 维度表，ad_group_id 从事实表带）
    for r in conn.execute(
        "SELECT * FROM fact_keyword_daily WHERE report_date = ?", (snap_date,)
    ):
        for field in TRACKED["keyword"]:
            rows.append((
                "keyword", r["keyword_id"], r["market_id"],
                r["campaign_id"], r["ad_group_id"], r["keyword_text"], field,
                None if r[field] is None else str(r[field]),
            ))

    # --- placement（实体 id 用 campaign_id + placement 组合）
    for r in conn.execute(
        "SELECT * FROM fact_placement_daily WHERE report_date = ?", (snap_date,)
    ):
        for field in TRACKED["placement"]:
            rows.append((
                "placement", f"{r['campaign_id']}::{r['placement']}", r["market_id"],
                r["campaign_id"], None, r["placement"], field,
                None if r[field] is None else str(r[field]),
            ))

    # 每个实体类型的 captured_at 用「这批报表最早是哪次 ingest 抓的」，
    # 不能用 now —— now 会让所有回补的数据看起来都是刚刚抓的，失去比较意义
    cap = {et: _captured_at(conn, DATASET_OF[et], snap_date) for et in TRACKED}

    payload = [
        (snap_ts, snap_date, 0, et, entity_id, market_id, campaign_id,
         ad_group_id, label, field, value, cap[et])
        for (et, entity_id, market_id, campaign_id, ad_group_id, label, field, value) in rows
    ]

    conn.executemany(
        """
        INSERT INTO config_snapshot
            (snap_ts, snap_date, legacy, entity_type, entity_id, market_id,
             campaign_id, ad_group_id, entity_label, field, value, captured_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(snap_ts, snap_date, entity_type, entity_id, field)
        DO UPDATE SET value = excluded.value
        """,
        payload,
    )
    conn.commit()
    return {"snap_date": snap_date, "snap_ts": snap_ts, "rows": len(payload),
            "written": len(payload), "captured_at": cap}


# ---------------------------------------------------------------- 变更检测

def _points(conn: sqlite3.Connection) -> list[dict]:
    """所有观测点，按观测时刻升序。

    legacy 取 MAX：一个 snap_ts 下混了新旧行是不可能的（旧行全是 legacy=1），
    这里取 MAX 只是为了让 GROUP BY 有个确定值。
    """
    return [dict(r) for r in conn.execute(
        """
        SELECT snap_ts,
               MAX(snap_date)  AS snap_date,
               MAX(legacy)     AS legacy,
               MAX(captured_at) AS captured_at
        FROM config_snapshot
        GROUP BY snap_ts
        ORDER BY snap_ts
        """
    )]


def _log_key(point: dict) -> str:
    """一个观测点在 config_change_log 里该用什么键。

    迁移前的旧点没有真实观测时刻，snap_ts 是造出来的 snap_date||'T00:00:00Z'。
    如果拿它当键，旧行（键是纯日期 '2026-11-13'）就永远匹配不上：
    DELETE ... WHERE from_snap=? AND to_snap=? 删不掉它们，INSERT 又会插一份
    新的，同一批变更在流水里出现两遍（实测：11-13→11-15 的 6 条变成 12 条）。
    所以旧点的键退回日期字符串，和历史记录保持同一个口径 —— 新旧记录因此
    能共存、互不重复，现有按日期查这两列的查询也不会炸。
    """
    return point["snap_date"] if point.get("legacy") else point["snap_ts"]


def _diff_pair(conn: sqlite3.Connection, a: dict, b: dict) -> dict:
    """比较两个观测点，把差异写进 config_change_log（先删后插，幂等）。"""
    # 这里有两套坐标，别混：
    #   a_ts / b_ts   —— 观测点在 config_snapshot 里的真实身份（JOIN 用它）
    #   from_snap / to_snap —— 流水键（config_change_log 用它，旧点退回日期）
    # 混用的后果实测过：拿流水键去 JOIN snap_ts，旧点那一段永远查出 0 行，
    # 于是「重算 11-13→11-15」把 6 条历史变更删掉后插不回来。
    a_ts, b_ts = a["snap_ts"], b["snap_ts"]
    from_snap, to_snap = _log_key(a), _log_key(b)

    # 这次比较可不可信，取决于「两个点是不是两次独立的观测」。
    #
    # 旧判据比的是 captured_at（run_log 反推的抓取时刻）。那套判据在
    # 「一天一个点」时勉强成立，但一旦允许同一天多个点就必然失效：
    #   _captured_at 取的是 MIN(started_at)，而重拉同一个 report_date 时
    #   run_log 只新增行、旧行不动，MIN 永远指向最早那一次拉取。
    #   结果 captured_from 恒等于 captured_to，每一次真实重拉都被误判成
    #   「同一时刻抓取」→ reliable 永远是 0 → snapshot_history 只返回
    #   reliable=1，日内变更照样查不到。等于白改。
    #
    # 现在身份已经是 snap_ts（= MAX(ingested_at)，真实落库时刻），
    # 「两个点 snap_ts 不同」本身就证明数据在两个不同时刻被重写过。
    #
    # 但 snap_ts 对迁移前的旧点是个造出来的占位值（snap_date||'T00:00:00Z'），
    # 拿它当证据不成立。所以这里不按点类型分派，而是统一问一句：
    # 这两个点各自「真实的观测时刻」是什么？—— 新点就是 snap_ts，
    # 旧点退回 captured_at（run_log 反推的抓取时刻，对旧点仍然有效）。
    # 两个真实时刻都存在、都不是 unknown、且不相等，才算两次独立观测。
    #
    # 这样写的好处是：旧点对新点（迁移那天的比较）也走同一套逻辑，
    # 不用为「混合」再开一个分支。
    captured_from, captured_to = a["captured_at"], b["captured_at"]
    unknown = "unknown" in (captured_from or "", captured_to or "")
    real_from = captured_from if a.get("legacy") else a_ts
    real_to = captured_to if b.get("legacy") else b_ts
    reliable = 1 if (real_from and real_to and real_from != real_to
                     and not unknown) else 0

    rows = conn.execute(
        """
        SELECT a.entity_type, a.entity_id, a.entity_label, a.campaign_id, a.field,
               a.value AS old_value, b.value AS new_value
        FROM config_snapshot a
        JOIN config_snapshot b
          ON a.entity_type = b.entity_type
         AND a.entity_id   = b.entity_id
         AND a.field       = b.field
        WHERE a.snap_ts = ? AND b.snap_ts = ?
          AND COALESCE(a.value,'') <> COALESCE(b.value,'')
        """,
        (a_ts, b_ts),
    ).fetchall()

    # note 要区分「为什么不可信」：两种情况的可信度差得很远，
    # 混成一句话会让后来读流水的人没法判断这条记录能用到什么程度。
    if reliable:
        note = None
    elif a.get("legacy"):
        note = ("起点是迁移前的旧快照，只有日期粒度、没有真实观测时刻；"
                "只能证明「这段时间里变过」，定位不到具体是哪天")
    else:
        note = ("这批数据的抓取时刻不明（run_log 里找不到覆盖该报表日的成功拉取），"
                "无法证明中间真的变过，仅作记录")

    # 同一对观测点只应有一份差异记录。引擎重跑 N 次就会把同一批差异插 N 遍。
    # 实测过：819 行里 789 行是重复的，一条 `keyword ... bid 1.2→1.0` 被插了 44 遍。
    # 先删后插，让这个函数变成幂等的。
    conn.execute(
        "DELETE FROM config_change_log WHERE from_snap=? AND to_snap=?",
        (from_snap, to_snap),
    )
    now = _now()
    conn.executemany(
        """
        INSERT INTO config_change_log
            (entity_type, entity_id, entity_label, campaign_id, field,
             old_value, new_value, from_snap, to_snap,
             captured_from, captured_to, reliable, detected_at, source, note)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        [
            (r["entity_type"], r["entity_id"], r["entity_label"], r["campaign_id"],
             r["field"], r["old_value"], r["new_value"], from_snap, to_snap,
             captured_from, captured_to, reliable, now, "jijia_snapshot_diff", note)
            for r in rows
        ],
    )
    # 记下「这一对比过了」，零变更的对也一样记 —— 否则下一轮还会重算
    conn.execute(
        """
        INSERT INTO config_diff_state (from_snap, to_snap, changes, reliable, scanned_at)
        VALUES (?,?,?,?,?)
        ON CONFLICT(from_snap, to_snap)
        DO UPDATE SET changes = excluded.changes, reliable = excluded.reliable
        """,
        (a_ts, b_ts, len(rows), reliable, now),
    )
    return {"from_snap": from_snap, "to_snap": to_snap, "changes": len(rows),
            "reliable": reliable, "captured_from": captured_from,
            "captured_to": captured_to}


def diff_snapshots(conn: sqlite3.Connection, from_snap: str | None = None,
                   to_snap: str | None = None) -> dict:
    """把所有「相邻观测点」的差异写进 config_change_log。

    和旧版的区别（这就是修 bug 的地方）：
      旧版只比较「最近两次快照」。一天只落一个点的时候，日内改了又改回去，
      两次快照看起来一模一样，变更就永久丢了。
      现在按观测时刻排序，把所有相邻点都比一遍 —— 同一天内的多个观测点
      也会各自比较一次，日内变更因此能落进流水。

    只往前算「还没算过的对」：已经算过的对（config_diff_state 里登记过）
    直接跳过，这样 detected_at 保留首次发现的时间，不会每次重跑都被刷成
    当前时间。迁移前的旧点对也照样算 —— 旧版只比「最近两次」，
    11-01→11-12 这些历史对从来没算过，现在能补出来。

    返回的 from_snap/to_snap 是最新一对观测点，changes 是这一对在流水里的
    实际条数（不是本次新插入的条数），这样引擎连跑两次报告文字一致。
    """
    ensure_snapshot_schema(conn)
    _ensure_diff_state(conn)

    if from_snap and to_snap:
        # 手工指定：强制重算这一对
        pts = {p["snap_ts"]: p for p in _points(conn)}
        if from_snap not in pts or to_snap not in pts:
            return {"changes": 0, "note": "指定的观测点不存在",
                    "from_snap": from_snap, "to_snap": to_snap}
        info = _diff_pair(conn, pts[from_snap], pts[to_snap])
        conn.commit()
        return {**info, "pairs_new": 1, "changes_written": info["changes"]}

    points = _points(conn)
    if len(points) < 2:
        return {"changes": 0, "note": "只有一份快照，无从比较",
                "from_snap": points[0]["snap_ts"] if points else None,
                "to_snap": points[0]["snap_ts"] if points else None}

    written = 0
    pairs_new = 0
    for a, b in zip(points, points[1:]):
        # 这里不跳过 legacy 点：旧版只比较「最近两次」，11-01→11-12 这些
        # 历史点对从来没算过。现在按观测时刻把所有相邻点扫一遍，历史上的
        # 变更也能补出来。补不回来的只有「观测本身已被覆盖」的那种（见模块
        # 文档里 11-14 的例子）—— 那不是少算了一对，是数据没留下来。
        # 用状态表判断「算过了」而不是查流水：零变更的对流水里没有行，
        # 靠流水判断会让它们每轮重算（见 _ensure_diff_state 的说明）。
        done = conn.execute(
            "SELECT 1 FROM config_diff_state WHERE from_snap=? AND to_snap=? LIMIT 1",
            (a["snap_ts"], b["snap_ts"]),
        ).fetchone()
        if done:
            continue                      # 算过了，保住 detected_at
        info = _diff_pair(conn, a, b)
        written += info["changes"]
        pairs_new += 1

    conn.commit()

    # 报最新一对的结论。注意不能从 config_change_log 统计：
    # 零变更的对在流水里没有行，count 是 0，reliable 就会被报成 0，
    # 于是引擎打印「同一时刻抓取，仅记录」—— 和数据里记的可信结论矛盾。
    # 结论以 config_diff_state 为准（每一对都登记了 changes 和 reliable），
    # 流水里的条数再叠加到 changes 上。
    last_a, last_b = points[-2], points[-1]
    last_from, last_to = _log_key(last_a), _log_key(last_b)
    state = conn.execute(
        "SELECT changes, reliable FROM config_diff_state WHERE from_snap=? AND to_snap=?",
        (last_a["snap_ts"], last_b["snap_ts"]),
    ).fetchone()
    logged = conn.execute(
        "SELECT COUNT(*) AS n FROM config_change_log WHERE from_snap=? AND to_snap=?",
        (last_from, last_to),
    ).fetchone()
    return {
        "from_snap": last_from,
        "to_snap": last_to,
        "changes": logged["n"] or 0,
        "reliable": state["reliable"] if state else 0,
        "pairs_scanned": len(points) - 1,
        "pairs_new": pairs_new,
        "changes_written": written,
    }


def snapshot_history(conn: sqlite3.Connection, entity_type: str | None = None,
                     entity_id: str | None = None, limit: int = 50) -> list[dict]:
    """某个对象过去发生过哪些配置变更（只返回可信的）。"""
    sql = "SELECT * FROM config_change_log WHERE reliable = 1"
    args: list = []
    if entity_type:
        sql += " AND entity_type = ?"
        args.append(entity_type)
    if entity_id:
        sql += " AND entity_id = ?"
        args.append(entity_id)
    # 加 change_id 兜底排序：同一秒内检测到的多条记录顺序要稳定，
    # 否则同一份数据两次查询给出的顺序可能不同
    sql += " ORDER BY detected_at DESC, change_id DESC LIMIT ?"
    args.append(limit)
    return [dict(r) for r in conn.execute(sql, args)]
