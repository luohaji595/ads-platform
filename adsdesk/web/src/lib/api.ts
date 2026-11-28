/** 唯一的网络层。所有请求都从这里出去，方便一眼看清「前端碰了哪些接口」。 */

const BASE = '/api'

/**
 * 前端期望的后端接口版本，必须与 adsdesk/api/main.py 的 API_VERSION 一致。
 * 不一致时顶栏会红字提示：这个项目只有一个人改，最容易的失败不是写错逻辑，
 * 而是「改了接口没重启」或「改了前端没 build」，然后对着一个空白页面猜半天。
 */
export const EXPECT_API = 3

export type Health = { ok: boolean; api_version?: number; db: string; db_exists: boolean }

export const getHealth = () => get<Health>('/health')

function formatErrorDetail(detail: unknown): string | null {
  if (!detail) return null
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) {
    const parts = detail
      .map((item) => {
        if (typeof item === 'string') return item
        if (item && typeof item === 'object') {
          const rec = item as { loc?: unknown[]; msg?: unknown }
          const loc = Array.isArray(rec.loc)
            ? rec.loc.filter((x) => x !== 'body' && x !== 'query' && x !== 'path').join('.')
            : ''
          const msg = rec.msg != null ? String(rec.msg) : JSON.stringify(item)
          return loc ? `${loc}: ${msg}` : msg
        }
        return String(item)
      })
      .filter(Boolean)
    return parts.length ? parts.join('；') : null
  }
  if (typeof detail === 'object') {
    const rec = detail as { message?: unknown; error?: unknown }
    if (typeof rec.message === 'string') return rec.message
    if (typeof rec.error === 'string') return rec.error
    return JSON.stringify(detail)
  }
  return String(detail)
}

async function j<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(BASE + path, {
    headers: { 'Content-Type': 'application/json' },
    ...init,
  })
  const text = await r.text()
  let body: any = null
  try {
    body = text ? JSON.parse(text) : null
  } catch {
    body = { detail: text }
  }
  if (!r.ok) {
    const detail = formatErrorDetail(body?.detail)
    throw new Error(detail || `HTTP ${r.status}`)
  }
  return body as T
}

export const get = <T,>(p: string) => j<T>(p)
export const post = <T,>(p: string, body: unknown) =>
  j<T>(p, { method: 'POST', body: JSON.stringify(body) })

// ---------------------------------------------------------------- 类型

export type Freshness = {
  now: string
  tables: Record<string, { last_date: string | null; age_hours: number | null; limit_hours: number; stale: boolean }>
  stale_any: boolean
  last_run: { run_id: number; status: string; decisions: number; suppressed: number; window_start: string; window_end: string } | null
  last_memorial: { run_id: number; business_day: string; bytes: number; agent_state: string } | null
  /** pending_policy / open_objection 是后加的（老后端不带，所以可选）：
   *  前者是归因 agent 攒出来、等人点一下才生效的策略条数；
   *  后者是 agent 提了反对、但还没到兑现判定日的条数。 */
  counts: {
    proposed: number
    approved: number
    executed: number
    pending_policy?: number
    open_objection?: number
  }
}

/** 该活动最紧的那档位次（ratio 最大的一天一个位次） */
export type Tightest = {
  placement: string
  report_date: string
  clicks: number
  cost: number
  cpc: number
  bid: number
  bid_adjustment: number
  cap: number
  ratio: number
  strategy?: string[]
} | null

export type CampaignInConflict = {
  campaign_id: string
  campaign_name: string
  clicks: number
  orders: number
  cost: number
  sales: number | null
  bid: number
  /** 该活动全位次混合均价，只说明规模，不是判定依据 */
  cpc: number | null
  cpc_all_placements?: number | null
  cvr: number | null
  acos: number | null
  share: number
  /** 最紧那档位次（逐日 × 逐位次算出来的） */
  tightest?: Tightest
  bid_values?: number[]
  adj_values?: number[]
  strategies?: string[]
  config_changed?: boolean
  /**
   * ok       正常
   * near     接近该位次上限（≥95%）
   * mismatch 当日配置与实际扣费对不上，去核那一天
   * unknown  窗口内没有可判数据
   */
  flag?: 'ok' | 'near' | 'mismatch' | 'unknown'
}

