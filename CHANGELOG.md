# 变更记录

本仓库按「一次真实改动 = 一条记录」写。每条都写清三件事：**改之前会怎样**（症状或隐患）、
**改了什么**（文件与机制）、**怎么验的**（可复跑的命令与结果）。

线上渠道：`https://github.com/luohaji595/ads-platform`（私有，`main`）。
本机 `github.com:443` 的 TLS 被掐，推送走 Git Data API —— 见 `docs/GitHub推送说明.md`。

---

## v1.1 · 2026-11-24

提交 `5f29dee`（父 `b8dc2ba`）｜ 36 文件 ｜ +2983 / −1088 ｜ API 版本 2 → **3**

这一版是三件事叠在一起发的：**前台重构**、**策略认领闭环**、**一批归因与审批链的真实缺陷修复**。
前两件是功能，第三件是 bug —— 它们混在同一批里，是因为前台要展示的正是这些修完之后的语义
（比如归因页现在显示「可结算日」而不是「窗口末日」）。

### 一、修掉的 9 个缺陷（这是本版最该看的部分）

按严重度排。每条都在 `tests/test_bugfixes.py` 里有对应的回归用例（14 项全过）。

| # | 位置 | 改之前会怎样 | 改成什么 |
|---|---|---|---|
| 1 | `adsdesk/api/routes/approve.py` `mark_executed` | 状态检查写的是 `not in ("approved", "proposed")` —— **`proposed` 也能标「已改」**，等于绕过「主人打钩」这道唯一的人工闸门 | `!= "approved"` 才放行，其余一律 409 |
| 2 | `adsdesk/api/routes/approve.py` `_close_exec_items` | 关单时按 `decision_id` 一刀切更新 `exec_order_item`，把**已取消单里的同条目**也标上 `executed_at`，作废单凭空多出「已改」记录 | 更新语句补 `and order_id in (...)`，`order_ids` 只取 `exec_order.status != 'cancelled'` 的单 |
| 3 | `adsdesk/agents/attribute.py` 结算时机 | 归因窗一到就结算。窗末最后 1~2 天的订单还没回传 → 后窗口订单**系统性低估** → ACOS 虚高 → 判成 `worsened` → `record.py` 据此**错误降配额** | 新增 `ATTR_SETTLE_LAG_DAYS = 3`；`_window_matured()` 要求「数据已到 `窗末日 + 3 天`」才允许结算，数据没追平时宁可 `deferred` 也不拿半截数据出结论 |
| 4 | `adsdesk/agents/attribute.py` `_windows()` / `_peer_window()` | 后窗口从执行当天算起（含 `exd`）。3 天窗里 1/3、7 天窗里 1/7 是**执行当天的脏数据**（那天既含改前也含改后的投放） | 后窗口改为 `[exd+1, exd+n]`，长度仍是 n 天；对照组锚点当天同样排除 |
| 5 | `adsdesk/agents/attribute.py` `peers()` | 对照组只排了「同规则同层级」，没排「同对象期间另有已执行决策」的候选 —— **别人的动作造成的变化被当成趋势扣掉** | 加 `not exists (… status='executed' and 落在 anchor±7 天内)` 子查询 |
| 6 | `ingest/jijia_client.py` `call()` | token 过期重试是**死代码**：只认 `RuntimeError` 里的 "401"/"40005"，而积加真实返回是 `HTTP 200 + {"code":40005}`，没人接 → 当天取数全废直到人工换 token。且重试复用 60s 主节流，一次过期阻塞一分钟 | 新增 `TOKEN_EXPIRED_CODES` / `_is_token_expired_resp()`；HTTP200 业务码命中就清 token 重试一次；重试走独立的 `RETRY_INTERVAL`（默认 5s，`JIJIA_RETRY_INTERVAL` 可调），不占主节流 |
| 7 | `engine/facts.py` `ctr` | `_r(imp and ck / imp, 6)` —— `imp=0` 时 `imp and ...` 短路成 `0`，返回 `0` 而不是 `None`，**把「没有曝光」和「CTR 0%」混为一谈**；同函数里 cvr/cpc/acos 全走 `_safe_div`，只有 ctr 例外 | `_r(_safe_div(ck, imp), 6)`，与邻居一致 |
| 8 | `adsdesk/api/db.py` `connect(readonly=True)` | 只读句柄上**无条件**执行 `pragma journal_mode=WAL`（写操作）。库已是 WAL 时只是回读模式不炸，换到非 WAL 的副本（备份、换机器、CI）必抛 `attempt to write a readonly database` | 只读分支改 `pragma query_only=ON`，`journal_mode=WAL` 只在非只读分支执行；`busy_timeout` 两者共用 |
| 9 | `adsdesk/chain.py` + `midship/` | 事件中台投影**完全没接进链**（`MAX_ATTEMPTS` 无此步、`build_cmd` 无此分支、`--only` 的 choices 无此值） | 接进来但 **opt-in**（见下节） |

