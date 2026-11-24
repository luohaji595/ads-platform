/** 今日结论：这一屏要回答「今天到底该动什么钱」，所以先给建议条数与拦下原因，
 *  再给最贵的三笔钱各自谁在管 —— 最后那句「今天没动作不等于这些钱没问题」是
 *  这张卡存在的理由，不是装饰。
 */
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
              数据窗口 {pack.window_start} ~ {pack.window_end} · run #{runId}
            </span>
          </h2>
          <div className="row" style={{ marginBottom: 8 }}>
            <span className="tag">建议 {pack.front.length} 条</span>
            {(pack.observing?.length ?? 0) > 0 && (
              <span className="tag near">证据不够、等你定 {pack.observing?.length}</span>
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
            <div className="warn"><Rich text={pack.portfolio_headline} /></div>
          )}
          {who.length > 0 && (
            <>
              <div className="spacer" />
              <div className="sub" style={{ marginBottom: 6 }}>
                今天最贵的三笔钱，各自谁在管
              </div>
              <table className="tbl">
                <tbody>
                  {who.map((w) => (
                    <tr key={w.campaign_id}>
                      <td title={w.campaign}>{w.campaign.slice(0, 30)}</td>
                      <td className="num">{fmtMoney(w.cost)}</td>
                      <td className="num">{w.orders} 单</td>
                      <td>
                        <span className={`tag ${w.kind === '被锁' || w.kind === '无人管' ? 'over' : w.kind === '有动作' ? 'ok' : ''}`}>
                          {w.how}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {who.every((w) => w.kind !== '有动作') && (
                <div className="note">
                  这三笔钱今天一条可执行动作都没有。规则各自都有理由，但叠起来的结果是最大的花费没人动 ——
                  「今天没动作」不等于「这些钱没问题」。
                </div>
              )}
            </>
          )}
          {alerts.length > 0 && (
            <>
              <div className="spacer" />
              <div className="sub" style={{ marginBottom: 6 }}>
                账户级告警 · 只留痕，不可审批
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
          <div className="spacer" />
          <div className="note">
            打钩只记台账，不会替你改广告后台。改完回来点「已改」，归因窗从那一刻开始算。
          </div>
        </div>
  )
}
