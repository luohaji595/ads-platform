/** 待答问题：勾选沉淀规则，或「划掉」90 天不再问，或直接跳过。 */
import type { Question } from '../../lib/api'
import { DEST_LABEL, qkeyOf } from './shared'

export function QuestionsCard({
  questions,
  answers,
  crossed,
  freeText,
  setAnswers,
  setCrossed,
  setFreeText,
  busy,
  onSubmit,
}: {
  questions: Question[]
  answers: Record<string, number[]>
  crossed: Record<string, number[]>
  freeText: Record<string, string>
  setAnswers: (v: Record<string, number[]>) => void
  setCrossed: (v: Record<string, number[]>) => void
  setFreeText: (v: Record<string, string>) => void
  busy: boolean
  onSubmit: (q: Question, skipped?: boolean) => void
}) {
  return (
    <div className="card">
      <h2>
        需要你判断的问题
        <span className="sub">打钩即可，不需写长文</span>
      </h2>
      {questions.length === 0 && <div className="note">今天没有需要判断的问题。</div>}
      {questions.map((q) => {
        const k = qkeyOf(q)
        const sel = answers[k] || []
        const cross = crossed[k] || []
        const metas = q.option_meta || []
        return (
          <div key={k} style={{ borderTop: '1px solid var(--line)', paddingTop: 10, marginTop: 10 }}>
            <div className="row">
              <span className="tag">{q.id}</span>
              <b>{q.object}</b>
            </div>
            <div className="small" style={{ margin: '6px 0' }}>
              {q.ask}
            </div>
            <div>
              {q.options.map((opt, i) => {
                const m = metas[i] || null
                const dest = m?.destination
                const done = Boolean(m?.settled_table)
                const isCrossed = cross.includes(i)
                const dead = Boolean(m?.excluded) || isCrossed
                const tag = done
                  ? `已在生效（到 ${m?.settled_until || '未设'}）`
                  : dead
                    ? `已划掉（到 ${m?.exclude_until || '90 天后'}）`
                    : dest
                      ? DEST_LABEL[dest] || dest
                      : ''
                return (
                  <div key={i} className="row" style={{ padding: '2px 0', opacity: done ? 0.55 : 1 }}>
                    <label className="small" style={{ flex: 1 }}>
                      <input
                        type="checkbox"
                        checked={sel.includes(i)}
                        onChange={() => {
                          const next = sel.includes(i) ? sel.filter((x) => x !== i) : [...sel, i]
                          setAnswers({ ...answers, [k]: next })
                          if (!sel.includes(i)) {
                            const c = (crossed[k] || []).filter((x) => x !== i)
                            setCrossed({ ...crossed, [k]: c })
                          }
                        }}
                      />{' '}
                      {opt}
                      {tag && <span className="small muted"> · {tag}</span>}
                    </label>
                    <button
                      type="button"
                      className={`cross-btn ${isCrossed ? 'active' : ''}`}
                      title="这个方向以后别再问（90 天内）"
                      onClick={() => {
                        const c = isCrossed ? cross.filter((x) => x !== i) : [...cross, i]
                        setCrossed({ ...crossed, [k]: c })
                        setAnswers({ ...answers, [k]: sel.filter((x) => x !== i) })
                      }}
                    >
                      {isCrossed ? '撤销划掉' : '划掉'}
                    </button>
                  </div>
                )
              })}
            </div>
            <div className="row" style={{ marginTop: 6 }}>
              <input
                type="text"
                placeholder="补充依据（可选）"
                value={freeText[k] || ''}
                onChange={(e) => setFreeText({ ...freeText, [k]: e.target.value })}
                style={{ flex: 1 }}
              />
              <button className="btn" disabled={busy} onClick={() => onSubmit(q, true)}>
                跳过
              </button>
              <button className="btn primary" disabled={busy} onClick={() => onSubmit(q)}>
                提交
              </button>
            </div>
          </div>
        )
      })}
    </div>
  )
}
