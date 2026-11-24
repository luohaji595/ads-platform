/** 每天几个「打钩就行」的问题。答案会沉淀成规则参数/排除项，
 *  所以勾选与划掉必须分开（一个表示要，一个表示以后别再问）。
 */
import type { Question } from '../../lib/api'
import { DEST_LABEL } from './shared'

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
            <span className="sub">打钩就行，不问开放题</span>
          </h2>
          {questions.length === 0 && <div className="note">今天没有问题。</div>}
          {questions.map((q) => {
            const sel = answers[q.id] || []
            const cross = crossed[q.id] || []
            const metas = q.option_meta || []
            return (
              <div key={q.id} style={{ borderTop: '1px solid var(--line)', paddingTop: 10, marginTop: 10 }}>
                <div className="row">
                  <span className="tag">{q.id}</span>
                  <b>{q.object}</b>
                </div>
                <div className="small" style={{ margin: '6px 0' }}>{q.ask}</div>
                <div>
                  {q.options.map((opt, i) => {
                    const m = metas[i] || null
                    const dest = m?.destination
                    const done = Boolean(m?.settled_table)
                    const dead = Boolean(m?.excluded) || cross.includes(i)
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
                              // 勾了就不能同时算划掉：两种表态互相矛盾
                              setAnswers({ ...answers, [q.id]: next })
                              if (!sel.includes(i)) {
                                const c = (crossed[q.id] || []).filter((x) => x !== i)
                                setCrossed({ ...crossed, [q.id]: c })
                              }
                            }}
                          />{' '}
                          {opt}
                          {tag && <span className="small muted"> · {tag}</span>}
                        </label>
                        <button
                          className="small"
                          style={{ background: 'none', border: 'none', cursor: 'pointer', opacity: 0.7 }}
                          title="这个方向以后别再问（90 天内）"
                          onClick={() => {
                            const c = cross.includes(i) ? cross.filter((x) => x !== i) : [...cross, i]
                            setCrossed({ ...crossed, [q.id]: c })
                            // 划掉的同时取消勾选，避免「又要又不要」
                            setAnswers({ ...answers, [q.id]: sel.filter((x) => x !== i) })
                          }}
                        >
                          {cross.includes(i) ? '撤销划掉' : '划掉'}
                        </button>
                      </div>
                    )
                  })}
                </div>
                <div className="row" style={{ marginTop: 6 }}>
                  <input
                    type="text"
                    placeholder="补一句依据（可选，写数字会被读成参数）"
                    value={freeText[q.id] || ''}
                    onChange={(e) => setFreeText({ ...freeText, [q.id]: e.target.value })}
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
