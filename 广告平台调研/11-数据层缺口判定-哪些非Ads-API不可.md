# 数据层缺口判定：哪些非 Amazon Ads API 不可

> 场景：工作台只做数据层，操作权留给自己。工作台 → grok bot → 每天 9:00 通过云电脑推送「今天该做哪些操作」→ 人工执行。
> 结论基于 2026-11-15 真实调用积加 OpenAPI（marketId=7 amazon-us）与本地库 `ingest/data/ads.sqlite3` 实测，不是文档推断。

---

## 一句话结论

**数据层 93% 够用，真正「非 Ads API 不可」的只有 1 项：否定词清单。**
其余 5 项缺口都是「有损但能绕」，不申请也能跑，只是 bot 的判断力打折。

按 bot 的 9 类建议算：**7 类完全可行、1 类有重复风险、1 类不可行**。

---

## 一、bot 的 9 类建议 × 数据可得性（实测）

| bot 每天要说的操作 | 数据是否够 | 现值来源 | 实测依据 |
|---|---|---|---|
| 关键词加价/降价 | ✅ 够 | `keyword.bid` | 15 天并集 83 个词，bid 100% 有值（0.1~1.54，中位 0.83） |
| ASIN/类目投放加价/降价 | ✅ 够 | `targeting.bid` | 69 个 targetId 全有 bid（0.02~0.88） |
| 活动预算调整 | ✅ 够 | `campaign.daily_budget` | 57/57 活动有预算（6~20） |
| 广告位溢价调整 | ✅ 够 | `placement.bid_adjustment` | 真实值 0%~900%，Top of Search 有 15 档 |
| 竞价策略调整 | ✅ 够 | `placement.bid_strategy` | 0=只降低 / 1=提高和降低 / 2=固定 |
| 暂停/启用活动 | ✅ 够 | `campaign.campaign_state` | '0'=52 个在跑，'1'=5 个暂停 |
| ASIN 级表现决策 | ✅ 够 | `adsSpProduct` | 25 个 ASIN，halo 拆分成立（adsSales = adsProductSales + otherProductSales，83/83 匹配） |
| 移词（搜索词→关键词） | ⚠️ 有重复风险 | `keyword.keyword_text` | 481 个搜索词中 438 个不在词表；但词表本身不全（见 §4），会重复建议创建已有词 |
| **否词（否定关键词/否定投放）** | **❌ 不够** | **无** | `jijia_list_endpoints(keyword="否定")` 返回 `[]`，积加 26 个广告接口零覆盖 |

**出价可见性量化（决定 bot 能不能给调价建议）：**

| 维度 | 覆盖 |
|---|---|
| 活动数 | 45 / 57 = **79%** |
| 曝光量 | 323,882 / 345,618 = **93.7%** |
| 花费 | $4,081.94 / $4,264.57 = **95.7%** |

看不到出价的 12 个活动，性质都是「没在跑」或「几乎没跑」：

- **5 个已暂停**：`L4-auto-同类`（历史 11,230 imp）、`L2-cable machine广泛`（6,858）、`L2-基础bid0.8-cable machine`（1,478）、`L2-基础bid0.5-ab workout`（1,321）、`L3-基础bid0.4ros120-ab machine for home-FIX`（834）
- **5 个 15 天零曝光**：`粉色综训小词词组`、`freestanding cable machine-粉色综训-ph`、`foldable cable machine词组`、`lat pulldown machine首页卡位`、`sp-哑铃-精准2`
- **2 个近零**：`sp-多边形哑铃-竞品`（13 imp）、`自动同类`（2 imp）

⇒ 对「正在花钱的活动」，出价可见性接近 100%。**唯一代价是：暂停的活动看不到原出价，bot 无法建议「重启并把 bid 从 X 调到 Y」，只能说「建议重启」而不给具体出价。**

---

## 二、真正「非 Ads API 不可」的 1 项

### 否定词清单（P0，唯一硬阻断）

**缺什么**：否定关键词（广告组级 + 活动级）、否定商品投放（广告组级 + 活动级）的完整清单，含匹配类型与当前状态。

**积加现状**：`jijia_list_endpoints(keyword="否定")` → `[]`；`jijia_list_endpoints(keyword="广告组")` → `[]`；`jijia_list_endpoints(keyword="预算")` → `[]`。四张事实表 raw_json 里 grep `egative` 零命中。**26 个广告接口中没有任何一个能返回否词。**

**为什么这是硬阻断，而不是「有损」**：

bot 每天要说「把 `ab machine for home` 这个词否掉」。但如果这个词上周已经被否过，bot 会**每天重复说同一句**。三天后你就不看它的推送了——这不是数据精度问题，是**产品可用性问题**。

否词是广告优化里**唯一需要「历史记忆」的操作**：加价、降价、调预算都是「覆盖式」的（改了就是改了，看现值就够），而否词是「累加式」的（否过的词永远不会再出现在报表里，也无法从报表反推）。

