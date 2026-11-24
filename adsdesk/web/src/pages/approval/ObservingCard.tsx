/** 观察区：引擎算出了动作，但证据还没到判定线 —— 这些是「等你拍板的东西」，
 *  不是「被否掉的东西」。强制批准会留下 override 三件套，和「等证据」分开记账。
 */
import { useState } from 'react'
import type { Decision } from '../../lib/api'
import { fmtMoney, fmtPct } from '../../lib/charts'
import { ACTION_TEXT, CONF_TEXT, Rich, attrProgress, gateText, showValue } from './shared'

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
  // hooks 必须在任何 early return 之前
  const [openId, setOpenId] = useState<number | null>(null)
  const [draft, setDraft] = useState('')
  if (observing.length === 0) return null

  // 「你按住」（观察期内，本轮不落库、不可操作）与「数学按住」（证据不够，可强制批准）
  // 是两件事：前者是你已经拍过板了，后者是引擎在等样本。混在一起会让人以为
  // 观察期内的东西还能再批一次。
  const isHeld = (d: Decision) =>
    d.held === true || (d.rationale || '').includes('已批先观察')
  const held = observing.filter(isHeld)
  const weak = observing.filter((d) => !isHeld(d))

  return (
  <div className="card">
    <h2>
      引擎算出了动作，但证据不够
      <span className="sub">
        要不要照样做，由你定 · {observing.length} 条
        {held.length > 0 && ` · ${held.length} 条是你在观察期`}
        {weak.length > 0 && ` · ${weak.length} 条是数学按住`}
      </span>
    </h2>
    <div className="note">
      这些不是「没发现问题」，是引擎已经算出了目标价、只是样本还没到判定线 ——
      每条都写着「差多少证据」（现在多少击、需要多少击）。
      等样本攒够它自己会升进待批；你判断等不起，也可以现在拍板：
      强制批准理由必填，账本里会分清「等到证据」和「人拍的板」。
    </div>

    {held.length > 0 && (
      <>
        <div className="spacer" />
        <div className="small" style={{ marginBottom: 4 }}>
          <b>你按住</b>（批过「先观察」，观察期内不再进待批）
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
                    '你在观察期内批过「先观察」，本轮不再作为待批'}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </>
    )}

    {weak.length > 0 && (
      <>
        <div className="spacer" />
        <div className="small" style={{ marginBottom: 4 }}>
          <b>数学按住</b>（置信度不够，不是没人管）
        </div>
        <table className="tbl">
          <tbody>
            {weak.map((d) => {
              const id = d.decision_id
              const st = d.status || 'observing'
              const opened = id != null && openId === id
              return (
                <tr key={`wk-${d.rule_id}-${d.entity_id}`}>
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
                  <td style={{ minWidth: 220 }}>
                    {st === 'executed' && (
                      <span className="tag ok" title="窗口满了还要等报表回传归因，那天才是真的能看结果">
                        已改 · {attrProgress(d) ?? '归因窗已开'}
                      </span>
                    )}
                    {st === 'approved' && (
                      <>
                        <span className="tag ok">已强制批准</span>{' '}
                        <button className="btn primary" disabled={busy} onClick={() => onExecuted(d)}>
                          已改
                        </button>
                        {d.override_reason && (
                          <div className="small muted" style={{ marginTop: 4 }}>
                            你当时的理由：{d.override_reason}
                          </div>
                        )}
                      </>
                    )}
                    {st !== 'approved' && st !== 'executed' && (
                      <>
                        {!opened && (
                          <button
                            className="btn"
                            disabled={busy || id == null}
                            title={id == null ? '这条不在账本里（上一轮的快照），不能批' : '证据不够，但你可以拍板'}
                            onClick={() => {
                              if (id == null) return
                              setOpenId(id)
                              setDraft('')
                            }}
                          >
                            强制批准
                          </button>
                        )}
                        {opened && (
                          <div style={{ marginTop: 4 }}>
                            <input
                              type="text"
                              placeholder="为什么现在就动（至少 8 字，会进 reason_hit）"
                              value={draft}
                              onChange={(e) => setDraft(e.target.value)}
                              style={{ width: '100%' }}
                            />
                            <div className="row" style={{ marginTop: 4 }}>
                              <button
                                className="btn primary"
                                disabled={busy || draft.trim().length < 8}
                                onClick={() => onOverride(d, draft.trim())}
                              >
                                确认强制批准
                              </button>
                              <button className="btn" disabled={busy} onClick={() => setOpenId(null)}>
                                取消
                              </button>
                            </div>
                            <div className="small muted" style={{ marginTop: 2 }}>
                              引擎的判断不会改：这条仍然记着「证据不够」，只是加了「你拍的板」。
                            </div>
                          </div>
                        )}
                      </>
                    )}
                  </td>
                </tr>
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

/** 被规则自己的保护闸门掐掉的动作 —— 连建议都不算。
 *
 * 这张卡是「今天什么都没有」的直接解药：这一天正文 0 条，可 suppressed 有 19 条，
 * 每条都写着为什么不算。以前这些只写进 md 底稿，网页上一片空白，
 * 人就以为引擎今天什么都没干。
 */
