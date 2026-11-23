-- 中台留痕。event_inbox 已在 P0 建好，这里只补派单和待送达。
-- 可重复执行。

CREATE TABLE IF NOT EXISTS dispatch_log (
    dispatch_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id       INTEGER NOT NULL,
    rule_name      TEXT NOT NULL,
    assigned_to    TEXT NOT NULL,
    action         TEXT NOT NULL,
    tell           TEXT NOT NULL,
    status         TEXT NOT NULL,
    note           TEXT,
    backlink       TEXT,
    dispatched_at  TEXT NOT NULL,
    FOREIGN KEY (event_id) REFERENCES event_inbox(event_id)
);
CREATE INDEX IF NOT EXISTS ix_dispatch_event ON dispatch_log(event_id);
CREATE INDEX IF NOT EXISTS ix_dispatch_at ON dispatch_log(dispatched_at);

CREATE TABLE IF NOT EXISTS tell_outbox (
    outbox_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    dispatch_id    INTEGER,
    channel        TEXT NOT NULL,
    business_day   TEXT NOT NULL,
    action         TEXT NOT NULL,
    title          TEXT NOT NULL,
    body           TEXT,
    backlink       TEXT,
    status         TEXT NOT NULL DEFAULT 'pending',
    created_at     TEXT NOT NULL,
    delivered_at   TEXT,
    note           TEXT,
    UNIQUE(channel, business_day, action)
);
CREATE INDEX IF NOT EXISTS ix_outbox_status ON tell_outbox(status, business_day);
