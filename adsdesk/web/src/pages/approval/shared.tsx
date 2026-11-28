/** 审批台的共用件：常量、人话映射、以及几个「把库里的字段翻成中文」的函数。
 *
 *  为什么单独拎出来：它们被 5 个组件同时用到。留在 Approval.tsx 里就会变成
 *  「组件 import 页面、页面 import 组件」的循环依赖，Vite 打包时不报错但运行时
 *  会拿到 undefined —— 这类错误最难查，所以一开始就分开。
 */
import type { Decision, Suppressed } from '../../lib/api'
import { fmtMoney, fmtPct } from '../../lib/charts'

export function Rich({ text }: { text: string }) {
  const parts = String(text).split(/\*\*(.+?)\*\*/g)
  return (
    <>
      {parts.map((part, i) =>
        i % 2 === 1 ? <b key={i}>{part}</b> : <span key={i}>{part}</span>,
      )}
    </>
  )
}

/**
 * 四类去向的人话名。engine/reason_routes.py 的 destination() 给的是表名，
 * 这里只做翻译 —— 不要在别处再写一套映射，否则改了词表两头对不上。
 */
export const DEST_LABEL: Record<string, string> = {
  policy: '记住这条（策略）',
  param_override: '调参数',
  observation_task: '排观察任务',
  experiment: '开实验',
  reason_hit: '只留痕',
}

export const CONF_LABEL: Record<string, string> = {
  high: '证据够',
  medium: '证据一般',
  low: '证据不足',
}

/**
 * 证据标签。跟 engine/report.py 的 `_evidence_label` 同一个口径 ——
 * 报告和网页说两套话，主人会以为是两件事。
 *
 * R32（止损线）的 confidence 必然是 low：它明说了「统计上判不了」，
 * 依据是「已花掉几单的钱」。直接印「证据不足」正好把它的依据说反了。
 */
export function evidenceLabel(d: Decision): string {
  const sig: any = d.significance || {}
  if (sig.basis === 'cost_ceiling') {
    const units = sig.cost_evidence?.units
    return units != null ? `止损线（已花 ${Number(units).toFixed(2)} 单的钱）` : '止损线'
  }
  return CONF_LABEL[d.confidence] || d.confidence
}

/**
 * 动作枚举的人话名。
 *
 * 引擎在「不是改一个数」的动作上会把枚举直接写进 proposed_value：
 * pause_or_restructure / consider_new_ad_group / pause / increase / reduce。
 * 原来前端一路纯文本插值，主人看到的是 `0.8811 → pause_or_restructure` ——
 * 意思是「这个价位的词建议停掉或重构」，但读的人得先猜这个词是什么意思。
 *
 * 只翻译「整个值就是这个枚举」的情况；像「20%（从 0% 提）」这种已经把话说清了的
 * 中文串原样放行，不去动它 —— 翻译的边界是「英文枚举 → 中文」，不是「改写引擎的话」。
 */
export const ACTION_TEXT: Record<string, string> = {
  pause: '停用',
  pause_or_restructure: '停用或重构（否词 / 换匹配方式）',
  consider_new_ad_group: '考虑拆到独立广告组',
  increase: '提高',
  reduce: '降低',
}

export function showValue(v: string | number | null | undefined): string {
  if (v == null || v === '') return '—'
  const s = String(v)
  return ACTION_TEXT[s] || s
}

export type Mark = Record<string, 'approve' | 'reject'>

/**
 * 一道题的稳定定位键。`id` 只是题号（Q01/Q08…），同一份奏折里会重复 ——
 * 拿 id 当 key 会让「给第 2 个词打的钩」覆盖掉「第 1 个词」，提交时后端
 * 也只按 id 找第一条。老奏折没有 qkey，回退到 id（那时同号题确实只有一道）。
 */
export function qkeyOf(q: { id: string; qkey?: string }): string {
  return q.qkey || q.id
}

export function gateText(d: Decision): string {
  if (d.gate_reason) return d.gate_reason
  const s = d.significance || {}
  const clicks = s.clicks
  const orders = s.orders
  const need = s.min_clicks_needed
  if (clicks != null && need != null) {
    return `证据不够：${clicks} 击${orders != null ? ` ${orders} 单` : ''}；判到判定线需 ${need} 击`
  }
  return '证据不够、未达判定线（这条是旧账本，当时没记下差多少）'
}

