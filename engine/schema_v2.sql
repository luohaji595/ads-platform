-- ============================================================
-- 模块3 规则引擎 · 数据层 v2
-- 叠加在 ingest/schema.sql 的 facts 之上，不改动原表。
--
-- 三条设计原则：
--   1. 积加是唯一事实源。事实表(fact_*)只读，本文件只新增"我们的判断"。
--   2. 任何变更都必须可 diff 出来 → config_snapshot 每天一份全量配置快照。
--   3. 任何建议都必须可复算 → calculation_json 存完整计算链，不是结论。
-- ============================================================

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ------------------------------------------------------------
-- 1. 配置快照：每天一份，用于 diff 出"谁改了什么"
--    bid / daily_budget / state 这些字段在积加报表里是快照值，
--    只有每天拉一次、逐日对比，才能得到真实的变更历史。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS config_snapshot (
    snap_date     TEXT    NOT NULL,          -- 观测日（不是报表日）
    entity_type   TEXT    NOT NULL,          -- keyword | campaign | adgroup | target
    entity_id     TEXT    NOT NULL,
    market_id     INTEGER,
    campaign_id   TEXT,
    ad_group_id   TEXT,
    entity_label  TEXT,
    field         TEXT    NOT NULL,          -- bid | daily_budget | state | placement_tos | placement_pp | placement_ros
    value         TEXT,
    captured_at   TEXT    NOT NULL,
    PRIMARY KEY (snap_date, entity_type, entity_id, field)
);
CREATE INDEX IF NOT EXISTS ix_snap_entity ON config_snapshot(entity_type, entity_id, snap_date);

-- ------------------------------------------------------------
-- 2. 变更事件：由 config_snapshot 两两 diff 自动生成，零人工录入
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS config_change_log (
    change_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type       TEXT NOT NULL,
    entity_id         TEXT NOT NULL,
    entity_label      TEXT,
    campaign_id       TEXT,
    field             TEXT NOT NULL,
    old_value         TEXT,
    new_value         TEXT,
    from_snap         TEXT NOT NULL,
    to_snap           TEXT NOT NULL,
    captured_from     TEXT,                  -- 旧快照的实际抓取时刻
    captured_to       TEXT,                  -- 新快照的实际抓取时刻
    reliable          INTEGER NOT NULL DEFAULT 1,  -- 0 = 两次快照同一时刻抓的，无法证明中间真的变过
    detected_at       TEXT NOT NULL,
    source            TEXT NOT NULL DEFAULT 'jijia_snapshot_diff',
    matched_decision_id INTEGER,             -- 反向关联：这次变更是不是我们的建议被执行了
    note              TEXT
);
CREATE INDEX IF NOT EXISTS ix_change_entity ON config_change_log(entity_type, entity_id, to_snap);

