# 给 Antigravity 的审查提示词 v1

> 用途：把下面「提示词正文」整段粘给 Antigravity（或任何能读写仓库 + 跑终端命令的 agentic IDE），让它对 `/Users/baoyanan/自动化广告平台` 做一次**带取证的不变量审计**。
> 交付物：一份可直接归档的审查报告（`docs/审查报告-Antigravity-<日期>.md`）。
> 设计要点：先钉死「钱和人工闸门」这十条不变量，再让它逐条找反例——不给它不变量，它会去挑命名和缩进。

---

## 提示词正文（从下一行开始整段复制）

你是本次审计的唯一执行者。你运行在一个可以读写仓库、执行终端命令的 agentic 环境里，这意味着**你的每一次「顺手修一下」都可能改坏一个正在生产运行的系统**。所以本次任务的第一条规则是：

> **只读审查。不改代码、不 commit、不 push、不装依赖、不动 `ingest/data/ads.sqlite3` 生产库、不启动或重启任何服务、不写 `ingest/data/` 与 `engine/reports/` 下的任何文件。** 需要执行代码时，只跑白名单命令（见 §6），需要写库时只在 `/tmp` 副本上跑。

你的产出是一份审查报告，不是一份 PR。

---

### 1. 这个系统是什么（读一遍，别猜）

这是一个**单人使用的亚马逊广告自动化工作台**，本地跑，单账户。它不是 demo，是每天真实出建议、真实花钱的系统。它的闭环是：

```
积加 ERP（广告数据源）→ ingest 落本地 SQLite → engine 算事实+规则引擎出「奏折」
   → adsdesk/agents 用 LLM 出诊断/归因/收敛 → 前端审批台（人打钩）→ 执行记录 → 归因回看
```

**不可动摇的设计前提**（这些是产品决策，不是技术缺陷，别当 bug 报）：

| 前提 | 含义 |
|---|---|
| 人工闸门唯一 | agent 只出建议单，任何改 bid / 改预算 / 否词都必须由主人在审批台打钩后才成立。系统**没有** Amazon Ads 写权限。 |
| 只读优先 | 广告类 18 个接口全只读；写动作只落在本地 `exec_order` / `decision_log`。 |
| 单账户自我收敛 | 只做本店历史自积累，不做跨账户迁移。 |
| 数据不留在 ERP | 事实数据落平台自建库，这是用户明确选择。 |

**技术栈与规模基线**（用来判断你有没有找错重点）：

- Python 3.13，无 ORM，全部手写 SQL；SQLite（WAL），**39 张表**，生产库 `ingest/data/ads.sqlite3`
- 后端 FastAPI，`127.0.0.1:5273`，`API_VERSION = 3`（`adsdesk/api/main.py`）
- 前端 React + Vite + TypeScript，`adsdesk/web/src/`（`Approval.tsx` 389 行、`Cockpit.tsx`、`pages/approval/` 7 个组件）
- 约 26,000 行 Python（不含 `03-ivyeaops-src/` 第三方源码与 `node_modules`）
- 日更链 `adsdesk/chain.py`：ingest → market → rank → engine → diagnose → attribute → converge（midship 为 opt-in 第 8 步）
- 调度：`~/Library/LaunchAgents/com.baoyanan.ads-chain.plist`（RunAtLoad + StartInterval 1800s + flock 单实例 + 09:00~23:00 时间窗）

**这个项目已经踩过并修过的坑**（`CHANGELOG.md` v1.1 有 9 条、`docs/验收报告-2026-11-23.md` 有 4 条）。**你的价值不在于重复发现它们，而在于找到「同类缺陷的其它实例」**——同一个错误模式往往只修了被抓到的那一处。

---

### 2. 审查目标

不要做通用代码质量评估。你要回答一个问题：

> **这套系统在「数据新的一天」和「主人打钩」这两个动作上，有没有任何一条路径会让它（a）花错钱、（b）在错误的数据上出结论、（c）绕过人工闸门、（d）静默失败而无人知道？**

按此优先级组织审查，风格、命名、注释密度、类型标注、测试覆盖率数值都排在这些之后。

---

### 3. 十条不变量（核心工作量在这里）