/** 置信度的人话。别直接印 low/medium —— 那是给代码看的。 */
export const CONF_TEXT: Record<string, string> = {
  high: '置信度高',
  medium: '置信度中',
  low: '置信度低',
}


export function suppressedWhy(s: Suppressed): string {
  const d = s.detail || {}
  const num = (v: any) => (typeof v === 'number' ? v : null)

  switch (s.reason) {
    case '空转': {
      const bid = num(d.bid)
      const computed = num(d.computed)
      return `当前 $${bid?.toFixed(2)}，算出来 $${computed?.toFixed(2)}，差不到一个步长。${d.note || ''}`
    }
    case '花费闸门': {
      const cost = num(d.cost)
      const gate = num(d.gate)
      const gi = d.gate_inputs || {}
      return `花了 ${fmtMoney(cost)}，一单允许的花费上限是 ${fmtMoney(gate)}（客单价 ${fmtMoney(gi.aov)} × 目标 ACOS ${fmtPct(gi.target_acos, 0)}）。还没花到该动的时候。`
    }
    case 'CVR 低于盈亏平衡': {
      return `这个词的 CVR ${fmtPct(num(d.kw_cvr))}，盈亏平衡要 ${fmtPct(num(d.breakeven_cvr))}。加价只会放大亏损，不是放量。`
    }
    case '证据不足': {
      const clicks = num(d.clicks)
      const min = num(d.raise_min_clicks)
      const need = num(d.need_for_orders)
      return `只有 ${clicks} 击（加价下限 ${min} 击），要到 ${need} 击才够判。小样本下 p 值会虚低，不能只看 p。`
    }
    case '活动整体无销售额': {
      return `这个活动整体 ROAS ${num(d.campaign_roas)?.toFixed(2)}，花了 ${fmtMoney(num(d.cost))}，一单没有。活动本身的问题，不是某一个词的问题。`
    }
    case '位次加价已为 0，无法再降': {
      return `位次加价已经是 ${num(d.bid_adjustment)?.toFixed(0)}%，降无可降。${d.note || ''}`
    }
    case '活动整体不如全店平均，不该加价': {
      return `活动 ROAS ${num(d.campaign_roas)?.toFixed(2)} 低于全店 ${num(d.account_roas)?.toFixed(2)}。${d.note || ''}`
    }
    case '占活动花费比例过小': {
      return `只占活动花费 ${fmtPct(num(d.share), 1)}（${fmtMoney(num(d.cost))}），动它影响太小。`
    }
    default:
      return d.note || '（这条没写原因细节）'
  }
}

/** 诊断 agent 的输出。
 *
 * 影子模式：它提的问题不进 `pack.questions`、不占审批流，只在这里给人看。
 * 一周后拿 agent_eval 的兑现率决定要不要让它真的挡建议。
 */

/** 归因成熟度：执行 → 归因窗 → 回填缓冲 → 可回看。
 *
 *  后端把归因窗到期日记在 attribution_due_at，但那天报表还在回传（见
 *  attribute.py 的 ATTR_SETTLE_LAG_DAYS=3）。以前界面只写「归因至 X 日」，
 *  人到那天来看还是空的，就以为系统没跑。这里把两段分开说，别让人白等。
 *  改后端那个常量时记得同步这里。
 */
export const ATTR_SETTLE_LAG_DAYS = 3

const DAY = 86_400_000

export function attrProgress(d: Decision, now = Date.now()): string | null {
  if (d.status !== 'executed' || !d.executed_at || !d.attribution_due_at) return null
  const start = new Date(d.executed_at).getTime()
  const due = new Date(d.attribution_due_at).getTime()
  if (!Number.isFinite(start) || !Number.isFinite(due) || due <= start) return null
  const windowDays = Math.max(1, Math.round((due - start) / DAY))
  const settleAt = due + ATTR_SETTLE_LAG_DAYS * DAY
  const dueText = d.attribution_due_at.slice(0, 10)

  if (now >= settleAt) return `窗口已满（${dueText}）· 等引擎回看`
  if (now >= due) {
    const left = Math.max(1, Math.ceil((settleAt - now) / DAY))
    return `归因回填缓冲中（剩 ${left} 天）· ${dueText} 满窗`
  }
  const day = Math.min(windowDays, Math.max(1, Math.floor((now - start) / DAY) + 1))
  return `窗口第 ${day}/${windowDays} 天 · ${dueText} 满窗`
}
