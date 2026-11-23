# 10 · 积加广告 API 接入量 vs Amazon Ads API（逐项对比）

> 取证时间：2026-11-16
> 证据来源：①积加接口地图 `mcp__jijia__jijia_list_endpoints`（166 endpoint 全量）②积加真实返回 `ingest/data/ads.sqlite3` 四张表 raw_json ③Amazon 官方 Postman 集合 `Amazon_Ads_API.postman_collection.json`（1,648,753 字节，244 请求）④官方 v3 报表文档 campaign.md / placement.md / search-term.md / overview.md ⑤官方 rate-limiting.md / exports/overview.md / apply-for-access.md

---

## 一、一句话结论

**积加接的是「报表读」这一层，不是「广告管理」这一层。**
Amazon Ads API 有 244 个请求 / 177 个去重路径，其中 **163 个是广告管理 + 预算 + 素材，98 个是写操作**；积加广告相关只有 **26 个接口、0 个写接口**。
换算：**接口量 ≈ 官方的 10.6%（26/244）**，**写能力 0%（0/177）**，**报表类型槽位覆盖 10/32（31%）**，**去重报表类型 5/18（28%）**。

---

## 二、积加到底接了多少（可核验数字）

### 2.1 总量

| 口径 | 数量 |
|---|---|
| 积加 OpenAPI 全量 endpoint | 166 |
| 其中 `/operation/ads/` 广告域 | **25** |
| 再加 `/finance/asset/adsInvoice/page`（广告发票） | **26** |
| 其中 Amazon 广告**报表**接口 | 17 |
| 其中广告**写**接口（改 bid / 预算 / 否词 / 建活动） | **0** |
| 其中自动化**配置**接口 | 1（`strategyTemplate/page`，只读分时策略模板） |
| 其中**促销/Deal** 接口 | 4（秒杀 / 优惠券 / 管理促销 / 会员折扣） |
| 其中**跨平台广告**接口 | 2（Walmart 广告商品、TikTok GMVMax 商品） |

### 2.2 逐条清单（25 个 `/operation/ads/` + 1 个发票）

**SP（商品广告）9 个**
| 接口 | 对应官方报表 |
|---|---|
| `adsSpCampaigns/query` | `spCampaigns` groupBy=campaign |
| `adsSpProduct/query` | `spAdvertisedProduct` |
| `spKeywordsPage/query` | `spTargeting`（关键词侧，带当前 bid） |
| `adsSpProductTargeting/query` | `spTargeting`（商品投放侧） |
| `adsSpPlacement/page` | `spCampaigns` groupBy=**campaignPlacement** |
| `spSearchKeywordsReport/page` | `spSearchTerm`（关键词侧） |
| `spSearchTargetingReport/page` | `spSearchTerm`（投放侧） |
| `adsAsinAnalytical/page` | 无直接对应（积加自建 ASIN 汇总视图） |
| `adsKeywordAnalytical/page` + `/query` | 无直接对应（积加自建关键词/搜索词汇总视图） |

**SB（品牌广告）6 个**
`adsSbCampaign/query`（`sbCampaigns`）、`sbKeywordsPage/query`、`adsSbTargeting/query`、`sbPlacementPage/query`（`sbCampaignPlacement`）、`sbSearchKeywordsReport/page`（`sbSearchTerm`）、`adsSbCreative/page`（创意读取，非报表）

**SD（展示广告）2 个 —— 缺口最大**
`adsSdCampaigns/query`（`sdCampaigns`）、`adsSdProduct/query`（`sdAdvertisedProduct`）
→ **无 SD 关键词、无 SD 投放（targeting）、无 SD 广告组、无 SD 搜索词、无 SD 创意、无 SD 受众**

**其他 8 个**
`strategyTemplate/page`（分时策略模板只读）、`deal/query`、`coupon/query`、`openManagePromotion/query`、`openPrimeDiscount/query`、`walmartAdsProduct/query`、`tiktokGmvMaxProduct/query`、`/finance/asset/adsInvoice/page`

---

## 三、和 Amazon Ads API 的量级对比

### 3.1 硬数字

