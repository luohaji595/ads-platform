/** 观察区：引擎算出了动作但证据不够，收在诊断抽屉中，支持全宽行内强制批准。 */
import { Fragment, useState } from 'react'
import type { Decision } from '../../lib/api'
import { fmtMoney } from '../../lib/charts'
import { CONF_TEXT, attrProgress, gateText, showValue } from './shared'

export function ObservingCard({
  observing,
  notes,
  busy,
  onOverride,
  onExecuted,
}: {
  observing: Decision[]
  notes: string[]
  busy: boolean
  onOverride: (d: Decision, reason: string) => void
  onExecuted: (d: Decision, target?: string | number | null) => void
}) {
  const [openId, setOpenId] = useState<number | null>(null)
  const [draft, setDraft] = useState('')
  if (observing.length === 0) return null

  const isHeld = (d: Decision) =>
    d.held === true || (d.rationale || '').includes('已批先观察')
  const held = observing.filter(isHeld)
  const weak = observing.filter((d) => !isHeld(d))

  return (
    <div className="card">
      <h2>
        引擎算出了动作，但证据不够
        <span className="sub">
          共 {observing.length} 条
          {held.length > 0 && ` · ${held.length} 条观察期按住`}
          {weak.length > 0 && ` · ${weak.length} 条证据不足`}
        </span>
      </h2>

      {held.length > 0 && (
        <>
          <div className="small" style={{ marginBottom: 4 }}>
            <b>观察期按住</b>
          </div>
          <table className="tbl">
            <tbody>
              {held.map((d) => (
                <tr key={`lk-${d.rule_id}-${d.entity_id}`}>
                  <td title={d.entity_label}>{d.entity_label.slice(0, 26)}</td>
                  <td>
                    <span className="tag">{d.rule_id}</span>
                  </td>
                  <td className="num">
                    {showValue(d.current_value)} → {showValue(d.proposed_value)}
                  </td>
                  <td className="num">{fmtMoney(d.spend_impact)}</td>
                  <td className="small muted">
                    {d.gate_reason ||
                      (d.rationale || '').split('；').filter((s) => s.includes('先观察')).join('；') ||
                      '观察期内暂不进待批'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      {weak.length > 0 && (
        <>
          {held.length > 0 && <div className="spacer" />}
          <div className="small" style={{ marginBottom: 4 }}>
            <b>证据不足（需要时可强制批准）</b>
          </div>
          <table className="tbl">
            <tbody>
              {weak.map((d) => {
                const id = d.decision_id
                const st = d.status || 'observing'
                const opened = id != null && openId === id
                return (
                  <Fragment key={`wk-${d.rule_id}-${d.entity_id}`}>
                    <tr>
                      <td title={d.entity_label}>{d.entity_label.slice(0, 26)}</td>
                      <td>
                        <span className="tag">{d.rule_id}</span>
                      </td>
                      <td className="num">
                        {showValue(d.current_value)} → {showValue(d.proposed_value)}
                      </td>
                      <td className="num">{fmtMoney(d.spend_impact)}</td>
                      <td>
                        <span className={`tag ${d.confidence}`}>
                          {CONF_TEXT[d.confidence] || d.confidence}
                        </span>
                      </td>
                      <td className="small muted" style={{ maxWidth: 260 }}>
                        <div>{gateText(d)}</div>
                        {d.object_record && <div style={{ marginTop: 4 }}>· {d.object_record}</div>}
                        {d.rule_record && (
                          <div style={{ marginTop: 4, fontSize: 11, lineHeight: 1.5, opacity: 0.8 }}>
                            · {d.rule_record}
                          </div>
                        )}
                      </td>
                      <td style={{ whiteSpace: 'nowrap' }}>
                        {st === 'executed' && (
                          <span className="tag ok" title="窗口满了还要等报表回传归因">
                            已改 · {attrProgress(d) ?? '归因窗已开'}
                          </span>
                        )}
                        {st === 'approved' && (
                          <div className="row">
                            <span className="tag ok">已强制批准</span>
                            <button className="btn primary" disabled={busy} onClick={() => onExecuted(d)}>
                              已改
                            </button>
                          </div>
                        )}
                        {st !== 'approved' && st !== 'executed' && !opened && (
                          <button
                            className="btn"
                            disabled={busy || id == null}
                            title={id == null ? '这条不在账本里，不能批' : '证据不够，但你可以拍板'}
                            onClick={() => {
                              if (id == null) return
                              setOpenId(id)
                              setDraft('')
                            }}
                          >
                            强制批准
                          </button>
                        )}
                      </td>
                    </tr>
                    {st === 'approved' && d.override_reason && (
                      <tr className="override-row">
                        <td colSpan={7} className="small muted">
                          强制批准理由：{d.override_reason}
                        </td>
                      </tr>
                    )}
                    {opened && st !== 'approved' && st !== 'executed' && (
                      <tr className="override-row">
                        <td colSpan={7}>
                          <div className="inline-override">
                            <div className="row">
                              <input
                                type="text"
                                placeholder="填写为什么现在就动（至少 8 个字）"
                                value={draft}
                                onChange={(e) => setDraft(e.target.value)}
                                style={{ flex: 1 }}
                              />
                              <button
                                className="btn primary"
                                disabled={busy || draft.trim().length < 8}
                                onClick={() => {
                                  onOverride(d, draft.trim())
                                  setOpenId(null)
                                }}
                              >
                                确认强制批准
                              </button>
                              <button className="btn" disabled={busy} onClick={() => setOpenId(null)}>
                                取消
                              </button>
                            </div>
                            <div className="small muted">
                              引擎的「证据不够」标记会保留，同时记录你的拍板理由。
                            </div>
                          </div>
                        </td>
                      </tr>
                    )}
                  </Fragment>
                )
              })}
            </tbody>
          </table>
        </>
      )}

      {notes.length > 0 && (
        <div className="small muted" style={{ marginTop: 8 }}>
          {notes.map((n, i) => (
            <div key={`note-${i}`}>· {n}</div>
          ))}
        </div>
      )}
    </div>
  )
}