对每一条，**必须给出「我找过哪些位置 + 有没有反例」的明确结论**，不允许写「看起来没问题」。

**I1 · 人工闸门不可绕过**
`adsdesk/api/routes/approve.py` 的 `mark_executed` 只允许 `status == "approved"` 通过，其余（含 `proposed`）一律 409。关单 `_close_exec_items` 不得把 `cancelled` 单里的条目标成 `executed_at`。
→ 你要做的：把 `decision_log` / `exec_order` / `exec_order_item` / `ticket` / `param_override` 五张表所有能写「已执行」语义的代码路径全列出来，逐条确认它的前置状态校验。**任何一处写 `in (...)`、`!= "rejected"`、或干脆没校验，都是 P0。**

**I2 · 只读连接不得写库**
`adsdesk/api/db.py` 的 `connect(readonly=True)` 必须只用 `pragma query_only=ON`，`journal_mode=WAL` 只能在读写分支执行。
→ 检查所有 `connect(` 调用点：谁传了 `readonly=True` 却执行了 DDL/`pragma` 写/`insert`；谁该只读却拿了读写句柄。

**I3 · 归因不得在半截数据上结算**
`adsdesk/agents/attribute.py`：`ATTR_SETTLE_LAG_DAYS = 3`，`_window_matured()` 要求数据覆盖到「窗末日 + 3 天」，未覆盖必须 `deferred` 而不是出结论。
→ 检查：`deferred` 的判定是否真的拦住了写库（不是只拦了日志）；有没有第二条路径（如 `converge.py`、`report.py`、`routes/cockpit.py`）绕开 `_window_matured()` 直接读窗口数据下结论。

**I4 · 窗口口径唯一真源**
`ATTRIBUTION_DAYS`（`lower_budget`/`reduce_budget`/`raise_budget` = 5 天）与 `ATTR_SETTLE_LAG_DAYS` 只能定义一次，`routes/cockpit.py` 必须 `from agents.attribute import ...`。
→ 全仓库 `grep` 硬编码的 3 / 5 / 7 天窗口常量，找出任何一处「第二份定义」。数字漂移是本项目已发生过的缺陷类型。

**I5 · 零值不等于缺失**
`engine/facts.py` 的 `_r()` / `_safe_div()`：分母为 0 必须返回 `None`，不能返回 `0`。积加侧语义不一致（campaign 无单时 `acos=0.0`，searchterm 无单时 `null`），**判「无单」只能看 `orders`/`clicks`，不能看 `acos == 0`**。
→ 找出所有把 `0` 与 `None` 混用的地方：`or 0`、`if not x`、`x == 0` 的三元判断、`coalesce(..., 0)` 的 SQL。每一处说明后果（会显示 0% 还是空白、会被规则引擎当成「表现差」还是「无数据」）。

**I6 · 日期上界是排他的**
积加接口 `endDate` 是排他上界：拉 D 必须传 `(D, D+1)`；传 `(D, D)` 会**静默丢最新一天**。`count` 上限 500，`page/pageNo/offset` 被忽略。
→ 检查 `ingest/jijia_client.py` 与 `ingest/ingest.py` 所有日期构造；检查 `ingest/market.py` 的 `_top_window()` 是否满足 sorftime 的 `[今天−4, 今天−2]`、3~40 天窗口约束。检查任何「分页」假设。

**I7 · 跨库/跨路径写入必须设防**
`adsdesk/agents/diagnose.py` 的 `_report_write_guard()` 两道闸：`pragma database_list` 的库路径必须是生产库、md 路径必须在项目根下。
→ 找出其它「读 A 写 B」的位置（尤其 `engine/report.py` 的 `AGENT_OBJECTIONS_START/END` 原位替换、`midship/project.py` 的投影、`tools/push_via_api.py`），确认它们也有等价守卫，或者说明为什么不需要。

**I8 · 链的完成判定只认库真状态**
`adsdesk/chain.py` 的 `step_done()` 不得读 `chain_run` 当完成凭据；midship 的完成要读 `run_log`（`dataset='midship.project'`）。`freshness` 闸门要求「今天 ok 且晚于最后一次 ingest 成功时刻」。
→ 检查 `engine_run`（UTC `...Z`）与 `chain_run`（`+08:00`）的**时间字符串比较**：任何直接 `>` 比较两个不同时区格式的字符串，都会把「刚跑完」判成更早。这是本项目已踩过的坑，确认它只修了一处还是全修了。