**能不能绕**：能，但要人工维护台账。

- 方案 A：在积加后台导出一次否词 CSV，建本地 `negative_keywords` 表，bot 每次建议前查一遍；每次人工执行否词后手工回写。→ 可行，但**依赖你每天记得回写**，漏一天就重复建议。
- 方案 B：用 `spSearchKeywordsReport` / `spSearchTargetingReport` 的搜索词列表做「差集推断」——某个词连续 N 天有曝光但今天突然消失，猜测被否了。→ 不可靠（预算耗尽、竞价掉队、活动暂停都会造成同样现象）。

**最小补法**：只调 4 个只读接口，一次拉全量，无需日期参数：
```
POST /sp/negativeKeywords/list          # 广告组级否定关键词
POST /sp/campaignNegativeKeywords/list  # 活动级否定关键词
POST /sp/negativeTargets/list           # 广告组级否定投放
POST /sp/campaignNegativeTargets/list   # 活动级否定投放
```

---

## 三、真正「非 Ads API 不可」的第 2、3 项（影响小，但确实拿不到）

### 3.1 广告组默认出价（P2）

**积加现状**：`fact_keyword_daily` 只有 `ad_group_id` / `ad_group_name`，**没有 ad group default bid**。无广告组接口。

**影响**：bot 无法建议「把广告组默认出价从 0.5 降到 0.4」。但 SP 的绝大多数投放是关键词级/投放级出价，广告组默认出价只在**新建投放时**起作用，日常优化用不到。→ **可以不补。**

### 3.2 Purchased ASIN 归因（P2）

**积加现状**：`adsSpProduct` 给了 `otherProductSales`（halo 销售额），但不给「是哪个 ASIN 卖出去的」。实测 `B0GKNQJ95M` 的 `adsSales=129.99`、`adsProductSales=0`、`otherProductSales=129.99` —— 知道有 129.99 是 halo，但不知道买了什么。

**影响**：无法做「投放 ASIN A，实际卖出 ASIN B」的交叉决策。→ 对单品类店铺影响小，对多 SKU 店铺影响大。

---

## 四、有损但能绕的 3 项（不申请也能跑）

### 4.1 配置快照不完整（最需要设计规避的一项）

积加的实体接口（`spKeywordsPage` / `adsSpProductTargeting` / `adsSpCampaigns`）**不是「配置快照」，而是「按天的绩效报表」**。三条实测证据：

| 现象 | 实测 |
|---|---|
| 单日拉会漏实体 | 11-15 单日 60 个关键词；15 天并集 83 个 → **单日漏 27.7%** |
| 宽窗口被截断到最旧 | 传 30 天窗口（10-17..11-16）返回正好 500 行，`createDate` 只覆盖**最旧 9 天**（10-17..10-25），近期数据一条没有 |
| 无日期参数直接报错 | 三个实体端点不带 `startDate/endDate` → 全部 `code=40004`、`rows=0`，**没有「拿当前配置」的免日期入口** |
| 休眠/暂停实体不可见 | 15 天窗口 500 行里 `state` 全是 0；18 个关键词在 10-17..10-25 出现过、近 15 天完全消失（其中 4 个 `state=1` 已暂停：`adjustable dumbbell set`/`adjustable weights`/`adjustable dumbells`/`cap adjustable dumbbells`） |

**规避方案（必须做，否则 bot 会误判）**：

1. **逐日拉 + 15 天滚动并集**重建配置视图，而不是单日拉。成本：7 个端点 × 15 天 × 60s 间隔 ≈ 105 分钟（可并行压缩到 ~15 分钟）。
2. **用 campaign 表做兜底**：57/57 活动都有 `daily_budget`，6 个零曝光活动在 campaign 表里有完整配置（budget 15/12/12/20/20/12，`campaign_state='0'`）。campaign 表是唯一能当「活动级配置快照」用的表。
3. **bot 输出必须带「置信标记」**：对只在并集里出现、当天无曝光的实体，建议要标注「该对象近期无曝光，出价为历史值」。

**另外**：本地库缺 2026-11-14 整天（11-13 直接跳 11-15），但接口实查 11-14 正常返回（campaign 46 行 / targeting 64 行 / keyword 65 行）。→ **ingest 需要加缺天自检**，否则 bot 拿到的窗口是断的。

### 4.2 归因分层与首页顶位份额

**积加现状**：只有单一归因口径（`adsSales` / `adsOrders` / `adsProductSales`），无 1d/7d/14d/30d 分层；无 `topOfSearchImpressionShare`；无 `acosClicks7d/14d`、`roasClicks7d/14d`、`salesOtherSku`。

**影响**：bot 无法回答「我加了 20% bid，首页顶位份额涨了没」——只能看 ACOS 结果反推。慢转化品类（大家电、高客单健身器械）会被误杀：7 天归因显示 ACOS 200%，14 天归因可能只有 60%。