### 二、归因窗口径的两处连带调整

改了窗的定义，下游口径必须一起动，否则页面与 agent 会各说各话：

- `adsdesk/agents/attribute.py` `ATTRIBUTION_DAYS`：`lower_budget` / `reduce_budget` / `raise_budget`
  由 **3 天 → 5 天**。3 天的前提是「窗一到就能结算」，而窗末 1~2 天订单没回传，3 天里能用的只有 1~2 天。
  同步改了 `adsdesk/api/selftest.py` 的 `plan_days` 与 `docs/工作台-工程计划书-v1.0.md` §4.4 表格（含变更注记）。
- `adsdesk/api/routes/cockpit.py`：`attribution_due_at` 是**窗口最后一天**，不是「可以结算的那天」。
  原来按 due 判 `overdue`，会出现「页面标红三天、归因 agent 按兵不动」的假红。
  现在 `from agents.attribute import ATTR_SETTLE_LAG_DAYS`（唯一真源，避免两处漂移），
  接口新增 `settle_at`，`overdue` / `days_left` 全部改按 `settle_at` 算。
  前端归因表随之把「到期 / 剩余」拆成「窗末 / 可结算 / 距结算」三列。

### 三、midship 接进链，但是 opt-in

`MAX_ATTEMPTS["midship"] = 2`、`TIMEOUTS["midship"] = 600`、
`build_cmd("midship")` → `python3 -m midship --project`、`step_done` 补 midship 分支；
但 `main()` 的默认七步**不含它**。理由两条：

1. 它的输入是 `event_inbox` 事件账本，而事件由 ingest / engine 收尾时写入 —— 整链跑完前账本没闭合，
   先投影等于把半截事件推给 gbrain。
2. 它失败会把整链标成 `blocked`，一个旁路模块不该有这个权力。

用法：链跑完后 `python3 adsdesk/chain.py --only midship`。

**踩过的坑（已修）**：`step_done` 最初读 `chain_run`，而 `run_step` 的顺序是
「跑完 → 复验 → 才 record」，所以**第一次成功也必判 failed**，链会一路重试到上限然后报红。
现在由 midship 自己在退出前写 `run_log`（`dataset='midship.project'`），`step_done` 读它 ——
和 rank / market 一个路子。回归用例 `test_step_done_reads_run_log_not_chain_run` 就钉这件事：
先往 `chain_run` 记一条 `ok`，断言 `step_done` 仍为 `False`。

### 四、策略认领闭环（新功能）

`policy` 表原来只写 `proposed`（这是有意的审批闸门），但**没有任何入口把它变成 `active`** ——
`context.py` / `meta.py` 只读 `active`，所以草案永远是草案。

- 新增 `adsdesk/api/routes/policy.py`（216 行）：`GET /pending`、`GET /active`、
  `POST /{id}/activate`、`POST /{id}/reject`。只允许 `proposed → active/rejected`；
  重复点返回 `noop` 不刷时间戳；生效中想悄悄改内容直接 409；
  note 以「· 谁 哪天：内容」追加进 `evidence_text`。
- `adsdesk/api/main.py`：挂 `/api/policy`，`API_VERSION = 3`，`/api/health` 返回它。
- `adsdesk/api/routes/meta.py`：`/freshness` 的 counts 加 `pending_policy` / `open_objection`，
  各自 try/except —— 徽章显示 0 远好过让全局顶栏每页都 500。

### 五、前台重构（Approval.tsx 1213 → 389 行）

硬约束：**不加任何第三方依赖**，`package.json` 的 dependencies 仍只有 `react` + `react-dom`，
图表仍是手写 SVG。完整说明见 `docs/前台改造说明-2026-11-24.md`，此处只列结论：

- **导航**：四视图改常驻挂载（`hidden` 保活），切走不卸载；`pushState` + `popstate` 让后退键可用、`?tab=` 可分享；数字键 1~4 切页。
- **接口版本自检**：后端返 `api_version`，与前端 `EXPECT_API` 不等时顶栏红字直说 —— 单人项目最贵的失败方式是改完前端忘了 build。
- **审批台拆解**：`Approval.tsx` 只留状态/请求/编排，渲染分到 `src/pages/approval/` 9 个文件；右栏改 `.sticky-rail` 粘性跟随，≤1080px 退回静态。
- **执行单**：补 409 闸门（非 `approved` 不渲染「已改」）、进度条 `已改 3/7`、整单复制、单条复制、`提交全部（N）`（走 `POST /api/approve/batch` 整批一个事务，只提交与当前状态不同的条目）。
- **驾驶舱**：折线图全部改 `viewBox` 自适应（废弃写死 `W=720`）；末段未成熟区画灰带 + 虚线；遇 `null` 断线不再跨缺口编值；KPI 六格 → 七格（补 ROAS）；ACOS 与 CVR 并排；归因进度改两段式「窗口第 4/7 天 → 回填缓冲中（剩 2 天）→ 窗口已满等引擎回看」。
- **Agent 页**：新增「待认领策略」卡；反对表加「最近 / 按到期日」切换，后端 `sort=due` 把 `verifiable_at` 为空的排最后（35 条里 26 条没兑现日，前端只能排已返回的 20 条，最近到期的可能根本没进来）。