**I9 · 幂等**
`ingest/ingest.py` 的 upsert 必须让「同一天重跑两次」行数不变。
→ 检查所有 `insert` 是否都带 `on conflict ... do update`；检查 `verify.py` 的幂等校验是否覆盖五张事实表；检查 `midship`、`rank`、`market` 的重复投影。

**I10 · 失败必须可见**
任何 `except: pass`、`except Exception: return 0`、`except: continue`，都必须说明：失败会体现为「0」还是「空」还是「无数据」？下游会不会把它当成「表现正常」？
→ 重点看：`agents/gateway.py` 的 `chat_chain()` 全通道降级（全挂之后返回什么？调用方分得清「模型挂了」和「模型说没问题」吗？）、`routes/meta.py` 的 per-field `try/except`、`ingest/market.py` 的「单节点空则跳过 / 全空则抛错」逻辑。

---

### 4. 六个横向维度（不变量之外）

1. **SQL 正确性**：39 张表的 join 是否会产生行膨胀（一对多未去重导致金额翻倍）；`sum()` 遇到全 `NULL` 返回 `NULL` 是否被当成 0；`where` 里日期比较的边界（`>=` / `<=` / 排他）。
2. **并发与事务**：`flock` 单实例是否覆盖所有入口（手工 `python3 adsdesk/chain.py` 与 launchd 同时跑会怎样）；SQLite `busy_timeout` 是否覆盖长事务；`connect()` 的提交/回滚路径。
3. **本地服务攻击面**：`:5273` 绑 `127.0.0.1` 但无鉴权——如果主人开着浏览器访问了恶意页面，CSRF / DNS rebinding 能对 `/api/policy/{id}/activate`、`/api/approve` 做什么？（这是「人工闸门」的真实威胁模型，请具体到可执行请求，不要泛泛而谈。）
4. **前后端契约漂移**：`API_VERSION = 3`、`/api/health` 字段、`settle_at` / `attribution_due_at` 的改名——前端 `lib/api.ts` 与 `pages/` 是否有读旧字段名的残留（会静默显示 `undefined` / `NaN`）。
5. **文档与实现漂移**：`docs/工作台-工程计划书-v1.0.md`（1316 行，第十二节是验收清单，`tools/acceptance.py` 28 条）与代码现状是否一致；`CHANGELOG` 声称修了 9 条缺陷，`tests/test_bugfixes.py` 是否真的每条都有对应用例（14 项）；有没有「文档说做了、代码里没有」或反之。
6. **回归覆盖缺口**：现有测试是 `tests/test_bugfixes.py`(14，已实测全过) + `engine/test_r31.py`(15) + `engine/test_sediment.py`(39) + `midship/test_midship.py` + `adsdesk/api/selftest.py`(57)，全部是纯 unittest/脚本风格、**本机无 pytest**。指出**上述十条不变量里哪几条完全没有测试保护**——这比"覆盖率低"有用。

---

### 5. 输出格式（严格遵守）

先写一节 **「一句话结论」**：这套系统现在能不能信、最该先修哪一条。

然后 **问题总表**，按严重度排序，一行一条：

| # | 级别 | 位置 | 一句话症状 | 后果（钱/数据/闸门） | 修法 |
|---|---|---|---|---|---|

级别定义：

- **P0**：会让钱算错、让错误数据进结论、让「人打钩」这道闸门失效、或让失败静默成「正常」
- **P1**：不会立刻出错，但在特定真实条件下会出错（数据延迟、模型全挂、断网、重跑、并发）
- **P2**：契约漂移、可维护性、可观测性缺口（出问题时你会查不到原因）
- **P3**：风格、命名、注释

然后 **逐条详证**，每条必须有这五样，缺一样视为无效发现：

