-- 亚马逊广告自动化工作台 · 数据层 schema（模块2 落地验证版）
-- 设计要点（源自 04-模块2-数据存储.md + 积加实测）：
--   1. 幂等：每张事实表都有业务主键，重复拉取只更新不新增。
--   2. 可回溯：raw_json 保留 API 原始行，字段口径变了能回查。
--   3. 可审计：run_log 记录每次拉取的区间/行数/状态/耗时。
--   4. 无单指标存 NULL 不存 0（积加实测 acos/cpa 无单时为 null）。

PRAGMA journal_mode = WAL;

-- ---------- 元数据 ----------
CREATE TABLE IF NOT EXISTS dim_market (
    market_id              INTEGER PRIMARY KEY,
    market                 TEXT,
    country_name           TEXT,
    seller_id              TEXT,
    ads_state              TEXT,          -- Successfully = 广告授权正常
    auth_expire_date       TEXT,
    ads_report_period      TEXT,
    ads_report_init_begin  TEXT,          -- 广告报表可回溯起点
    timezone               TEXT,
    currency               TEXT,
    updated_at             TEXT NOT NULL
);

-- ---------- 事实表：广告活动（日 × campaign）----------
CREATE TABLE IF NOT EXISTS fact_campaign_daily (
    market_id          INTEGER NOT NULL,
    report_date        TEXT    NOT NULL,   -- 积加 createDate 实为报表日期
    campaign_id        TEXT    NOT NULL,
    campaign_name      TEXT,
    portfolio_id       TEXT,
    portfolio_name     TEXT,
    targeting_type     TEXT,
    campaign_state     TEXT,               -- API 字段 state
    serving_status     TEXT,
    daily_budget       REAL,
    impressions        INTEGER,
    clicks             INTEGER,
    cost               REAL,
    ads_sales          REAL,
    ads_orders         INTEGER,
    other_product_sales REAL,
    ads_product_sales  REAL,
    ctr                REAL,
    cpc                REAL,
    cvr                REAL,
    acos               REAL,               -- 无单为 NULL，不是 0
    roas               REAL,
    cpa                REAL,
    row_hash           TEXT,
    raw_json           TEXT,
    source             TEXT NOT NULL DEFAULT 'jijia',
    ingested_at        TEXT NOT NULL,
    PRIMARY KEY (market_id, report_date, campaign_id)
);

-- ---------- 事实表：关键词 / 定向（日 × keyword）----------
CREATE TABLE IF NOT EXISTS fact_keyword_daily (
    market_id          INTEGER NOT NULL,
    report_date        TEXT    NOT NULL,
    keyword_id         TEXT    NOT NULL,
    keyword_text       TEXT,
    match_type         TEXT,               -- 0=EXACT 1=PHRASE 2=BROAD（实测推断，待官方确认）
    ad_group_id        TEXT,
    ad_group_name      TEXT,
    campaign_id        TEXT,
    campaign_name      TEXT,
    bid                REAL,               -- 当前出价，改 bid 规则直接用
    serving_status     TEXT,
    impressions        INTEGER,
    clicks             INTEGER,
    cost               REAL,
    ads_sales          REAL,
    ads_orders         INTEGER,
    ctr                REAL,
    cpc                REAL,
    cvr                REAL,
    acos               REAL,
    roas               REAL,
    cpa                REAL,
    row_hash           TEXT,
    raw_json           TEXT,
    source             TEXT NOT NULL DEFAULT 'jijia',
    ingested_at        TEXT NOT NULL,
    PRIMARY KEY (market_id, report_date, keyword_id)
);

-- ---------- 事实表：搜索词（日 × campaign × ad group × keyword × query）----------
CREATE TABLE IF NOT EXISTS fact_search_term_daily (
    market_id          INTEGER NOT NULL,
    report_date        TEXT    NOT NULL,
    campaign_id        TEXT    NOT NULL DEFAULT '',
    ad_group_id        TEXT    NOT NULL DEFAULT '',
    keyword_id         TEXT    NOT NULL DEFAULT '',
    keyword_text       TEXT,
    match_type         TEXT,
    search_term        TEXT    NOT NULL DEFAULT '',   -- API 字段 query
    impressions        INTEGER,
    clicks             INTEGER,
    cost               REAL,
    ads_sales          REAL,
    ads_orders         INTEGER,
    ctr                REAL,
    cpc                REAL,
    cvr                REAL,
    acos               REAL,
    roas               REAL,
    cpa                REAL,
    row_hash           TEXT,
    raw_json           TEXT,
    source             TEXT NOT NULL DEFAULT 'jijia',
    ingested_at        TEXT NOT NULL,
    PRIMARY KEY (market_id, report_date, campaign_id, ad_group_id, keyword_id, search_term)
);

-- ---------- 事实表：广告位（日 × campaign × placement）----------
CREATE TABLE IF NOT EXISTS fact_placement_daily (
    market_id          INTEGER NOT NULL,
    report_date        TEXT    NOT NULL,
    campaign_id        TEXT    NOT NULL,
    placement          TEXT    NOT NULL DEFAULT '',
    bid_adjustment     REAL,
    bid_adjustment_name TEXT,
    bid_strategy       INTEGER,            -- dynamic_factor 的数据源
    bid_strategy_name  TEXT,
    impressions        INTEGER,
    clicks             INTEGER,
    cost               REAL,
    ads_sales          REAL,
    ads_orders         INTEGER,
    ctr                REAL,
    cpc                REAL,
    cvr                REAL,
    acos               REAL,
    roas               REAL,
    cpa                REAL,
    row_hash           TEXT,
    raw_json           TEXT,
    source             TEXT NOT NULL DEFAULT 'jijia',
    ingested_at        TEXT NOT NULL,
    PRIMARY KEY (market_id, report_date, campaign_id, placement)
);

-- ---------- 运行日志 ----------
CREATE TABLE IF NOT EXISTS run_log (
    run_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset       TEXT NOT NULL,
    market_id     INTEGER,
    start_date    TEXT,
    end_date      TEXT,
    pages         INTEGER DEFAULT 0,
    rows_fetched  INTEGER DEFAULT 0,
    rows_upserted INTEGER DEFAULT 0,
    status        TEXT NOT NULL,           -- running | ok | failed
    error         TEXT,
    started_at    TEXT NOT NULL,
    finished_at   TEXT
);

CREATE INDEX IF NOT EXISTS ix_run_log_dataset ON run_log(dataset, started_at);
CREATE INDEX IF NOT EXISTS ix_campaign_date   ON fact_campaign_daily(report_date);
CREATE INDEX IF NOT EXISTS ix_keyword_date    ON fact_keyword_daily(report_date);
CREATE INDEX IF NOT EXISTS ix_st_date         ON fact_search_term_daily(report_date);
CREATE INDEX IF NOT EXISTS ix_placement_date  ON fact_placement_daily(report_date);