| 维度 | 积加 | Amazon Ads API | 差距 |
|---|---|---|---|
| 接口/请求数 | 26（广告相关） | **244**（Postman 集合叶子请求） | 积加 ≈ 10.6% |
| 去重路径数 | 26 | **177** | 积加 ≈ 14.7% |
| 写操作数 | **0** | **177**（POST 135 + PUT 31 + DELETE 11） | **积加 0%** |
| 读操作数 | 26（全读） | 67 | 积加 38.8% |
| 广告类型 | SP / SB / SD（各只到报表层） | SP / SB / SD / **Sponsored TV** / **Amazon DSP** | 少 2 个广告产品 |
| 报表类型覆盖 | 5 种去重类型 / 10 个槽位（官方 18 种 / 32 槽位） | 18 种 report type（含 DSP 独占 10 种） | 见 3.2 |
| 数据时效 | T+1 以上（官方报表同步） | 报表 T+1；**Marketing Stream 小时级推送** | 积加无实时流 |
| 单次取数窗口 | 积加侧受 count≤500 + 不可翻页限制，只能逐日查 | 单报表最长 31 天，一次拿全区间 | 积加调用数 ≈ 天数 × 数据集 |
| 节流 | 连续调用必须间隔 ~60s（用户实测告知） | 动态限流 + `429`/`Retry-After` + 指数退避 | 积加是固定硬间隔，更慢 |
| 保留期 | 跟随 Amazon（SP 95 / SB 60 天） | SP 95 / SB 60 / SD 65 / STV 95 / DSP 95 天 | 一致 |
| 预算规则 | 无 | SP 9 + SD 8 + SB 9 = 26 个端点 | 全缺 |
| 否定词/否定投放 | 无 | SP 16 个端点（4 组 CRUD）+ SB 8 个 | 全缺 |
| 素材库 | 无 | `assets/upload`、`assets/register`、`assets/search` 等 5 个 | 全缺 |
| 快照/导出 | 无 | Snapshots 9 + Exports 5 | 全缺 |
| AMC / DSP | 无 | AMC 33 个端点、DSP 5 个 | 全缺 |
| 推荐/建议类 | 无 | SP 关键词推荐、商品投放推荐、主题出价推荐、预算推荐、SB 4 类推荐 | 全缺 |
| 预算消耗实时查询 | 无 | Budget usage 3 个 | 全缺 |

### 3.2 报表类型逐类对照（官方 18 种 × 积加覆盖）

| Report type | SP | SB | SD | STV | DSP | 积加是否覆盖 |
|---|---|---|---|---|---|---|
| Ad | | ✓ | | | | ✗（SB 广告维度） |
| Ad group | | ✓ | ✓ | | | ✗ |
| Advertised product | ✓ | | ✓ | | | ✓ SP、✓ SD |
| Audience | | | | | ✓ | ✗ |
| Audio and video (beta) | | | | | ✓ | ✗ |
| Bid adjustments | | | | | ✓ | ✗ |
| Campaign | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ SP / ✓ SB / ✓ SD |
| Conversion Path | | | | | ✓ | ✗ |
| Geo | | | | | ✓ | ✗ |
| Gross and invalid traffic | ✓ | ✓ | ✓ | | | ✗（三类全缺） |
| Inventory | | | | | ✓ | ✗ |
| Placement | | ✓ | | | | ✓ SB、✓ SP（走 campaignPlacement） |
| Product | | | | | ✓ | ✗ |
| Purchased product | ✓ | ✓ | ✓ | | | ✗（三类全缺，影响跨 ASIN 归因） |
| Reach and frequency | | | | | ✓ | ✗ |
| Search term | ✓ | ✓ | | | | ✓ SP、✓ SB |
| Targeting | ✓ | ✓ | ✓ | ✓ | | ✓ SP、✓ SB、**✗ SD** |
| Tech | | | | | ✓ | ✗ |

**按广告类型算覆盖率（分母 = 该类型在官方矩阵中打勾的类型数）**
- SP：6 类中覆盖 4 类（Campaign / Advertised product / Search term / Targeting），缺 Gross and invalid traffic、Purchased product → **67%**
- SB：8 类中覆盖 4 类（Campaign / Placement / Search term / Targeting）→ **50%**
- SD：6 类中覆盖 2 类（Campaign / Advertised product）→ **33%**
- STV：2 类覆盖 0 → **0%**
- DSP：10 类覆盖 0 → **0%**
- **槽位合计：10 / 32 = 31%**；按**去重报表类型**算：覆盖 5 种（Campaign / Advertised product / Placement / Search term / Targeting）÷ 官方 18 种 = **28%**

