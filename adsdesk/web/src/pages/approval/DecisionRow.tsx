/** 一条待批动作。三颗按钮的顺序是「批 → 不批 → 提交」，因为最常见的动作是批；
 *  「已改」只在 approved 之后才出现 —— 标已改会开始算归因窗，跳过审批直接标
 *  等于把「没批的动作」混进「改完的效果」里。
 */
import type { Decision } from '../../lib/api'
import { fmtMoney } from '../../lib/charts'
import { Rich, attrProgress, evidenceLabel, showValue, type Mark } from './shared'

export function DecisionRow({
  d,
  mark,
  reason,
  busy,
  onMark,
  onReason,
  onSubmit,
  onExecuted,
}: {
  d: Decision
  mark?: Mark[number]
  reason: string
  busy: boolean
  onMark: (id: number, verdict: Mark[number]) => void
  onReason: (id: number, text: string) => void
  onSubmit: (d: Decision) => void
  onExecuted: (d: Decision) => void
}) {
  const id = d.decision_id
  return (
    <div style={{ borderTop: '1px solid var(--line)', paddingTop: 10, marginTop: 10 }}>
      <div className="row">
        <b>{d.entity_label}</b>
        <span className="tag">{d.rule_id}</span>
        <span className="tag">{d.action_label || d.action_type}</span>
        <span className={`tag ${d.confidence}`}>{evidenceLabel(d)}</span>
        {d.spend_impact != null && (
          <span className="small muted mono">影响 {fmtMoney(d.spend_impact)}</span>
        )}
        {d.status && d.status !== 'proposed' && <span className="tag ok">{d.status}</span>}
      </div>
      <div className="small" style={{ margin: '6px 0' }}>
        {showValue(d.current_value)} → <b>{showValue(d.proposed_value)}</b>
      </div>
      <div className="small muted">
        <Rich text={d.rationale ?? ''} />
      </div>
      {(d.rule_record || d.object_record) && (
        <div className="small muted" style={{ marginTop: 4 }}>
          {d.rule_record && <div>· {d.rule_record}</div>}
          {d.object_record && <div>· {d.object_record}</div>}
        </div>
      )}
      {d.objections && d.objections.length > 0 && (
        <div className="warn" style={{ marginTop: 6 }}>
          agent 反对：{d.objections.map((o) => o.why).join('；')}
        </div>
      )}
      <div className="row" style={{ marginTop: 8 }}>
        <button
          className={`btn ${mark === 'approve' ? 'primary' : ''}`}
          disabled={busy}
          onClick={() => id != null && onMark(id, 'approve')}
        >
          批
        </button>
        <button
          className={`btn ${mark === 'reject' ? 'danger' : ''}`}
          disabled={busy}
          onClick={() => id != null && onMark(id, 'reject')}
        >
          不批
        </button>
        <button className="btn" disabled={busy || !mark} onClick={() => onSubmit(d)}>
          提交
        </button>
        {d.status === 'approved' && (
          <button className="btn primary" disabled={busy} onClick={() => onExecuted(d)}>
            已改
          </button>
        )}
        {d.status === 'executed' && (
          <span className="tag ok" title="窗口满了还要等报表回传归因，那天才是真的能看结果">
            已改 · {attrProgress(d) ?? '归因窗已开'}
          </span>
        )}
        {mark === 'reject' && (
          <input
            type="text"
            placeholder="为什么不批（可选，会进 reason_hit）"
            value={id != null ? reason : ''}
            onChange={(e) => id != null && onReason(id, e.target.value)}
            style={{ flex: 1, minWidth: 200 }}
          />
        )}
      </div>
    </div>
  )
}