**规避**：用 7 天移动窗口的 `adsSales` 代替，把「加价是否有效」的判断周期拉长到 7 天，并且**对高客单 SKU 单独放宽阈值**。这是纯策略层补偿，不需要新数据。

### 4.3 无效流量（GIV）

**积加现状**：无 Gross and invalid traffic 报表 → ACOS 里含机器人点击。

**规避**：用异常检测近似（单关键词 CTR 突然翻倍但 CVR=0、单次点击成本异常、深夜时段集中点击）。精度不如官方，但足以触发「这个词要查一下」的告警。

---

## 五、如果要补，最小申请方案

**只补数据层的话，申请 5 个只读接口就够，不需要任何写权限。**

| 序号 | 接口 | 补什么 | 对应缺口 |
|---|---|---|---|
| 1 | `POST /sp/negativeKeywords/list` | 广告组级否词 | §2 唯一硬阻断 |
| 2 | `POST /sp/campaignNegativeKeywords/list` | 活动级否词 | §2 |
| 3 | `POST /sp/negativeTargets/list` | 否定商品投放 | §2 |
| 4 | `POST /sp/campaignNegativeTargets/list` | 活动级否定投放 | §2 |
| 5 | `POST /reporting/reports` + `GET /reporting/reports/{id}` | `spCampaigns` 报表，`groupBy=campaign` | §4.2 顶位份额 + 7d/14d 归因；顺带 §4.3 的 GIV |

**申请信息（已核实）**：

- 类型：Direct Advertiser 自助申请，**费用 0**，审核 ≤ 1 工作日
- 三步：① 建 LwA 应用拿 Client ID/Secret（无需审核）→ ② `https://advertising.amazon.com/partner-network/register-api?ref_=a20m_us_api_drctad` 用**建 LwA 时的同一邮箱**登录申请 → ③ 批准后 assign scope `advertising::campaign_management`
- 凭证三件套：`Amazon-Advertising-API-ClientId` / `Authorization: Bearer <access_token>` / `Amazon-Advertising-API-Scope: <profileId>`
- 注意：Amazon 没有「只读」scope，`campaign_management` 含写权限——但**你可以只调 list 接口，不调写接口**，风险自控
- 区域 host：NA `https://advertising-api.amazon.com`、EU `https://advertising-api-eu.amazon.com`、FE `https://advertising-api-fe.amazon.com`；18 个站点跨三区 → 需三套 token + profile 缓存
- `access_token` 60 分钟过期，`refresh_token` 永不过期；authorization code 仅 5 分钟有效

**这 5 个接口的调用成本**：否词 4 个接口各 1 次/天（分页最多 3~5 次），报表 1 次创建 + 1 次轮询/天。**总耗时 < 1 分钟**，对比积加逐日并集的 105 分钟，差 100 倍。

---

## 六、不申请时的完整降级方案

如果决定不申请，工作台照样能跑，按下面做：

| 缺口 | 降级做法 | 残余风险 |
|---|---|---|
| 否词 | 积加后台导一次 CSV 建本地台账，人工执行后回写 | 漏回写就会重复建议；建议 bot 输出里带「请确认是否已否」 |
| 配置快照 | 逐日拉 + 15 天滚动并集，campaign 表兜底 | 暂停/休眠实体出价永远缺失；建议标注「历史值」 |
| 顶位份额 | 用 7 天移动 ACOS 代理，高客单 SKU 放宽阈值 | 慢转化词可能被误杀 |
| 归因分层 | 统一用 7 天口径，不用 1 天判断 | 短周期决策精度下降 |
| GIV | 异常检测告警（CTR 翻倍+CVR=0） | 无法精确剔除 |
| 广告组出价 | 放弃广告组级建议，只做关键词/投放级 | 新建投放时需人工定默认价 |
| Purchased ASIN | 只用 halo 金额，不做 ASIN 交叉 | 多 SKU 店铺的交叉决策缺失 |

---

## 七、一句话回答你的问题

> **除了控制权，数据层唯一「非 Ads API 不可」的是否定词清单**——因为它是唯一「累加式」的操作，积加 26 个广告接口零覆盖，且无法从报表反推。
>
> 其余都能绕：出价、预算、广告位溢价、竞价策略、活动状态、ASIN 表现，积加全都有现值（出价按花费覆盖 95.7%）；配置快照要靠逐日并集重建；归因分层和顶位份额只能用代理指标。
>
> **如果只补否词，申请 4 个 list 接口，0 元、1 个工作日。** 顺带把 `spCampaigns` 报表也接上，就同时解决了顶位份额和归因分层——总共 5 个只读接口，1 分钟跑完，比现在逐日并集快 100 倍。

---

*实测数据源：积加 OpenAPI 真实调用（2026-11-15，marketId=7）+ `ingest/data/ads.sqlite3`（2026-11-01..11-15，57 活动 / 83 关键词 / 69 投放 / 481 搜索词 / 25 ASIN）*