-- ------------------------------------------------------------
-- 3. 决策账本：我们建议了什么、为什么、后来怎样了
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS decision_log (
    decision_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id            INTEGER,
    decided_at        TEXT NOT NULL,
    rule_id           TEXT NOT NULL,
    rule_version      TEXT NOT NULL DEFAULT '1.0',
    entity_type       TEXT NOT NULL,          -- keyword | campaign | search_term | placement | account
    entity_id         TEXT NOT NULL,
    entity_label      TEXT,
    market_id         INTEGER,
    campaign_id       TEXT,
    ad_group_id       TEXT,
    action_type       TEXT NOT NULL,          -- reduce_bid | raise_bid | add_negative | harvest | raise_budget | lower_budget | adjust_placement | pause | observe | alert
    current_value     TEXT,
    proposed_value    TEXT,
    confidence        TEXT NOT NULL,          -- low | medium | high
    -- 上下文：决策当时的全貌，用于日后"同类案例检索"
    context_json      TEXT NOT NULL,
    -- 计算链：每一步的公式与代入值，可复算
    calculation_json  TEXT NOT NULL,
    -- 证据强度：样本量、显著性与否、为什么够/不够
    significance_json TEXT NOT NULL,
    rationale         TEXT,
    spend_impact      REAL,                   -- 影响的花费（正=预计省下，负=预计多花）
    -- 生命周期
    status            TEXT NOT NULL DEFAULT 'proposed',
                      -- proposed | observing | approved | rejected | executed | expired | rolled_back | floor_breach
                      -- observing = 引擎算过、但被置信度/证据按住，不进待批队列；
                      -- 人可在工作台显式二次确认后 override 成 approved（留 override 来源）
    executed_at       TEXT,
    executed_value    TEXT,
    -- 结果回填（从积加回读，非人工）
    outcome_7d_json   TEXT,
    outcome_14d_json  TEXT,
    outcome_filled_at TEXT,
    -- 保护
    cooldown_until    TEXT,
    expires_at        TEXT,
    ticket_id         TEXT,
    -- P0 2026-11-16 审批落点
    checked_at        TEXT,
    checked_by        TEXT,
    reject_reason     TEXT,
    reject_reason_code TEXT,
    -- S1 2026-11-19 证据闸门与人工强制批准
    -- gate_reason 只由引擎写：人话说明「为什么按住这条」。工作台写白名单不含此列，
    -- 所以引擎的诚实判断不会被审批侧改写。
    gate_reason       TEXT,
    -- override_* 只在 status='observing' 的条目被显式二次确认时写。
    -- 引擎照旧说证据不够，但人的决定权与来源一起留痕，审计时能分清「证据够」和「人拍的」。
    override_by       TEXT,
    override_reason   TEXT,
    overridden_at     TEXT
);
CREATE INDEX IF NOT EXISTS ix_decision_rule   ON decision_log(rule_id, decided_at);
CREATE INDEX IF NOT EXISTS ix_decision_entity ON decision_log(entity_type, entity_id, decided_at);
CREATE INDEX IF NOT EXISTS ix_decision_status ON decision_log(status);

-- ------------------------------------------------------------
-- 4. 工单：一次运行产出的建议集合，供人工一次批准/否决
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ticket (
    ticket_id         TEXT PRIMARY KEY,
    run_id            INTEGER,
    created_at        TEXT NOT NULL,
    title             TEXT,
    status            TEXT NOT NULL DEFAULT 'pending',
                      -- pending | approved | rejected | partially_executed | executed | expired
    rule_count        INTEGER DEFAULT 0,
    decision_count    INTEGER DEFAULT 0,
    spend_impact      REAL,
    summary           TEXT,
    approved_at       TEXT,
    approved_by       TEXT
);

-- ------------------------------------------------------------
-- 5. 运行记录
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS engine_run (
    run_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    window_start  TEXT,
    window_end    TEXT,
    status        TEXT NOT NULL DEFAULT 'running',   -- running | ok | failed
    rules_enabled TEXT,
    decisions     INTEGER DEFAULT 0,
    suppressed    INTEGER DEFAULT 0,
    error         TEXT,
    note          TEXT
);

-- ------------------------------------------------------------
-- 6. 视图：给报表直接用
-- ------------------------------------------------------------
DROP VIEW IF EXISTS v_open_decisions;
CREATE VIEW v_open_decisions AS
SELECT d.*, t.title AS ticket_title
FROM decision_log d
LEFT JOIN ticket t ON t.ticket_id = d.ticket_id
WHERE d.status IN ('proposed', 'approved')
ORDER BY
    CASE d.confidence WHEN 'high' THEN 0 WHEN 'medium' THEN 1 ELSE 2 END,
    ABS(COALESCE(d.spend_impact, 0)) DESC;


-- ------------------------------------------------------------
-- P0 2026-11-16：沉淀表与事件收件箱（见 engine/migrations/2026-11-16-p0-ledger.sql）
-- ------------------------------------------------------------