### 3.3 字段深度对比（同一报表，积加 vs 官方）

以 SP Campaign 报表为例：

| 能力 | 积加 | 官方 |
|---|---|---|
| 归因窗口 | **单一口径**（`adsSales` / `adsOrders` / `adsProductSales`） | **分层 1d / 7d / 14d / 30d**（purchases、sales、unitsSold、attributedSalesSameSku、unitsSoldSameSku 各自 4 档） |
| 同 SKU / 其他 SKU 拆分 | 只有 `otherProductSales` 一个汇总 | `purchasesSameSku*`、`salesOtherSku7d`、`unitsSoldOtherSku7d` |
| 首页首位曝光份额 | **无** | `topOfSearchImpressionShare` |
| 预算规则字段 | **无** | `campaignRuleBasedBudgetAmount`、`campaignApplicableBudgetRuleId/Name` |
| 竞价策略 | 有（`bidStrategy`，仅 placement 表） | `campaignBiddingStrategy`（campaign 级） |
| ACOS/ROAS 口径 | 单一 `acos` / `roas` | 另有 `acosClicks7d/14d`、`roasClicks7d/14d` |
| 关键词状态 | `servingStatus` / `state` | `adKeywordStatus`（含不可投原因） |
| 搜索词行是否带出价 | **不带 bid** | `keywordBid`、`keywordType`、`targeting`、`adKeywordStatus` 全带 |
| 品牌新客（NTB） | **无** | SB 有 `newToBrandPurchases/Sales/UnitsSold` + Percentage/Rate 全套 |
| 视频指标 | 无 | SB 有 `video5SecondViews`、`videoCompleteViews`、四分位、`viewableImpressions` |
| 加购/心愿单 | 无 | `addToCart`、`addToCartClicks`、`addToList`、`brandedSearches`、`detailPageViews` |
| 币种 | 无 | `campaignBudgetCurrencyCode` |

---

## 四、能力维度打分（0–5）

| 能力 | 积加 | 官方 | 说明 |
|---|---|---|---|
| 读报表 | **3.5** | 5 | 积加能拿到主流量指标，但字段浅、归因单口径、缺 NTB/GIV/PP 类 |
| 写广告（改 bid/预算/状态） | **0** | 5 | 积加 OpenAPI 零写接口 |
| 建/删广告结构 | **0** | 5 | 官方 SP 4 组 + SB 5 组 + SD 5 组 CRUD |
| 否定词/否定投放 | **0** | 5 | 官方 SP 16 + SB 8 个端点 |
| 预算规则（自动加预算） | **0** | 5 | 官方 26 个端点 |
| 素材/创意资产 | **0（SB 仅可读）** | 5 | 官方 5 个资产端点 + SB 5 种创意类型创建 |
| 实时/小时级数据 | **0** | 5 | Marketing Stream 16 个数据集，NA/EU/FE 全支持 |
| 推荐/优化建议 | **0** | 4 | 官方关键词/投放/出价/预算/主题 5 类推荐端点 |
| DSP / Sponsored TV | **0** | 5 | 积加完全未接 |
| AMC | **0** | 5 | 官方 33 个 AMC 端点 |
| 多站点 | **4** | 5 | 积加 18 站点 13 个广告授权正常，聚合方便 |
| 上手成本 | **5** | 2 | 积加 appId/appKey + md5 签名，5 分钟跑通；官方要 LwA + Partner 审核（≤1 工作日） |

---

## 五、对我们平台意味着什么（结论）

1. **读侧继续用积加是划算的**：17 个报表接口 + 18 站点聚合 + 零鉴权成本，够支撑 SP/SB 的日常诊断与规则引擎出建议单。
2. **三个必须绕过的坑**：
   - **SD 只有 2 张表**，SD 的 targeting/ad group 维度缺失 → 若要做 SD 优化必须直连官方。
   - **无 Gross and invalid traffic / Purchased product** → 无法做无效流量清洗与跨 ASIN 归因分析。
   - **归因只有单口径** → 决策阈值不能按 7d/14d 分层校准，只能自己定一个口径全库统一（建议统一用积加口径并在规则里固化，不要混用）。