/**
 * 一处「去向」= 打这一钩会落到哪张表。
 *
 * 由引擎出题时算好（engine/report.py `_build_questions`），审批侧只照抄写库。
 * 为什么不在这里算：参数值（放宽到多少、步长多少）是业务口径，散在前后端
 * 两处就一定会漂。
 */
export type OptionMeta = {
  reason_code: string | null
  /** policy | param_override | observation_task | experiment | reason_hit */
  destination: string
  scope?: string
  object_id?: string
  param_key?: string
  param_value?: string | number
  ttl_days?: number
  policy_type?: string
  metric?: string
  hypothesis?: string
  judge_rule?: string
  /** 这个码已经沉淀过、还在生效期内 —— 前端置灰用 */
  settled_table?: string
  settled_id?: number
  settled_until?: string
  /** 这个码你划掉过，静默到哪天 */
  excluded?: boolean
  exclude_until?: string
}

export type Question = {
  id: string
  /**
   * 奏折内唯一的位置标识（形如 `Q01#2`）。`id` 只是题号，
   * 同一份奏折里会重复（Q01 可以问 3 个不同的词），
   * 打钩时要用 qkey 定位，否则永远命中第一道同号题。
   * 老奏折没有这个字段 —— 前端回退到 id。
   */
  qkey?: string
  object: string
  ask: string
  options: string[]
  reason_codes: string[]
  /**
   * 与 options 同下标。老奏折（S3 之前落库的）没有这个字段，
   * 前端必须容忍缺失 —— 否则历史报告会白屏。
   */
  option_meta?: (OptionMeta | null)[]
  decision_ref?: string
}

export type Decision = {
  decision_id: number | null
  rule_id: string
  entity_type: string
  entity_id: string
  entity_label: string
  action_type: string
  action_label: string
  current_value: string | null
  proposed_value: string | null
  spend_impact: number | null
  confidence: 'high' | 'medium' | 'low'
  rationale: string
  calculation?: any[]
  significance?: Record<string, any>
  campaign_id?: string | null
  status?: string
  checked_at?: string | null
  checked_by?: string | null
  executed_at?: string | null
  attribution_due_at?: string | null
  // 证据不够（observing）的条目：引擎给出的「为什么按住」+ 人强制批准后留下的来源
  gate_reason?: string | null
  held?: boolean
  override_by?: string | null
  override_reason?: string | null
  overridden_at?: string | null
  // 历史战绩：这条规则过去几次执行的结果、这个对象自己几次被改坏过。
  // 引擎只在攒够 3 次有方向的结果时才给，攒不够就整条不出现。
  rule_record?: string | null
  object_record?: string | null
  objections?: { verdict: string; why: string }[]
}

export type LayerEvidence = {
  layer: string
  label: string
  significant: boolean | null
  p_value: number | null
  groups: number | null
  clicks: number | null
  orders: number | null
  /** false = 这一层不受本组合限制（组合层覆盖全店） */
  scoped?: boolean | null
  covered_clicks?: number | null
  base_clicks?: number | null
  /** covered_clicks / base_clicks。小于 1 说明这层没覆盖全部点击 */
  coverage?: number | null
}

/**
 * 一条被规则自己拦下的动作（engine/rules.py RuleResult.suppress）。
 *
 * 它和「只看不动」不是一回事：
 *   suppressed = 规则算出来了，但被自己的保护闸门掐掉，**连建议都不算**；
 *   observing  = 规则给出建议了，只是置信度低或被观察期锁住，**是建议但不动**。
 *
 * 这两种以前都只在 md 底稿里写，网页上看不见，于是「今天什么都没有」——
 * 其实今天有 19 条被拦、每条都写着为什么。这里把它搬上来。
 *
 * 注意字段名是 entity（不是 entity_label），且没有 spend_impact / decision_id。
 */