-- P0 · 2026-11-16 Asia/Shanghai
-- 在现有 ads.sqlite3 上补审批字段、8 张沉淀表、事件收件箱。
-- 不改 fact_* 表。不迁移事实数据。
-- 本文件可重复执行：列和表都按“已有则跳过”处理（由 migrate_p0.py 执行）。

-- decision_log 审批字段（SQLite 无 IF NOT EXISTS 加列，交脚本判断）
-- ALTER TABLE decision_log ADD COLUMN checked_at TEXT;
-- ALTER TABLE decision_log ADD COLUMN checked_by TEXT;
-- ALTER TABLE decision_log ADD COLUMN reject_reason TEXT;
-- ALTER TABLE decision_log ADD COLUMN reject_reason_code TEXT;

CREATE TABLE IF NOT EXISTS policy (
    policy_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    scope          TEXT NOT NULL,
    object_id      TEXT NOT NULL,
    policy_type    TEXT NOT NULL,
    params_json    TEXT,
    source         TEXT NOT NULL,
    evidence_text  TEXT,
    status         TEXT NOT NULL DEFAULT 'active',
    created_at     TEXT NOT NULL,
    expires_at     TEXT,
    reviewed_at    TEXT
);
CREATE INDEX IF NOT EXISTS ix_policy_object ON policy(object_id, status, expires_at);

CREATE TABLE IF NOT EXISTS param_override (
    override_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    scope          TEXT NOT NULL,
    object_id      TEXT NOT NULL,
    param_key      TEXT NOT NULL,
    param_value    TEXT NOT NULL,
    source         TEXT NOT NULL,
    evidence_text  TEXT,
    created_at     TEXT NOT NULL,
    expires_at     TEXT
);
CREATE INDEX IF NOT EXISTS ix_param_object ON param_override(object_id, param_key);

CREATE TABLE IF NOT EXISTS observation_task (
    task_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id      TEXT NOT NULL,
    reason_code    TEXT NOT NULL,
    hypothesis     TEXT NOT NULL,
    metric         TEXT NOT NULL,
    judge_rule     TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    due_at         TEXT NOT NULL,
    result         TEXT,
    resolved_at    TEXT
);
CREATE INDEX IF NOT EXISTS ix_obs_object ON observation_task(object_id, due_at);

CREATE TABLE IF NOT EXISTS experiment (
    experiment_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id      TEXT NOT NULL,
    reason_code    TEXT NOT NULL,
    hypothesis     TEXT NOT NULL,
    change_json    TEXT NOT NULL,
    control_json   TEXT NOT NULL,
    metric         TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    ends_at        TEXT NOT NULL,
    result         TEXT,
    evidence_level TEXT
);

CREATE TABLE IF NOT EXISTS reason_hit (
    hit_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id      TEXT NOT NULL,
    reason_code    TEXT NOT NULL,
    scope          TEXT NOT NULL,
    evidence_text  TEXT,
    created_at     TEXT NOT NULL,
    -- 收敛 agent 的提权出口（计划书 §8.5）：该题被判 refuted 时 +0.2，封顶 1.0。
    -- 语义是「这题以后别再跳过了，跳过就是错」，不是「这题出现得多」。
    skip_ratio     REAL
);
CREATE INDEX IF NOT EXISTS ix_reason_hit ON reason_hit(object_id, reason_code, created_at);

