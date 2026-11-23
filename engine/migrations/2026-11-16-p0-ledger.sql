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
    created_at     TEXT NOT NULL
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