export type Suppressed = {
  rule_id: string
  entity: string
  reason: string
  detail?: Record<string, any>
}

export type Pack = {
  window_start: string
  window_end: string
  run_id: number | null
  front: Decision[]
  questions: Question[]
  conflicts: { keyword_text: string; campaigns: CampaignInConflict[] }[]
  conflict_count: number
  overflow_count: number
  below_count: number
  saved: number
  spent: number
  target_reachable: boolean
  layer_evidence: LayerEvidence[]
  portfolio_headline: string
  /**
   * 规则给了建议、但今天不动的那批（action_type === 'observe'，外加被观察期锁住的）。
   * rationale 里会写明是「数学按住的」（confidence=low）还是「你自己批的先观察」。
   */
  observing?: Decision[]
  /** 已达判定线、被配额挤到正文外的。 */
  overflow?: Decision[]
  /** 算过了、但样本没攒够的。 */
  below?: Decision[]
  /** 被规则自己的保护闸门拦下的，连建议都不算。 */
  suppressed?: Suppressed[]
  /** 引擎旁注：哪些被观察期挡下、哪些已表过态不再重复列出。 */
  notes?: string[]
  /** 今天最贵的几笔钱各自谁在管。规则层各自看不见合成结果，只有报告层看得见。 */
  who_manages?: WhoManages[]
  /**
   * 账户级告警（R30 等）。它们 spend_impact=0、没有可执行动作，
   * 所以不进 front 也不进待批 —— 但必须显示：这是引擎唯一会主动说的话，
   * 藏起来等于让人以为「今天什么都没发生」。
   */
  alerts?: Decision[]
}

export type WhoManages = {
  campaign_id: string
  campaign: string
  cost: number
  orders: number
  /** 有动作 / 被锁 / 证据不够 / 未达线 / 无人管 */
  kind: string
  how: string
}

/** 诊断 agent 的一次运行。agent 只出「反对」和「疑问」，没有「建议」。 */
export type AgentQuestion = {
  id: string
  object: string
  ask: string
  options: string[]
  reason_codes: string[]
  impact: number | null
  uncertainty: number | null
}

export type AgentObjection = {
  objection_id: number
  decision_id: number
  verdict: 'oppose' | 'doubt'
  why: string
  evidence: string
}

export type AgentEval = {
  eval_id: number
  claim: string
  verifiable_at: string
  method: string
  expected: string
  verdict: 'pending' | 'correct' | 'wrong' | 'unverifiable'
  actual: string | null
}

/** 断言兑现率：跨全部 agent_run 累计，只算 correct + wrong。 */
export type AgentScore = {
  correct: number
  wrong: number
  /** correct + wrong。0 表示还一条都没判出来（不是全错）。 */
  judged: number
  unverifiable: number
  pending: number
  /** 真的进过判定的条数（判出对错 + 判不了）。分母太小时不该读比率。 */
  verifiable?: number
  /** 可验证率 = judged / verifiable。低说明它把断言写成了没法验的形式 —— 那个比「准不准」更早能改。 */
  verifiable_rate?: number | null
  /** null = 还没判过任何一条；有值才是「准不准」。 */
  rate: number | null
  last_checked: string | null
}

export type AgentView = {
  state: 'ok' | 'failed' | 'never_ran' | 'running'
  score?: AgentScore
  agent_run_id?: number
  /** 这条 agent 输出实际评的是哪个 run。引擎重跑过时它会小于当前 run_id。 */
  based_on_run?: number | null
  /** true = 这条是「同一天、更早那个 run」上跑的，输入可能和当前这版不同 */
  fallback?: boolean | null
  model?: string
  prompt_ver?: string
  started_at?: string
  finished_at?: string
  tokens?: [number | null, number | null]
  input_digest?: string
  error?: string | null
  questions?: AgentQuestion[]
  objections?: AgentObjection[]
  evals?: AgentEval[]
}

export type MemorialToday = {
  run_id: number
  business_day: string
  report_path: string
  agent_state: string
  created_at: string
  pack: Pack
  decisions: Decision[]
  agent: AgentView
}