CREATE TABLE IF NOT EXISTS reason_excluded (
    excluded_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id      TEXT NOT NULL,
    reason_code    TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    expires_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_reason_ex ON reason_excluded(object_id, reason_code);

CREATE TABLE IF NOT EXISTS agent_assumption (
    assumption_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    object_id      TEXT NOT NULL,
    question_code  TEXT NOT NULL,
    default_action TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    review_at      TEXT NOT NULL,
    result         TEXT
);

CREATE TABLE IF NOT EXISTS competitor_watchlist (
    asin           TEXT PRIMARY KEY,
    label          TEXT,
    watch_dims     TEXT NOT NULL,
    thresholds_json TEXT,
    active         INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS event_inbox (
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
CREATE INDEX IF NOT EXISTS ix_event_status ON event_inbox(status, occurred_at);
CREATE INDEX IF NOT EXISTS ix_event_type ON event_inbox(type, occurred_at);

-- ------------------------------------------------------------
-- 16. 卡位（R31）：配置 + 排名观测
--     优麦云式「由排名目标反推 bid」。我们不抓排名，只消费插件/上游写入的观测。
--     比优麦云多两个安全阀：acos_guard_mult（ACOS 闸）与 max_pushes（加价次数上限）。
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS rank_target (
    target_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id        INTEGER,
    keyword_id       TEXT NOT NULL,
    keyword_text     TEXT,
    campaign_id      TEXT,
    ad_group_id      TEXT,
    -- 卡位对象：卡的是哪一个 ASIN 的位次。
    -- 必须写明，否则同一关键词下自家多个 ASIN 出现时无法判断「挡住目标位的是不是自己人」。
    asin             TEXT,
    -- 目标位置：第 goal_page 页第 goal_position 位。
    -- 优麦云「卡到搜索结果顶部」= 第 1 页 1-4 位（已排除顶部品牌广告位）。
    goal_page        INTEGER NOT NULL DEFAULT 1,
    goal_position    INTEGER NOT NULL DEFAULT 4,
    -- 卡位方式：bid=提关键词竞价；tos=提搜索结果顶部加价（上限 900%，亚马逊规则）
    push_mode        TEXT NOT NULL DEFAULT 'bid',
    max_bid          REAL,                 -- 提竞价时必填，触顶即停
    max_tos_pct      REAL DEFAULT 900.0,
    -- 加价步长：fixed=固定金额，percent=按当前竞价百分比
    step_mode        TEXT NOT NULL DEFAULT 'fixed',
    step_value       REAL NOT NULL DEFAULT 0.10,
    -- 卡位成功后：连续 N 次成功则降价；decrease_mode 为空表示不降
    success_streak_required INTEGER NOT NULL DEFAULT 5,
    decrease_mode    TEXT,                 -- null | fixed | percent
    decrease_value   REAL,
    restore_on_end   INTEGER NOT NULL DEFAULT 1,
    -- 安全阀（优麦云没有这两条）
    acos_guard_mult  REAL NOT NULL DEFAULT 1.5,   -- 窗口 ACOS > target × 此倍数 → 停卡
    max_pushes       INTEGER NOT NULL DEFAULT 6,  -- 累计加价次数上限
    -- 生命周期
    status           TEXT NOT NULL DEFAULT 'active',  -- active | paused | ended
    created_at       TEXT NOT NULL,
    expires_at       TEXT
);
CREATE INDEX IF NOT EXISTS ix_rank_target_kw ON rank_target(keyword_id, status);

CREATE TABLE IF NOT EXISTS rank_snapshot (
    snapshot_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id     INTEGER,
    keyword_id    TEXT NOT NULL,
    keyword_text  TEXT,
    observed_at   TEXT NOT NULL,
    placement     TEXT NOT NULL DEFAULT 'sponsored',  -- organic | sponsored
    page          INTEGER,
    position      INTEGER,
    asin          TEXT,                               -- 该位次上出现的 ASIN
    is_self       INTEGER NOT NULL DEFAULT 0,         -- 是否自己家的 ASIN（自我竞争检测）
    -- 一次抓取会为同一个关键词写入多行（每个位次一行），observed_at 相同即为同一批次。
    -- 引擎按批次取「自己的位次」，不按单行取，否则会把自己的另一条记录当成当前排名。
    postal_code   TEXT,                               -- 抓取邮编（不同地区结果不同）
    source        TEXT,                               -- plugin | manual | api
    raw_json      TEXT
);
CREATE INDEX IF NOT EXISTS ix_rank_snap_kw ON rank_snapshot(keyword_id, observed_at);
CREATE INDEX IF NOT EXISTS ix_rank_snap_time ON rank_snapshot(observed_at);
