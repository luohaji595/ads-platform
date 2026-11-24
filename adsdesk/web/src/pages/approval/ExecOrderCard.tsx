/** 执行单：把批过的动作压成一张「照着抄」的清单。
 *
 *  两道门：①「已改」只在决策真的是 approved 时出现 —— 后端只有 approved/proposed
 *  能标已改，其他状态点了就是 409，而人看到的是一个点了没反应的按钮；
 *  ② 清单能一键复制成纯文本，改后台时切窗口照着贴，不用来回对屏幕。
 */
import { useState } from 'react'
import type { Decision } from '../../lib/api'
import { showValue } from './shared'

export function ExecOrderCard({
  approved,
  order,
  decisions,
  busy,
  msg,
  onMakeOrder,
  onExecuted,
}: {
  approved: Decision[]
  order: any
  decisions: Decision[]
  busy: boolean
  msg: string | null
  onMakeOrder: (all: Decision[]) => void
  onExecuted: (d: Decision, target?: string | number | null) => void
}) {
  const [copied, setCopied] = useState(false)
  // 单条复制：同一时刻只有一条显示「已复制」，用 item_id 记住是谁。
  const [oneCopied, setOneCopied] = useState<number | null>(null)

  const plain = approved
    .map(
      (d, i) =>
        `${i + 1}. ${d.entity_label}：${showValue(d.current_value)} → ${showValue(d.proposed_value)}`,
    )
    .join('\n')

  async function copy() {
    try {
      await navigator.clipboard.writeText(plain)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1600)
    } catch {
      // 剪贴板被浏览器拦（非 https / 未聚焦）时不要假装成功
      setCopied(false)
    }
  }

  async function copyOne(it: any) {
    const line = `${it.campaign_name || it.entity_label}：${it.current_value ?? '—'} → ${it.target_value ?? '—'}`
    try {
      await navigator.clipboard.writeText(line)
      setOneCopied(it.item_id)
      window.setTimeout(() => setOneCopied(null), 1600)
    } catch {
      setOneCopied(null)
    }
  }

  return (
  <div className="card">
    <h2>
      执行单
      <span className="sub">改后台时照着抄</span>
    </h2>
    {approved.length === 0 ? (
      <div className="note">还没有批的动作。</div>
    ) : (
      <>
        <div className="small muted" style={{ marginBottom: 6 }}>{approved.length} 条</div>
        {approved.map((d) => (
          <div key={d.decision_id} className="small" style={{ padding: '3px 0' }}>
            {d.entity_label}：{showValue(d.current_value)} → <b>{showValue(d.proposed_value)}</b>
          </div>
        ))}
        <div className="spacer" />
        <div className="row">
          <button className="btn primary" disabled={busy} onClick={() => onMakeOrder(approved)}>
            生成执行单
          </button>
          <button className={`btn copy-btn ${copied ? 'done' : ''}`} onClick={copy} title="复制成纯文本">
            {copied ? '已复制' : '复制清单'}
          </button>
        </div>
        {order && (
          <div style={{ marginTop: 10 }}>
            <div className="small muted">执行单 {order.order?.ticket_id}</div>
            {(() => {
              // 进度条：一张单子改到一半时，最想知道的是「还剩几条」。
              // 数字从 items 现算，不另存 state —— 否则点了「已改」条不动，看着像没生效。
              const items: any[] = order.items || []
              const done = items.filter((it) => it.executed_at).length
              const pct = items.length ? Math.round((done / items.length) * 100) : 0
              return (
                <div style={{ margin: '6px 0 2px' }}>
                  <div className="progress">
                    <div className="progress-bar" style={{ width: `${pct}%` }} />
                  </div>
                  <div className="small muted">
                    已改 {done}/{items.length}
                    {done === items.length && items.length > 0 ? ' · 这张单做完了' : ''}
                  </div>
                </div>
              )
            })()}
            {(order.items || []).map((it: any) => {
              const dec = decisions.find((x) => x.decision_id === it.decision_id)
              const canMark = dec?.status === 'approved'
              return (
                <div
                  key={it.item_id}
                  className="small"
                  style={{ padding: '4px 0', borderTop: '1px solid var(--line)' }}
                >
                  <div className="row" style={{ gap: 6 }}>
                    <span style={{ flex: 1 }}>
                      {it.campaign_name || it.entity_label}：{it.current_value ?? '—'} →{' '}
                      <b>{it.target_value ?? '—'}</b>
                    </span>
                    <button
                      className="btn copy-btn"
                      title="只复制这一条（后台按活动名搜索时用得上）"
                      onClick={() => copyOne(it)}
                    >
                      {oneCopied === it.item_id ? '已复制' : '复制'}
                    </button>
                  </div>
                  <div className="row" style={{ marginTop: 4 }}>
                    {it.executed_at ? (
                      <span className="tag ok">已改 {it.executed_at.slice(0, 10)}</span>
                    ) : canMark ? (
                      <button
                        className="btn primary"
                        disabled={busy}
                        onClick={() => onExecuted(dec!, it.target_value)}
                      >
                        已改
                      </button>
                    ) : (
                      <span className="small muted">
                        {dec
                          ? `当前状态「${dec.status ?? 'proposed'}」，这条要先批才能标已改`
                          : '这条决策已不在账本里'}
                      </span>
                    )}
                  </div>
                </div>
              )
            })}
          </div>
        )}
      </>
    )}
    {msg && <div className="note" style={{ marginTop: 8 }}>{msg}</div>}
  </div>
  )
}