export type FunnelPoint = {
  date: string
  cost: number
  clicks: number
  orders: number
  sales: number
  cpc: number | null
  cvr: number | null
  acos: number | null
}

/**
 * 外部市场一条「发生了什么」。竞品动作和 ABA 名次变动后端已归一成同一形状
 * （kind 区分来源），前端只画一张表 —— 否则每加一种数据源就要改一次面板。
 */
export type MarketMove = {
  kind: 'competitor' | 'aba'
  /** 竞品是 ASIN，ABA 是搜索词 */
  subject: string
  label: string | null
  move_type: string
  move_label: string
  field: string
  field_label: string
  old_value: string | null
  new_value: string | null
  delta: number | null
  /** ABA 名次的相对变动（占当前名次的百分比）。名次是长尾分布，绝对值不能横比：
   *  150 万名的词跳 42 万名，和在 1000 名处跳 142 名不是一回事。 */
  rel_pct?: number | null
  observed_at: string | null
  observed_date: string | null
  source: string
  note?: string
}

/** 最新一周里**自家搜索词**的 ABA 名次（不是全站热榜）。
 *  rank 是 ABA 搜索频次排名，数字越小越热；rank_gain = 上周 - 本周，正数 = 名次上升。 */
export type MarketAbaRow = {
  week: string
  term: string
  term_cn: string | null
  rank: number | null
  rank_gain: number | null
  search_volume: number | null
  click_share: number | null
  /** Top3 品牌销量占比 %。keyword_detail 本身只给品牌名不给占比，
   *  这个数是 keyword_search_results 的每项份额（每页合计 100）按品牌加总取前三得来的。 */
  top3_brand_share?: number | null
  cpc_bid: number | null
}

/** 品类/竞品/词的最近一天行情。price 只对竞品行有意义。 */
export type MarketDailyRow = {
  subject_type: string
  subject_id: string
  subject_name: string | null
  report_date: string
  avg_price: number | null
  top100_sales_volume: number | null
  top3_brand_share: number | null
  product_count: number | null
  price: number | null
  /** 竞品在它自己那个子类里的排名（product_detail 的 sub_category[0][2]）。
   *  不是 category_products 那个更大范围的总榜排名 —— 两者差三个数量级。 */
  subcategory_rank?: number | null
}

export type CockpitSummary = {
  funnel: { window: { start: string; end: string }; series: FunnelPoint[]; totals: Record<string, number | null> } | { error: string }
  portfolios: { portfolios: { name: string; clicks: number; orders: number; cvr: number | null; cvr_lo: number; cvr_hi: number; cost: number; acos: number | null }[] } | { error: string }
  conflicts: { conflict_count: number; abnormal_count: number; conflicts: { keyword_text: string; campaigns: CampaignInConflict[] }[] } | { error: string }
  keywords: { keywords: { keyword_text: string; clicks: number; orders: number; cost: number; cpc: number | null; cvr: number | null; acos: number | null; campaign_count: number }[] } | { error: string }
  market:
    | {
        status: string
        note: string | null
        /** 各表行数，用来判断「表建好了但还没采到」 */
        counts?: { aba: number; moves: number; daily: number; watchlist: number }
        latest_week?: string | null
        /** 竞品动作 + ABA 名次变动，按时间倒序 */
        watched?: MarketMove[]
        aba?: MarketAbaRow[]
        /** 最新一周库里一共有多少个词。aba 只给前 10 行，标题要写「前 10 / 共 N」。 */
        aba_total?: number
        markets?: MarketDailyRow[]
      }
    | { error: string }
  attribution: {
    open: any[]
    open_count: number
    due_count: number
    /** 已回填的结论（含 outcome_7d / outcome_14d 里的 verdict 与 basis）。 */
    filled?: any[]
    filled_count?: number
  } | { error: string }
}
// ---------------------------------------------------------------- 对话

/** 一条回答站在什么上面：引到的表 + 引到的 decision_id + 有没有诚实指向「没落库」那条。 */
export type ChatAnchors = {
  tables: string[]
  decision_ids: string[]
  null_decision: boolean
}

