# ingest —— 广告数据落地层（模块1 读侧 + 模块2 存储）

积加开放平台 REST → SQLite 日快照。**只读**：积加广告接口全部是 `query`/`page`，没有写能力，
改 bid / 改预算 / 加否词必须走 Amazon Ads API（模块3 写通道）。

## 文件

| 文件 | 作用 |
|---|---|
| `jijia_client.py` | 独立 REST 客户端：读钥匙串凭证 → 自算签名 → 直连 `open.gerpgo.com`，60s 节流，token 失效自动重取 |
| `schema.sql` | 表结构（WAL）：`dim_market` + 四张事实表 + `run_log` |
| `ingest.py` | 数据集注册表 + **逐日拉取** + 幂等 upsert + 运行报告 |
| `verify.py` | 数据质量核验：覆盖区间 / 每日行数（找缺日）/ 主键重复 / 金额汇总 / 异常值 / run_log |
| `data/ads.sqlite3` | 落库文件（`ADS_DB` 可覆盖） |

## 常用命令

```bash
# 刷新店铺维度（拿 marketId）
python3 ingest.py --markets-only

# 拉某天四个数据集
python3 ingest.py --market 7 --start 2026-11-12 --end 2026-11-12

# 只拉部分数据集
python3 ingest.py --market 7 --start 2026-11-01 --end 2026-11-13 --datasets campaign,searchterm

# 看库内现状（各表行数、最近运行、日期覆盖）
python3 ingest.py --report

# 数据质量核验（缺日、重复、异常值）
python3 verify.py

# 单接口调试
# ⚠️ endDate 是排他上界：拉 11-12 当天要写 endDate=2026-11-13
python3 jijia_client.py /operation/ads/adsSpCampaigns/query '{"marketId":7,"count":500,"page":1,"startDate":"2026-11-12","endDate":"2026-11-13"}'
```

环境变量：`ADS_DB`、`ADS_PAGE_SIZE`（默认 500）、`JIJIA_MIN_INTERVAL`（默认 60 秒；压测时可设 0，正式跑别动）。

> 时长预期：**每个数据集每天 1 次调用**，所以 13 天 × 4 数据集 = 52 次调用 ≈ 52 分钟。
> 日常增量（拉昨日）只有 4 次调用 ≈ 4 分钟。

## 数据集与接口

| 数据集 | 接口 | 表 | 粒度 |
|---|---|---|---|
| campaign | `/operation/ads/adsSpCampaigns/query` | `fact_campaign_daily` | 日 × campaign |
| keyword | `/operation/ads/spKeywordsPage/query` | `fact_keyword_daily` | 日 × keyword（带 `bid` 当前出价） |
| searchterm | `/operation/ads/spSearchKeywordsReport/page` | `fact_search_term_daily` | 日 × campaign × ad group × keyword × 搜索词 |
| placement | `/operation/ads/adsSpPlacement/page` | `fact_placement_daily` | 日 × campaign × 广告位（带 `bidStrategy`） |

## 踩过的坑（都已在代码里处理，改代码前先看）

1. **`createDate` 是报表日期**，不是 campaign 创建日；`startDate` 才是真实开始日。入库主键用 `createDate`。
2. **默认按 `id` 升序分页**，`page=1` 给最旧的数据。拉当日必须把日期窗口限定为那一天（见下条）。
2b. **`endDate` 是排他上界（exclusive），这是最容易静默丢数据的一条**。实测矩阵（四个接口一致）：
   `(D, D)` 在 D 为最新一天时返回 **0 行**；`(D, D+1)` 正好返回 D 当天；`(D-1, D)` 只返回 D-1；
   `(D-1, D+1)` 返回两天。→ **拉 D 当天必须传 `startDate=D, endDate=D+1`**，代码里是 `next_day(day)`。
   另一条：**数据 T+1 才可用**，今天查 `(今天, 明天)` 拿到的是昨天的数据。
3. **分页参数是假的（四个接口全部）**：`page`/`pageNo`/`pageIndex`/`offset` 全部被忽略，
   返回的首尾行完全相同。**没有任何可用翻页手段。**
4. **`count` 硬上限**：campaign `count=500` 可用、`count=1000` 报 `code=40004` 返回 0 行；
   placement `count=999` 报 `code=50099`。→ 默认 500，且**不能靠加大 count 拿全区间**。
5. **⇒ 只能一天一查**：区间查询会被 500 行上限截断（实测 11-01~11-13 只回来 11-01..11-09 的 500 行）。
   `ingest.py` 已把区间展开成逐日单查；单日若返回 ≥PAGE_SIZE 行会在 `run_log.note` 记"可能截断"。
6. **零值语义不统一**：`campaign` 接口无单时 `acos=0.0`、`roas=0.0`；`searchterm` 接口无单时 `acos=null`。
   判"无单"一律用 `ads_orders=0` / `clicks`，**不要**靠 `acos` 是否为空或是否等于 0。
7. **必须直连**：积加白名单校验的是请求出口 IP，走代理（Clash 等）会鉴权失败。客户端已强制 `--noproxy`。
8. **`pagesize` 全小写**：店铺表 `/middle/base/market/page` 写 `pageSize` 会报必填参数缺失。
9. **T+1 拉到的数是「能查到」，不是「已结算」——必须滚动重拉**。同一接口同一天：
   2026-11-15 在 T+1（11-16 06:10）拉到 campaign **23 击 / $23.50**，到 T+3（11-18 09:35）
   变成 **103 击 / $97.21**，**低报 4.5 倍**；同期 placement 已经 100 击，两表对不上。
   重拉老日期（11-05）则一字不变 —— 说明只有最近 2–3 天在回填。
   → `--recheck-days N`（默认 3）每次运行重拉最近 N 天；显式给 `--start/--end` 时不覆盖。
   幂等 upsert，重拉无副作用。**只拉 yesterday 会让引擎窗口的最后一天永远停在残缺值上**，
   而 `date_window()` 的 `end = max(report_date) - 2` 正好取的就是那天。

## 字段口径备忘

- `targetingType`：0 = 手动，1 = 自动（实测推断，campaign 名含"自动"者为 1；未获官方枚举确认）。
- `servingStatus`：`CAMPAIGN_STATUS_ENABLED` / `CAMPAIGN_OUT_OF_BUDGET` 等字符串。
- `state`：实测恒为 0，含义待确认（暂不用于判断启停，用 `servingStatus`）。
- `dim_market` 里的 `ads_report_init_begin`（本账号 2025-10-31）= 广告报表可回溯起点。
- `row_hash` 是积加行指纹，可做幂等键；跨库不可移植，已与业务主键并存。

## 定时（launchd）

见 `launchd/com.baoyanan.ads-ingest.plist`，每天 06:10 跑 `--recheck-days 3`：
重拉最近 3 天（含昨日）+ 幂等 upsert，覆盖 Amazon 的报表回填。
外层套 `caffeinate -i -m`，否则 macOS 的 Maintenance Sleep 会冻结进程（15 分钟一睡）。
单次调用实测 1–30 分钟（60s 节流 + 接口慢），3 天 × 4 数据集约 1 小时。
