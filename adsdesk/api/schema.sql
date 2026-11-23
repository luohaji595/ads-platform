-- 工作台 P1 建表脚本
-- 与 docs/工作台-工程计划书-v1.0.md §4.1 / §4.2 一一对应
-- 幂等：全部 IF NOT EXISTS，可重复执行
--
-- 执行：python3 adsdesk/api/migrate.py
-- 或：sqlite3 ingest/data/ads.sqlite3 < adsdesk/api/schema.sql

-- ═══════════════════════════════════════════════════════════
-- ① 奏折归档
-- pack_json 从 engine_run 分出来：engine_run 是运行记录，不该塞 30KB JSON
-- ═══════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS memorial (
    run_id        INTEGER PRIMARY KEY,
    business_day  TEXT NOT NULL,              -- 窗口末日，如 2026-11-14
    pack_json     TEXT NOT NULL,
    report_path   TEXT,
    agent_state   TEXT NOT NULL DEFAULT 'pending',
                  -- pending | ok | partial | failed（诊断 agent 的状态）
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memorial_day ON memorial(business_day DESC);

-- ═══════════════════════════════════════════════════════════
-- ② agent 调用留痕
-- 任何输出都能追到「哪个模型、哪版 prompt、多少 token」
-- ═══════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS agent_run (
    agent_run_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    job           TEXT NOT NULL,              -- diagnose | attribute | converge | converse
    run_id        INTEGER,                    -- diagnose 关联的引擎 run
    model         TEXT NOT NULL,
    prompt_ver    TEXT NOT NULL,              -- prompts/*.md 的 sha1 前 8 位
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    status        TEXT NOT NULL DEFAULT 'running',   -- running | ok | failed
    tokens_in     INTEGER,
    tokens_out    INTEGER,
    input_digest  TEXT,                       -- 输入 JSON 的 sha256，用于复现
    output_json   TEXT,
    error         TEXT
);
CREATE INDEX IF NOT EXISTS idx_agent_run_job ON agent_run(job, started_at DESC);

-- ═══════════════════════════════════════════════════════════
-- ③ agent 反对规则建议
-- 一条反对一个 decision_id，可反对多条
-- ═══════════════════════════════════════════════════════════
-- verdict 与 outcome 是两件事，绝对不能合并（合并过一次，26 条质疑因此烂在 doubt 里）：
--   verdict  = agent 写下这条反对时的「态度」：oppose（我反对这条决策）/ doubt（我怀疑这条决策）
--   outcome  = 事后裁判「它这次反对对了没有」：pending / correct / wrong / unverifiable
-- 一个是它说的话，一个是世界对它的判决。写进同一列就再也分不清
-- 「它态度很硬但其实判错了」和「它态度很软但确实说对了」。
CREATE TABLE IF NOT EXISTS agent_objection (
    objection_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_run_id  INTEGER NOT NULL,
    decision_id   INTEGER NOT NULL,           -- 必须指向已存在的 decision_log 行
    verdict       TEXT NOT NULL,              -- oppose | doubt（agent 态度，永不重写）
    why           TEXT NOT NULL,              -- 为什么反对（人话，一句话）
    evidence      TEXT,                       -- 引用了哪个字段/哪张表/哪条 policy
    created_at    TEXT NOT NULL,
    -- 以下五列是「可兑现化」改造：把反对从一句牢骚变成一条能被判真假的断言。
    -- predicts_json 是 agent 原始 predicts 数组的原文副本（不解析、不改写），
    -- 因为判定逻辑以后会变，原文不能丢 —— 丢了就再也没有第二次判的机会。
    predicts_json TEXT,                       -- agent 原始 predicts 数组（JSON）
    verifiable_at TEXT,                       -- min(predicts[].verifiable_at)，查 due 用
    checked_at    TEXT,                       -- 实际判定时刻
    outcome       TEXT NOT NULL DEFAULT 'pending',  -- pending|correct|wrong|unverifiable（与 agent_eval 同一套词表）
    actual        TEXT,                       -- 判定时看到的值，即判决依据
    judged_by     TEXT                        -- rule:xxx 或 model:<name>，谁判的
);
CREATE INDEX IF NOT EXISTS idx_objection_decision ON agent_objection(decision_id);
-- 注意：ix_agent_objection_due(verifiable_at, outcome) 故意不写在这里，写在
-- adsdesk/api/migrate.py 的 POST_INDEXES 里。原因是本文件整段 executescript
-- 先跑、补列(ALTER)后跑：对已有库来说 agent_objection 表已存在，
-- CREATE TABLE IF NOT EXISTS 是空转，此时建索引会因 verifiable_at 不存在而整段报
-- 「no such column: verifiable_at」，迁移直接中断。放到补列之后再建，
-- 新库和老库才会走同一条能成功的路径。

-- ═══════════════════════════════════════════════════════════
-- ④ agent 的可验证断言
-- 「学习」能成立的地基：没有它，agent 说什么都不算数
-- ═══════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS agent_eval (
    eval_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_run_id  INTEGER NOT NULL,
    agent         TEXT NOT NULL,
    claim         TEXT NOT NULL,
    verifiable_at TEXT NOT NULL,
    method        TEXT NOT NULL,              -- 用什么数据、什么口径验
    expected      TEXT NOT NULL,              -- 要能判真假
    verdict       TEXT NOT NULL DEFAULT 'pending',
                  -- pending | correct | wrong | unverifiable
    actual        TEXT,
    checked_at    TEXT,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_eval_due ON agent_eval(verdict, verifiable_at);

-- ═══════════════════════════════════════════════════════════
-- ⑤ 对话 agent 会话记录
-- 只读问答也要留痕，否则无法审计它说过什么
-- ═══════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS chat_turn (
    turn_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id    TEXT NOT NULL,
    role          TEXT NOT NULL,              -- user | assistant
    content       TEXT NOT NULL,
    cited_tables  TEXT,
    agent_run_id  INTEGER,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chat_session ON chat_turn(session_id, turn_id);

-- ═══════════════════════════════════════════════════════════
-- ⑥ 执行单（打钩后生成的「照这个改后台」清单）
-- 一张单对一次打钩批次，可含多条 decision
-- ═══════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS exec_order (
    order_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id     TEXT,                       -- 关联 ticket.ticket_id
    run_id        INTEGER,
    created_at    TEXT NOT NULL,
    created_by    TEXT NOT NULL DEFAULT 'owner',
    status        TEXT NOT NULL DEFAULT 'open',   -- open | done | partial | cancelled
    note          TEXT
);
CREATE TABLE IF NOT EXISTS exec_order_item (
    item_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id      INTEGER NOT NULL,
    decision_id   INTEGER NOT NULL,
    campaign_name TEXT,
    entity_label  TEXT,
    action_type   TEXT NOT NULL,
    current_value TEXT,
    target_value  TEXT,
    executed_at   TEXT,                       -- 用户点「已改」的时间
    executed_by   TEXT,
    UNIQUE(order_id, decision_id)
);
CREATE INDEX IF NOT EXISTS idx_exec_item_order ON exec_order_item(order_id);
CREATE INDEX IF NOT EXISTS idx_exec_item_decision ON exec_order_item(decision_id);

-- ═══════════════════════════════════════════════════════════
-- ⑦ 外部市场（P4-23：sorftime 采集，不在积加口径内）
-- 三张表全部由 ingest/market.py 逐项 upsert，唯一索引就是幂等键：
-- 同一份快照重复采集只会覆盖，不会重复插入。
--
-- market_id 在这里是**站点代码**（us/gb/de…），不是 dim_market.market_id
-- —— 后者是积加广告账户的整数 id。刻意换成 TEXT 存站点代码，是为了让
-- 「外部市场」和「自家广告账户」在 join 时直接类型不匹配而报错，
-- 而不是悄悄按 1/5/7 连错人。
-- ═══════════════════════════════════════════════════════════

-- ABA 搜索词周榜（源：sorftime keyword_list / keyword_list_from_history）
CREATE TABLE IF NOT EXISTS aba_weekly (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_week         TEXT NOT NULL,          -- ISO 周 '2026-W47'，跨周比较的时间轴
    snapshot_date         TEXT NOT NULL,          -- 源端 data_update_time 归一到 YYYY-MM-DD
    market_id             TEXT NOT NULL,          -- 站点代码 us/gb/…
    search_term           TEXT NOT NULL,
    search_term_cn        TEXT,
    search_frequency_rank INTEGER NOT NULL,       -- 本周搜索频次排名（1 = 最高）
    rank_gain             INTEGER,                -- 上周名次 - 本周名次；正数 = 名次上升
    search_volume         INTEGER,                -- 近 30 天搜索量
    click_share           REAL,                   -- Top3 商品点击占比 %
    conversion_share      REAL,                   -- Top3 商品转化占比 %
    top3_brand_share      REAL,                   -- Top3 品牌销量占比 %
    cpc_bid               REAL,                   -- 精确匹配建议 CPC
    peak_season           TEXT,
    source                TEXT NOT NULL DEFAULT 'sorftime.keyword_detail',
    row_hash              TEXT NOT NULL,
    raw_json              TEXT NOT NULL,
    ingested_at           TEXT NOT NULL,          -- 本地入库时间
    created_at            TEXT NOT NULL
);
-- 幂等键：同站点 + 同周 + 同词 = 一行。换周之后同一个词必然是新行。
CREATE UNIQUE INDEX IF NOT EXISTS ux_aba_weekly ON aba_weekly(market_id, snapshot_week, search_term);
CREATE INDEX IF NOT EXISTS idx_aba_week_rank ON aba_weekly(snapshot_week, search_frequency_rank);
CREATE INDEX IF NOT EXISTS idx_aba_term ON aba_weekly(search_term, snapshot_week);

-- 竞品动作流水（源：sorftime product_detail / product_content 的日快照 diff）
-- 只记**变化**，不记每日全量；首日没有前值就不产生任何 move。
CREATE TABLE IF NOT EXISTS competitor_move (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    asin          TEXT NOT NULL,
    market_id     TEXT NOT NULL,                  -- 站点代码 us/gb/…
    observed_at   TEXT NOT NULL,                  -- 采集时刻（ISO，含分钟）
    observed_date TEXT NOT NULL,                  -- 观测日 YYYY-MM-DD，按天聚合用
    move_type     TEXT NOT NULL,                  -- price|promotion|rating|rank|variants|title|image
    field         TEXT NOT NULL,                  -- 具体字段 price/coupon/star_rating/…
    old_value     TEXT,
    new_value     TEXT,
    delta         REAL,                           -- 数值型新值-旧值；文本变化为 NULL
    label         TEXT,                           -- 人读标签（品牌 + 标题片段），面板直接显示
    source        TEXT NOT NULL DEFAULT 'sorftime',
    row_hash      TEXT NOT NULL,
    raw_json      TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
-- 幂等键：同一 ASIN 同一天同类型同字段只留一条（当天多次采集以最后一次为准）。
CREATE UNIQUE INDEX IF NOT EXISTS ux_competitor_move
    ON competitor_move(asin, market_id, observed_date, move_type, field);
CREATE INDEX IF NOT EXISTS idx_move_recent ON competitor_move(observed_date DESC, observed_at DESC);

-- 品类 / 关键词 / 单 ASIN 的每日行情快照（源：category_report / category_products）
-- 一行 = 一天 × 一个站点 × 一个标的。类目级与 ASIN 级共用一套指标列，
-- 不适用于该标的的列留 NULL —— 比拆三张窄表更容易做跨天对比。
CREATE TABLE IF NOT EXISTS market_daily (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    report_date      TEXT NOT NULL,               -- YYYY-MM-DD
    market_id        TEXT NOT NULL,               -- 站点代码 us/gb/…
    subject_type     TEXT NOT NULL,               -- category | asin | keyword
    subject_id       TEXT NOT NULL,               -- node_id / asin / 词
    subject_name     TEXT,
    -- 类目级指标（category_report）
    top100_sales_volume  INTEGER,                 -- Top100 月销总量（-1 = 源端无数据）
    top100_sales_amount  REAL,
    avg_price            REAL,
    min_price            REAL,
    max_price            REAL,
    top3_brand_share     REAL,                    -- Top3 品牌月销占比 %
    top3_seller_share    REAL,                    -- Top3 卖家月销占比 %
    amazon_owned_share   REAL,                    -- 亚马逊自营占比 %
    return_rate          REAL,                    -- 平均退货率 %
    conversion_rate      REAL,                    -- 品类转化率 %
    product_count        INTEGER,                 -- 在线商品数
    avg_listing_days     INTEGER,                 -- 平均上架天数
    price_bands_json     TEXT,                    -- 价位带分布 [{"band","count","share"}]
    bsr_bands_json       TEXT,                    -- BSR（子类排名）分布
    review_bands_json    TEXT,                    -- 评论数分层分布
    -- ASIN / 关键词级指标（product_detail / keyword_list）
    price                REAL,
    list_price           REAL,
    coupon               REAL,
    star_rating          REAL,
    review_count         INTEGER,
    review_growth        REAL,                    -- 评论数较上一快照的增量（增速，条/天）
    variation_count      INTEGER,
    subcategory_rank     INTEGER,
    title                TEXT,                    -- 标题快照：竞品改标题的比对基准
    main_image           TEXT,                    -- 主图 URL：改主图的比对基准（product_content 才有）
    search_volume        INTEGER,
    source           TEXT NOT NULL,               -- sorftime.category_report / product_detail / …
    row_hash         TEXT NOT NULL,
    raw_json         TEXT NOT NULL,
    created_at       TEXT NOT NULL
);
-- 幂等键：一天一站点一标的只有一行，重采即覆盖。
CREATE UNIQUE INDEX IF NOT EXISTS ux_market_daily
    ON market_daily(report_date, market_id, subject_type, subject_id);
CREATE INDEX IF NOT EXISTS idx_market_daily_subject
    ON market_daily(subject_type, subject_id, report_date DESC);

-- ═══════════════════════════════════════════════════════════════════════════
-- ⑧ 读数快照（2026-11-20）
--
-- 为什么必须单独建这张表：fact_*_daily 是 upsert 覆盖，同一 report_date 被
-- 重拉一次就把上一次的读数抹掉了。而 agent 的断言里有一整类是关于「重拉会不会
-- 把数补上来」的 —— 实测 #74「11-16 的点击数在 T+3 重拉后 ≥ T+1 首次读数的 1.2 倍」，
-- 它需要同一个 (dataset, report_date) 的两次读数，可库里只剩最后一次。
-- 这条断言因此被判成 unverifiable，而 unverifiable 一旦落库就被幂等 UPDATE 钉死，
-- 永久结论 —— 明明只是我们没留历史。
--
-- 写入策略：只在读数**发生变化**时追加一行（同值重拉不写）。
-- 理由：日更链每半小时跑一次，全量追加会让表迅速膨胀到百万行，而其中 99% 是
-- 「什么都没变」。只记变化既省空间，又刚好是断言需要的 —— 「第一次读数是多少、
-- 后来被改成了多少」。
--
-- 代价（必须知道）：读数没变化时不会有新行，所以「重拉过但数没变」这件事
-- 只能从 run_log 看出来（同一天有两次 status=ok 的 ingest），不能从本表看出来。
-- 判 #74 这类断言时要把两者合起来读：本表给「变没变」，run_log 给「有没有重拉」。
-- ═══════════════════════════════════════════════════════════════════════════
CREATE TABLE IF NOT EXISTS fact_read_snapshot (
    snapshot_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    dataset      TEXT NOT NULL,              -- campaign | keyword | searchterm | placement
    market_id    INTEGER NOT NULL,
    report_date  TEXT NOT NULL,              -- 被读的那一天（不是读的那一天）
    clicks       INTEGER,
    impressions  INTEGER,
    cost         REAL,
    ads_orders   INTEGER,
    ads_sales    REAL,
    observed_at  TEXT NOT NULL,              -- 读的时刻（= ingest 写入时刻）
    run_id       INTEGER,                    -- run_log.run_id，串回那次取数
    note         TEXT                        -- 与上一次读数差在哪，人眼可读
);
-- 查「某天某个数据集的所有读数」是唯一的高频查询形状（收敛判 #74 就这一条）。
CREATE INDEX IF NOT EXISTS idx_read_snap_key
    ON fact_read_snapshot(dataset, market_id, report_date, observed_at);