/** 存进 chat_turn.cited_tables 的溯源；除了引用的表，还带着「这轮看的是哪份奏折」。 */
export type ChatCite = {
  run_id: number | null
  prompt_ver: string | null
  cited_tables: string[]
  cited_decision_ids: string[]
  null_decision: boolean
}

export type ChatTurn = {
  turn_id: number
  role: 'user' | 'assistant' | string
  content: string
  created_at: string
  cite: ChatCite
}

/**
 * 一轮的应答。失败时后端也回 200（回答是真的，只是没落库），所以 ok / written 都要看：
 * ok=false 且 reply 有值 = 话可以照看，但它在库里查不到，回看时别当成留痕。
 */
export type ChatSay = {
  ok: boolean
  written?: boolean
  warning?: string
  turn_id?: number
  user_turn_id?: number
  reply: string
  model: string | null
  tokens?: { in: number; out: number }
  elapsed_ms: number
  cited_tables?: string[]
  cited_decision_ids?: string[]
  anchors?: ChatAnchors
  run_id?: number
  business_day?: string
}

export const sayChat = (body: {
  text: string
  as_of?: string | null
  model?: string | null
  session_id?: string | null
}) => post<ChatSay>('/chat/say', body)

export const getChatHistory = (limit = 50, sessionId = 'web') =>
  get<{ ok: boolean; session_id: string; turns: ChatTurn[] }>(
    `/chat/history?limit=${limit}&session_id=${encodeURIComponent(sessionId)}`,
  )

// ───────────────────────────── Agent 面板 ─────────────────────────────
// 只读账本：agent 当时说的话，后来对不对。没有写接口。

export type AgentRunsBlock = {
  total: number
  ok: number
  failed: number
  running: number
  last_at: string | null
  error?: string
}

export type AgentObjectionsBlock = {
  total: number
  pending: number
  checked: number
  correct: number
  wrong: number
  unverifiable: number
  error?: string
}

export type AgentEvalsBlock = {
  total: number
  pending: number
  correct: number
  wrong: number
  unverifiable: number
  /** correct /(correct+wrong)；分母 0 时后端给 null，前端要显示「暂无到期断言」 */
  hit_rate: number | null
  error?: string
}

export type AgentAssumptionsBlock = { open: number; error?: string }

export type AgentSummary = {
  runs: AgentRunsBlock
  objections: AgentObjectionsBlock
  evals: AgentEvalsBlock
  assumptions: AgentAssumptionsBlock
  note: string | null
}

export type AgentRunRow = {
  agent_run_id: number
  job: string
  run_id: number | null
  status: 'ok' | 'failed' | 'running' | string
  started_at: string
  finished_at: string | null
  elapsed_s: number | null
  model: string | null
  tokens_in: number | null
  tokens_out: number | null
  error: string | null
  n_objections: number
  n_evals: number
}

/** predicts_json 里的一条可验证预测（历史行大多为空数组） */
export type AgentPredict = {
  claim?: string
  verifiable_at?: string
  method?: string
  expected?: string
}

export type AgentObjectionRow = {
  objection_id: number
  decision_id: number
  verdict: 'oppose' | 'doubt' | string
  why: string
  evidence: string | null
  created_at: string
  outcome: 'pending' | 'correct' | 'wrong' | 'unverifiable' | string
  predicts: AgentPredict[]
  verifiable_at: string | null
  actual: string | null
  judged_by: string | null
}

export type AgentObjectionSummary = {
  total: number
  pending: number
  checked: number
  correct: number
  wrong: number
  unverifiable: number
}

export type AgentEvalRow = {
  eval_id: number
  agent: string
  claim: string
  method: string
  expected: string
  verifiable_at: string
  verdict: 'pending' | 'correct' | 'wrong' | 'unverifiable' | string
  actual: string | null
  checked_at: string | null
}

export type AgentEvalByAgent = {
  agent: string
  total: number
  pending: number
  correct: number
  wrong: number
  unverifiable: number
  hit_rate: number | null
}