3. **写侧只能走官方 Ads API**：改 bid / 改预算 / 加否词 / 建活动 / 预算规则，积加一个都没有。这不是"以后会补"，是**产品定位决定的**——积加把执行能力留给了自家"自动策略"（规则引擎），OpenAPI 只暴露数据。
4. **积加真正的自动化能力不在 API 里**：`自动策略`（按表现，7 种活动动作 + 关键词/投放/受众/搜索词）和`广告抢位`（每小时跟排名）在 OpenAPI 中**完全不存在**，只有 `strategyTemplate/page` 能读到分时策略模板。→ **自建平台无法接管积加的规则，必须自己实现规则引擎**（这与 MEMORY 里"不接受纯规则引擎作终局、要可成长 agent"的方向一致：规则是底座，agent 是上层）。
5. **第二条取数通道**：积加「MCP 套餐」（官方原话"仅 MCP 数据接口能力，适合自研智能体的技术团队"），19 个 MCP server 中广告相关 `gerp-ads`（21 tools）+ `gerp-ads-v2`（18 tools），同样零写操作。适合快速做 agent 原型，不适合做生产主通道（点数池计费 + 调用上限）。

---

## 六、本账号实测证据（数据已落库）

`/Users/baoyanan/自动化广告平台/ingest/data/ads.sqlite3`：

| 表 | 行数 | 覆盖 | 关键字段验证 |
|---|---|---|---|
| `fact_campaign_daily` | 681 | 57 个活动 × 2026-11-01..11-15 | raw_json 30 字段，含 dailyBudget / targetingType / servingStatus |
| `fact_keyword_daily` | 763 | 81 个关键词 | bid 非空 763/763（**100% 有当前出价**） |
| `fact_search_term_daily` | 956 | — | 含 `query` 真实搜索词，**但不含 bid** |
| `fact_placement_daily` | 1,599 | 4 种广告位 | Top of Search 457 / Detail Page 533 / Other on-Amazon 551 / Off Amazon 58；含 bidAdjustment + bidStrategy |

`dim_market`：18 个站点，`adsState=Successfully` 13 个，`InvalidAuth` 2 个（market 1 amazon-us、market 11 amazon-de），`Pending` 3 个（CA/MX/BR）。

**三条已实测硬约束**（决定管道设计）：
1. `endDate` 是排他上界 → 拉 D 当天必须传 `(D, D+1)`，传 `(D, D)` 静默丢最新一天。
2. `page` / `pageNum` / `pageIndex` / `offset` **全部被忽略**，无可翻页手段。
3. `count` 硬上限 500（1000 → `code=40004`；999 → `code=50099`）→ **只能逐日单查**，成本 = 天数 × 数据集数 × 60s。

---

## 七、如果要把缺口补齐，最短路径

| 目标 | 做法 | 前置 |
|---|---|---|
| 补 SD 全维度 | 官方 `sdCampaigns`（groupBy=matchedTarget）+ `sdTargeting` 报表 + `/sd/targets` list | Ads API 读权限 |
| 补 GIV / Purchased product | 官方 `spCampaigns`/`sbCampaigns`/`sdCampaigns` 报表加 reportType 组合 | 同上 |
| 补 NTB / 视频 / 加购 | 官方 `sbCampaigns`、`sbCampaignPlacement` 报表 | 同上 |
| 执行层（改价/否词/预算） | 官方 `/sp/keywords` PUT、`/sp/negativeKeywords` POST、`/sp/campaigns` PUT、`/sp/budgetRules` | Ads API **写**权限（Direct Advertiser 或 Partner 审核，≤1 工作日） |
| 近实时 | Amazon Marketing Stream `sp-traffic` / `sp-conversion`（需 AWS SQS + 订阅） | 同上 + AWS 资源 |
| 保留期外历史 | 自建滚动快照（SP 95 / SB 60 天窗口，错过即永久缺失） | 今天就要开始跑 |

---

*本文所有官方数字均可由 `/tmp/pm.json`（Postman 集合）与官方文档原文复现；积加数字可由 `jijia_list_endpoints` + `ads.sqlite3` 复现。*
