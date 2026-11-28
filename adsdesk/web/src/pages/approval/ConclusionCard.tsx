/** 今日结论：紧凑展示核心计数、策略提示、最贵三笔花费与账户告警。 */
import type { Decision, Pack, WhoManages } from '../../lib/api'
import { fmtMoney } from '../../lib/charts'
import { Rich } from './shared'

export function ConclusionCard({
  pack,
  runId,
  who,
  alerts,
}: {
  pack: Pack
  runId: number | null | undefined
  who: WhoManages[]
  alerts: Decision[]
}) {
  return (
    <div className="card">
      <h2>
        今日结论
        <span className="sub">
          {pack.window_start} ~ {pack.window_end} · run #{runId}
        </span>
      </h2>
      <div className="row" style={{ marginBottom: 8 }}>
        <span className="tag">待批 {pack.front.length} 条</span>
        {(pack.observing?.length ?? 0) > 0 && (
          <span className="tag near">观测 {pack.observing?.length}</span>
        )}
        {(pack.suppressed?.length ?? 0) > 0 && (
          <span className="tag">被拦下 {pack.suppressed?.length}</span>
        )}
        {pack.overflow_count > 0 && <span className="tag">溢出 {pack.overflow_count}</span>}
        {pack.below_count > 0 && <span className="tag">不够线 {pack.below_count}</span>}
        <span className="tag">可省 {fmtMoney(pack.saved)}</span>
        <span className="tag">多花 {fmtMoney(pack.spent)}</span>
        {!pack.target_reachable && <span className="tag over">目标不可达</span>}
      </div>

      {pack.portfolio_headline && (
        <div className="warn">
          <Rich text={pack.portfolio_headline} />
        </div>
      )}

      {who.length > 0 && (
        <>
          <div className="spacer" />
          <div className="sub" style={{ marginBottom: 6 }}>
            今日重点花费（前 {who.length} 笔）
          </div>
          <table className="tbl">
            <tbody>
              {who.map((w) => (
                <tr key={w.campaign_id}>
                  <td title={w.campaign}>{w.campaign.slice(0, 30)}</td>
                  <td className="num">{fmtMoney(w.cost)}</td>
                  <td className="num">{w.orders} 单</td>
                  <td>
                    <span
                      className={`tag ${
                        w.kind === '被锁' || w.kind === '无人管'
                          ? 'over'
                          : w.kind === '有动作'
                            ? 'ok'
                            : ''
                      }`}
                    >
                      {w.how}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      {alerts.length > 0 && (
        <>
          <div className="spacer" />
          <div className="sub" style={{ marginBottom: 6 }}>
            账户级告警
          </div>
          {alerts.map((a) => (
            <div key={`${a.rule_id}-${a.entity_id}`} className="warn" style={{ marginBottom: 8 }}>
              <div className="row" style={{ marginBottom: 4 }}>
                <span className="tag">{a.rule_id}</span>
                <span className="small muted">{a.entity_label}</span>
              </div>
              <Rich text={a.rationale ?? ''} />
            </div>
          ))}
        </>
      )}
    </div>
  )
}