export type AgentAssumptionRow = {
  assumption_id: number
  object_id: string
  question_code: string
  default_action: string
  created_at: string
  review_at: string
  result: string | null
}

export type AgentOutcomeFilter = 'all' | 'pending' | 'correct' | 'wrong' | 'unverifiable'
export type AgentVerdictFilter = 'all' | 'pending' | 'correct' | 'wrong' | 'unverifiable'

export const getAgentSummary = () => get<AgentSummary>('/agents/summary')

export const getAgentRuns = (limit = 20) =>
  get<{ runs: AgentRunRow[]; note: string | null }>(`/agents/runs?limit=${limit}`)

/** sort='due' 按兑现日从近到远（没写日期的排最后），'recent' 按记录倒序。 */
export const getAgentObjections = (
  limit = 20,
  outcome: AgentOutcomeFilter = 'all',
  sort: 'recent' | 'due' = 'recent',
) =>
  get<{
    objections: AgentObjectionRow[]
    summary: AgentObjectionSummary | null
    note: string | null
  }>(`/agents/objections?limit=${limit}&outcome=${outcome}&sort=${sort}`)

export const getAgentEvals = (limit = 30, verdict: AgentVerdictFilter = 'all') =>
  get<{
    evals: AgentEvalRow[]
    by_agent: AgentEvalByAgent[]
    summary: AgentEvalByAgent | null
    note: string | null
  }>(`/agents/evals?limit=${limit}&verdict=${verdict}`)

export const getAgentAssumptions = () =>
  get<{ assumptions: AgentAssumptionRow[]; note: string | null }>('/agents/assumptions')


// ───────────────────────────── 策略库（认领） ─────────────────────────────
// 归因 agent 只写 proposed；人在这里点一下才变 active，engine 下次跑就按它行事。
// 这是整个工作台除了审批之外**唯一**会改变系统未来行为的写动作。

export type PolicyRow = {
  policy_id: number
  scope: string
  object_id: string
  policy_type: string
  /** 人话短语，如「该对象降价后 7/14 天实测有效 → 下次同类信号直接降」 */
  type_label: string
  evidence_text: string | null
  /** 形如 `归因agent#123-14d-2026-11-24`，是这条结论的出生证明 */
  source: string
  /** 从 source/params 里抠出来的来源决策，用于回看当时那条建议 */
  decision_id: number | null
  params: Record<string, unknown>
  status: string
  created_at: string
  reviewed_at: string | null
  expires_at: string | null
}

/**
 * `/policy/active` 比 `/policy/pending` 多一个 `expired`：过期条目单独列出来，
 * 而不是藏掉 —— 「这条规矩消失了」和「还在库里只是不再执行」处置完全不同。
 * 做成可选是因为 pending 那个端点不返回它。
 *
 * 注意：这个端点目前前端没有任何调用点（`getPolicyActive` 定义了但没人用），
 * 所以「当前规矩」面板其实还没建。类型先按真实响应写，等面板建起来时不用改。
 */
export type PolicyList = {
  policies: PolicyRow[]
  expired?: PolicyRow[]
  note: string | null
}

export const getPolicyPending = () => get<PolicyList>('/policy/pending')
export const getPolicyActive = () => get<PolicyList>('/policy/active')

/** 认领：proposed → active。note 会追加进 evidence_text，记下「当时凭什么」。 */
export const activatePolicy = (policyId: number, note?: string) =>
  post<{ ok: boolean; detail: { policy_id: number; status: string; noop: boolean; reviewed_at: string | null } }>(
    `/policy/${policyId}/activate`,
    { actor: 'owner', note: note || null },
  )

/** 驳回：proposed → rejected。留下 reviewed_at，说明「人看过、没认」。 */
export const rejectPolicy = (policyId: number, reason?: string) =>
  post<{ ok: boolean; detail: { policy_id: number; status: string; noop: boolean; reviewed_at: string | null } }>(
    `/policy/${policyId}/reject`,
    { actor: 'owner', reason: reason || null },
  )
