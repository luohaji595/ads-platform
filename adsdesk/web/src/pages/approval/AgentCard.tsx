/** 诊断 agent 的影子模式只读输出（收在诊断抽屉中，不带无效复选框）。 */
import { fmtMoney } from '../../lib/charts'
import type { AgentView } from '../../lib/api'

export function AgentCard({ agent }: { agent: AgentView | undefined }) {
  if (!agent) return null

  const sc = agent.score
  const scTouched = sc ? (sc.verifiable ?? sc.judged + sc.unverifiable) : 0
  const scLowVerifiable = scTouched >= 5 && (sc?.verifiable_rate ?? 1) < 0.6

  if (agent.state === 'never_ran') {
    return (
      <div className="card">
        <h2>
          诊断 agent
          <span className="sub">今天尚未运行</span>
        </h2>
        <div className="note">今日诊断 Agent 尚未运行，不影响上方待批决策。</div>
      </div>
    )
  }

  if (agent.state === 'running') {
    return (
      <div className="card">
        <h2>
          诊断 agent
          <span className="sub">运行中</span>
        </h2>
        <div className="note">
          从 {agent.started_at?.slice(11, 19)} 开始运行，完成后会自动更新，不影响待批决策。
        </div>
      </div>
    )
  }

  if (agent.state === 'failed') {
    return (
      <div className="card">
        <h2>
          诊断 agent
          <span className="sub">运行失败</span>
        </h2>
        <div className="err">{agent.error || '未返回错误信息'}</div>
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

      {agent.fallback && (
        <div className="note">
          <b>这条评的是 run #{agent.based_on_run} 的输入</b>，不是最新一版。
        </div>
      )}

      {agent.score && (
        <div className="small muted" style={{ marginTop: 6 }}>
          {agent.score.judged === 0 ? (
            <>
              历史断言待到判定日：{agent.score.pending} 条待验
              {agent.score.unverifiable > 0 ? `，${agent.score.unverifiable} 条无法判定` : ''}。
              {scLowVerifiable && ' 部分到期断言无法验证，暂不以命中率作依据。'}
            </>
          ) : (
            <>
              已判 {agent.score.judged} 条断言：
              <b>
                对 {agent.score.correct} / 错 {agent.score.wrong}
              </b>
              （{(agent.score.rate! * 100).toFixed(0)}%）
              {agent.score.pending > 0 ? `；另有 ${agent.score.pending} 条待验` : ''}
              {agent.score.unverifiable > 0 ? `、${agent.score.unverifiable} 条判不了` : ''}。
            </>
          )}
        </div>
      )}

      {obs.length > 0 && (
        <div style={{ marginTop: 10 }}>
          <div className="small" style={{ marginBottom: 4 }}>
            <b>反对</b>
          </div>
          {obs.map((o) => (
            <div key={o.objection_id} className="warn" style={{ marginBottom: 6 }}>
              <span className={`tag ${o.verdict === 'oppose' ? 'over' : 'medium'}`}>
                #{o.decision_id} {o.verdict === 'oppose' ? '反对' : '存疑'}
              </span>{' '}
              <span className="small">{o.why}</span>
            </div>
          ))}
        </div>
      )}

      {qs.length > 0 && (
        <div style={{ marginTop: 10 }}>
          <div className="small" style={{ marginBottom: 4 }}>
            <b>疑问（只读参考）</b>
          </div>
          {qs.map((q) => (
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
              <div className="small" style={{ margin: '6px 0' }}>
                {q.ask}
              </div>
              {(q.options || []).length > 0 && (
                <div className="small muted">
                  {(q.options || []).map((opt, i) => (
                    <div key={i} style={{ padding: '1px 0' }}>
                      · {opt}
                    </div>
                  ))}
                </div>
              )}
            </div>
          ))}
        </div>
      )}

      {evals.length > 0 && (
        <details style={{ marginTop: 10 }}>
          <summary className="small muted" style={{ cursor: 'pointer' }}>
            {evals.length} 条待验证断言
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
