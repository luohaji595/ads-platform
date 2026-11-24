/** 右栏的说明性三张卡：结构冲突（同一个词被几个活动抢）、分层证据（结论稳不稳）、
 *  数据新鲜度。它们不产生动作，但决定「上面那些建议能不能信」。
 */
import type { Freshness, Pack } from '../../lib/api'

export function SideCards({ pack, freshness }: { pack: Pack; freshness: Freshness | null }) {
  return (
  <>
      <div className="card">
        <h2>结构冲突</h2>
        {pack.conflicts.length === 0 && <div className="note">没有同词多活动。</div>}
        {pack.conflicts.map((c) => (
          <div key={c.keyword_text} style={{ marginBottom: 10 }}>
            <div className="small"><b>{c.keyword_text}</b> · {c.campaigns.length} 个活动</div>
            {c.campaigns.map((cp) => (
              <div key={cp.campaign_id} className="small muted" style={{ paddingLeft: 8 }}>
                {cp.campaign_name} bid {cp.bid} / {cp.clicks} 击 / {cp.orders} 单
              </div>
            ))}
          </div>
        ))}
      </div>

      <div className="card tight">
        <h2>分层证据</h2>
        {pack.layer_evidence.map((l) => (
          <div key={l.layer} className="small" style={{ padding: '2px 0' }}>
            <span className={`tag ${l.significant ? 'over' : l.significant === null ? 'low' : 'ok'}`}>
              {l.significant === null ? '算不出' : l.significant ? '有差异' : '检不出差异'}
            </span>{' '}
            {l.label}
            {l.p_value != null && <span className="muted mono"> p={l.p_value}</span>}
            {l.coverage != null && l.coverage < 0.995 && (
              <span className="muted">
                {' '}
                · 覆盖 {l.covered_clicks} 击（{Math.round(l.coverage * 100)}%）
              </span>
            )}
          </div>
        ))}
      </div>

      {freshness && (
        <div className="card tight">
          <h2>数据新鲜度</h2>
          {Object.entries(freshness.tables).map(([k, v]) => (
            <div key={k} className="small" style={{ padding: '2px 0' }}>
              <span className={`tag ${v.stale ? 'over' : 'ok'}`}>{v.stale ? '旧' : '新'}</span>{' '}
              {k} {v.last_date ?? '—'} <span className="muted">({v.age_hours ?? '—'} h)</span>
            </div>
          ))}
        </div>
      )}
  </>
  )
}