1. `file.py:行号` + 相关代码原文（不超过 10 行）
2. **触发条件**：什么真实输入/时序会走到这里（写具体值，不要写「某些情况下」）
3. **实际后果**：屏幕上会看到什么、库里会多出什么、下一个 agent 会据此做什么错事
4. **取证方式**：你实际跑过的只读命令与它的输出（sqlite3 select / grep / pytest / 构造的输入）；没跑过的写 `UNVERIFIED`，并说明为什么没跑
5. **修法**：最小改动，说明改完怎么验证（能写成一条 `pytest` 或一条 `sqlite3` 断言最好）

最后两节：

- **「我验证了什么 / 我没验证什么」**：列出你实际执行过的命令清单，以及因只读约束而跳过的部分（例如「未在 `/tmp` 副本上跑全链，因此 §X 的结论来自静态阅读」）。
- **「不建议改的地方」**：你看着别扭但**其实是刻意设计**的地方（例如 `proposed` 状态的存在、midship 不在默认七步里、`chain_run` 不当完成凭据）。这一节用来证明你读懂了系统，而不是在套模板。

---

### 6. 允许执行的命令白名单

只读、可重复、不产生副作用：

```bash
git log --oneline -20 ; git status --short ; git diff <a> <b> --stat
grep -rn "<pattern>" --include=*.py --include=*.ts --include=*.tsx --include=*.sql .
python3 -c "import sqlite3; ..."        # 只 select，且可加 mode=ro
sqlite3 -readonly ingest/data/ads.sqlite3 ".tables" / "select ..."
python3 tests/test_bugfixes.py           # 14 项回归用例（纯 unittest，本机没装 pytest）
python3 engine/test_r31.py               # 15 项卡位规则分支（会复制真库到临时目录）
python3 engine/test_sediment.py          # 39 项沉淀回路
python3 midship/test_midship.py          # 事件中台
python3 -m unittest discover -s tests -p 'test_*.py'
python3 adsdesk/chain.py --status        # 只看状态
python3 tools/acceptance.py              # 只读验收检查
```

注意：**本机没有 pytest**（`import pytest` → `ModuleNotFoundError`），这是刻意选择——仓库全部测试是纯脚本风格，`python3 tests/test_bugfixes.py` 直接跑。别为了跑测试去 `pip install pytest`。

禁止：

```bash
python3 adsdesk/chain.py                 # 会真跑 ingest / 烧模型额度
python3 ingest/ingest.py                 # 会调积加接口（60s 熔断）
任何 write / update / delete / drop / insert 到 ingest/data/ads.sqlite3
任何 launchctl load / bootout / kickstart
任何 pip install / npm install / git commit / git push
任何写 engine/reports/ 或 ingest/data/ 的命令
```

需要验证「写库行为」时：`cp ingest/data/ads.sqlite3 /tmp/audit-copy.sqlite3`，用 `ADS_DB=/tmp/audit-copy.sqlite3` 环境变量在副本上跑。

---

### 7. 如果你时间有限

按这个顺序做，做到哪算哪，并在报告里明确标注「本次只覆盖了 §X」：

1. **I1 人工闸门**（半小时，最高价值——这道闸门失效，整个系统的存在意义就没了）
2. **I5 零值与缺失**（一小时，会污染所有下游判断，且错误很隐蔽）
3. **I3 + I4 归因窗口**（一小时，直接决定配额升降，错了会连续错很多天）
4. **I8 时间比较与时区**（半小时，机械但必错的地方）
5. **I10 静默失败**（一小时，决定你出问题时能不能查出来）
6. 其余

---

## 提示词正文结束

---

## 怎么用（给 YG，不是给 Antigravity）

**第一轮**：把上面整段粘给 Antigravity，让它按 §7 顺序做。它大概率会想改代码——**明确回一句「只出报告，不要改」**。

**第二轮**：把第一轮的 P0 列表单独发回去，只问一句：「这三条，给我一个能在 `/tmp` 副本上复现的最小脚本」。复现得出来的才是真 P0，复现不出来的降级。

**第三轮**：修完之后，把 `git diff` 发回去，只问一句：「按 §3 十条不变量，逐条确认这次 diff 有没有引入新的反例」。

**别做的事**：不要让它「顺手重构一下」——这套系统的价值在闸门和口径一致性，重构最伤这两样。也不要让它把 `03-ivyeaops-src/` 一起审（那是第三方源码，`node_modules` 同理）。