### 六、文档口径同步

- `docs/工作台-工程计划书-v1.0.md`：链路由「六步」改「七步」（`ingest` / `market` / `rank` / `engine` / `diagnose` / `attribute` / `converge`），
  步表补 midship 行并注明 opt-in；§3.1 标题与架构图同步；§3.3 文件清单补 `midship/` 目录树；§4.4 归因窗表格标 3→5 天变更。
- 新增 `docs/前台改造说明-2026-11-24.md`（140 行）：前台的症状 → 原因 → 改法 → 验证，逐条可复核。
- `midship/README.md`：补「在链上是什么位置」一节，写明 opt-in 理由、用法、以及 `step_done` 读 `run_log` 而非 `chain_run` 的原因。

### 七、验证（全部可复跑）

```bash
cd /Users/baoyanan/自动化广告平台

# 1) 后端回归（纯 unittest，本机没装 pytest）
python3 -m unittest discover -s tests -q
#    Ran 14 tests in 0.223s / OK

# 2) 审批台端到端自测
python3 adsdesk/api/selftest.py
#    == 57 通过 / 0 失败 ==
#    [清理] {'decision_log': 0, 'exec_order': 0, ..., 'sediment': 0}

# 3) 前端类型与构建
cd adsdesk/web && npx tsc -b && npm run build
#    dist/index.html 0.48 kB
#    dist/assets/index-CZRulkNQ.js  211.30 kB │ gzip 69.15 kB
#    dist/assets/index-CI2cosqR.css   7.52 kB │ gzip  2.29 kB

# 4) 守夜人链状态
cd .. && python3 adsdesk/chain.py --status
#    七步全「已完成」

# 5) midship opt-in 实测
python3 adsdesk/chain.py --only midship --force
#    ✔ midship ok —— 今天已投影 N 次（最近 HH:MM:SS）
python3 adsdesk/chain.py --only midship
#    · midship 跳过 —— 今天已投影 N 次（最近 HH:MM:SS）
```

归因门控的实测（这一条最能说明改动生效）：库里唯一一条 `executed` 是 `#153`
（`observe`，`executed_at 2026-11-19T09:25:37+08:00`，`due 2026-11-26`），
`n=7`、`after=11-20~11-26`、`settle_day=11-29`，而事实表只到 `2026-11-23` ——
按日期模拟 11-26 / 11-27 / 11-29 / 12-04 全部「到期 0 条」。
**改之前 11-26 当天就会拿半截数据结算。**

### 八、这一版没做的事（不是遗漏）

- **26 条反对缺 `verifiable_at` 不补录**。这是有意设计：`diagnose.py` 里写死
  「空数组或缺失 verifiable_at 时一律留 NULL —— 宁可这条永远 pending，也不能编一个日期出来」，
  `objection_report.py` 报表里专门有一行 `(无 predicts)` 把它们和「真在等数据」的分开。
  补录等于伪造判据。
- **`agent_assumption` 生产库 0 行**。要等主人第一次点「跳过」才会产生真实假设，脚本不替你造。
- **执行单生产库 0 张**。同理，真库里的执行单只能来自真实决定；副本库自测 57 项已全过。
- **`docs/验收报告-2026-11-23.md` 里的「七步」没改**。那份是 11-23 的历史快照，
  当时的七步与今天的默认七步一致，留原样比改更像证据。

---

## v1.0 · 2026-11-23 及之前

- `832d3f9` 初始提交：广告自动化平台（工作台 v1.0 落地），132 个文件。
  验收口径见 `docs/验收报告-2026-11-23.md`（28 条检查：27 通过 / 0 未通过 / 1 正常跳过）。
- `4917fb9` 补推送工具与说明：`github.com:443` TLS 被掐，改走 Git Data API
  （`tools/push_via_api.py`，blob → tree → commit → ref 内容寻址重建，SHA 与本地逐位一致）。
- `b8dc2ba` 守夜人今晨跑出 2026-11-21 奏折。
