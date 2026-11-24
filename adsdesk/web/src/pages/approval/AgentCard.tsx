/** 诊断 agent 的输出（影子模式：不进审批流，只给人看）。
 *  一周后拿 agent_eval 的兑现率决定要不要让它真的挡建议。
 */
import { fmtMoney } from '../../lib/charts'
import type { AgentView } from '../../lib/api'
import { Rich } from './shared'

export function AgentCard({
  agent,
  answers,
  onToggle,
}: {
  agent: AgentView | undefined
  answers: Record<string, number[]>
  onToggle: (qid: string, i: number) => void
}) {
  if (!agent) return null

  // 可验证率：到期了却一条都判不出来，说明断言写成了没法验的形式 ——
  // 那和「答错」是两件事，但它同样说明现在还不能拿它的话当依据。
  // 分母 < 5 时不算，1 条判不了就喊「可验证率 0%」是在拿噪声吓人。
  const sc = agent.score
  const scTouched = sc ? (sc.verifiable ?? sc.judged + sc.unverifiable) : 0
  const scLowVerifiable = scTouched >= 5 && (sc?.verifiable_rate ?? 1) < 0.6

  if (agent.state === 'never_ran') {
    return (
      <div className="card">
        <h2>
          诊断 agent
          <span className="sub">今天没跑</span>
        </h2>
        <div className="note">
          跑一次 `python3 adsdesk/agents/diagnose.py` 就会有。它不影响上面的奏折。
        </div>
      </div>
    )
  }

  if (agent.state === 'running') {
    return (
      <div className="card">
        <h2>
          诊断 agent
          <span className="sub">正在跑</span>
        </h2>
        <div className="note">
          从 {agent.started_at?.slice(11, 19)} 开始，还没返回。正常一次 2–4 分钟；
          超过 30 分钟会被判为进程已死。规则引擎的奏折不受影响。
        </div>
      </div>
    )
  }

  if (agent.state === 'failed') {
    return (
      <div className="card">
        <h2>
          诊断 agent
          <span className="sub">跑失败了</span>
        </h2>
        <div className="err">{agent.error || '没拿到错误信息'}</div>
        <div className="note" style={{ marginTop: 6 }}>
          规则引擎的奏折照常，agent 只是附加价值。上面那些建议不受影响。
        </div>
      </div>
    )
  }

  const qs = agent.questions || []
  const obs = agent.objections || []
  const evals = agent.evals || []

  return (
  <div className="card">
    <h2>
      诊断 agent
      <span className="sub">
        {agent.model} · {qs.length} 个疑问 · {obs.length} 条反对 · {evals.length} 条待验证
        {agent.score && agent.score.judged > 0 && (
          <> · 兑现 {agent.score.correct}/{agent.score.judged}</>
        )}
      </span>
    </h2>
    <div className="note">
      影子模式：它提的问题不进上面的待批，也不占审批流。只在这里给你看。
      {agent.fallback && (
        <>
          {' '}
          <b>
            这条评的是 run #{agent.based_on_run} 的输入
          </b>
          ，不是现在这一版 —— 引擎今天重跑过，agent 还没在最新那版上跑。
        </>
      )}
    </div>

    {agent.score && (
      <div className="small muted" style={{ marginTop: 6 }}>
        {agent.score.judged === 0 ? (
          <>
            它过去的断言还没有一条到判定日：{agent.score.pending} 条在等
            {agent.score.unverifiable > 0 ? `，${agent.score.unverifiable} 条到期却判不了` : ''}
            。
            {scLowVerifiable
              ? `到期的那 ${agent.score.unverifiable} 条一条都没判出来 —— 不是它答错，是断言写成了没法验的形式。先让它把断言改成可验证的，再谈准不准。`
              : '现在还不能说它准不准。'}
          </>
        ) : (
          <>
            它过去的 {agent.score.judged} 条断言已判：{` `}
            <b>对 {agent.score.correct} / 错 {agent.score.wrong}</b>
            （{(agent.score.rate! * 100).toFixed(0)}% 兑现）
            {agent.score.pending > 0 ? `；另有 ${agent.score.pending} 条还在等判定日` : ''}
            {agent.score.unverifiable > 0 ? `、${agent.score.unverifiable} 条判不了` : ''}
            。样本 {agent.score.judged} 条，先别当成定论。
            {scLowVerifiable &&
              `另 ${agent.score.unverifiable} 条到期却判不了（可验证率 ${Math.round((agent.score.verifiable_rate ?? 0) * 100)}%）—— 断言写太大，先改断言。`}
          </>
        )}
      </div>
    )}

    {obs.length > 0 && (
      <div style={{ marginTop: 10 }}>
        <div className="small" style={{ marginBottom: 4 }}>
          <b>反对</b>（对已经存在的建议说的，不是新建议）
        </div>
        {obs.map((o) => (
          <div key={o.objection_id} className="warn" style={{ marginBottom: 6 }}>
            <span className={`tag ${o.verdict === 'oppose' ? 'over' : 'medium'}`}>
              #{o.decision_id} {o.verdict === 'oppose' ? '反对' : '存疑'}
            </span>
            <span className="small">{o.why}</span>
          </div>
        ))}
      </div>
    )}

    {qs.length > 0 && (
      <div style={{ marginTop: 10 }}>
        <div className="small" style={{ marginBottom: 4 }}>
          <b>疑问</b>
        </div>
        {qs.map((q) => {
          const sel = answers[q.id] || []
          return (
            <div
              key={q.id}
              style={{ borderTop: '1px solid var(--line)', paddingTop: 8, marginTop: 8 }}
            >
              <div className="row">
                <span className="tag">{q.id}</span>
                <b className="small">{q.object}</b>
                {q.impact != null && (
                  <span className="small muted mono">影响 {fmtMoney(q.impact)}</span>
                )}
                {q.uncertainty != null && (
                  <span className="small muted mono">不确定 {q.uncertainty}</span>
                )}
              </div>
              <div className="small" style={{ margin: '6px 0' }}>{q.ask}</div>
              {(q.options || []).map((opt, i) => (
                <label
                  key={i}
                  className="small"
                  style={{ display: 'block', padding: '2px 0' }}
                >
                  <input
                    type="checkbox"
                    checked={sel.includes(i)}
                    onChange={() => onToggle(q.id, i)}
                  />{' '}
                  {opt}
                </label>
              ))}
            </div>
          )
        })}
      </div>
    )}

    {evals.length > 0 && (
      <details style={{ marginTop: 10 }}>
        <summary className="small muted">
          它的 {evals.length} 条断言，到期会自动判
          {evals.some((e) => e.verdict === 'unverifiable')
            ? '；判不了的会写清楚为什么判不了（那是断言写法的问题，不是它答错）'
            : '对错'}
        </summary>
        {evals.map((e) => (
          <div key={e.eval_id} className="small" style={{ padding: '3px 0' }}>
            <span className="tag">{e.verdict}</span> {e.claim}
            <span className="muted"> · 到期 {e.verifiable_at.slice(0, 10)}</span>
          </div>
        ))}
      </details>
    )}
  </div>
  )
}
