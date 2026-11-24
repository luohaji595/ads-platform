import { useEffect, useState } from 'react'
import {
  get,
  type CockpitSummary,
  type CampaignInConflict,
  type MarketMove,
  type MarketAbaRow,
  type MarketDailyRow,
} from '../lib/api'
import { BarRow, IntervalBar, Line, fmtInt, fmtMoney, fmtPct } from '../lib/charts'
import RankPanel, { type RankPanelData } from './RankPanel'

const W = 720

/**
 * 末几天不进判断：报表还在回传归因。
 * 后端 cockpit.py 的 _days() 已经把最后 2 天排除在窗口外，attribute.py 的
 * ATTR_SETTLE_LAG_DAYS=3 又是「窗末之后还要等 3 天回传」。两个数不是一回事，
 * 但前端只需要说清一件事：这几天是半截数据，别当结论看。改后端常量时记得同步这里。
 */
const IMMATURE_DAYS = 2

// 并排的两张小图用更窄的 viewBox：等比缩放时 4:1 的比例铺到半屏宽才不会压成一条线。
// 和 W 一样，这里的数只影响画法，不影响数据。
const MINI_W = 360
const MINI_H = 84

function isErr(x: any): x is { error: string } {
  return x && typeof x.error === 'string'
}

/** 变化值按字段选格式：金额字段带 $，星级 1 位小数，其余按整数（名次/评论数/变体数）。 */
function fmtMove(field: string, v: string | number | null | undefined): string {
  if (v == null || v === '') return '—'
  const n = typeof v === 'number' ? v : Number(v)
  if (!Number.isFinite(n)) return String(v)
  if (field === 'price' || field === 'list_price' || field === 'coupon') return `$${n.toFixed(2)}`
  if (field === 'star_rating') return n.toFixed(1)
  return n.toLocaleString('en-US')
}

/** 差值。名次差正数是名次上升 —— 后端算的是「上周名次 − 本周名次」。 */
function fmtDelta(field: string, d: number | null, moveType: string): string {
  if (d == null) return '—'
  const sign = d > 0 ? '+' : ''
  if (moveType === 'rank' || field === 'rank_gain') return `${sign}${d} 名`
  if (field === 'price' || field === 'list_price' || field === 'coupon') return `${sign}${d.toFixed(2)}`
  return `${sign}${d.toLocaleString('en-US')}`
}

/** ABA 周变动：正数名次上升。用箭头比正负号直观，一眼看得出现在是热了还是凉了。 */
function rankMove(v: number | null | undefined): string {
  if (v == null) return '—'
  if (v === 0) return '0'
  return v > 0 ? `↑${v}` : `↓${Math.abs(v)}`
}

export default function Cockpit({ signal }: { signal?: number }) {
  const [data, setData] = useState<CockpitSummary | null>(null)
  const [err, setErr] = useState<string | null>(null)
  // 位次单独取：它依赖外部抓取，随时可能是空的，不能拖累首屏那六格。
  // 取不到就当 null 交给 RankPanel 显示「暂无数据」，不报错、不挡页面。
  const [rank, setRank] = useState<RankPanelData | null>(null)

  useEffect(() => {
    get<CockpitSummary>('/cockpit/summary')
      .then(setData)
      .catch((e) => setErr(String(e.message || e)))
    get<RankPanelData>('/cockpit/rank')
      .then(setRank)
      .catch(() => setRank(null))
  }, [])

  // 常驻挂载：App 每次轮询到新数据都会把 signal 加一，这里跟着重取一次。
  useEffect(() => {
    if (!signal) return
    get<CockpitSummary>('/cockpit/summary')
      .then(setData)
      .catch((e) => setErr(String(e.message || e)))
    get<RankPanelData>('/cockpit/rank')
      .then(setRank)
      .catch(() => setRank(null))
  }, [signal])

  if (err) return <div className="err">打不开驾驶舱：{err}</div>
  if (!data) return <div className="note">读取中…</div>

  const f = data.funnel as any
  const p = data.portfolios as any
  const c = data.conflicts as any
  const k = data.keywords as any
  const m = data.market as any
  const a = data.attribution as any

  const series = isErr(f) ? [] : f.series || []
  const dates = series.map((s: any) => s.date)
  const costs = series.map((s: any) => s.cost)
  const acoss = series.map((s: any) => s.acos)
  const cvrs = series.map((s: any) => s.cvr)

  // ROAS = 销售额 / 花费。ACOS 的倒数，但人看的是「一块钱回来几块」，
  // 换算成本低、解释成本也低，两个都摆着省得心算。
  const cost = !isErr(f) ? Number(f.totals.cost ?? 0) : 0
  const sales = !isErr(f) ? Number(f.totals.sales ?? 0) : 0
  const roas = cost > 0 ? sales / cost : null

  const pfMax = isErr(p) ? 1 : Math.max(0.0001, ...(p.portfolios || []).map((x: any) => x.cvr_hi || 0))
  const kwMax = isErr(k) ? 1 : Math.max(0.0001, ...(k.keywords || []).map((x: any) => x.cost || 0))

  return (
    <div>
      {/* 首屏六格：不看图也知道今天花了多少、赚不赚。图给趋势，数字给结论 ——
          以前要滚到每张卡里各看一个 tag 才拼出全貌。 */}
      {!isErr(f) && (
        <div className="kpi-grid" style={{ marginBottom: 12 }}>
          <div className="kpi">
            <div className="k-label">花费</div>
            <div className="k-value">{fmtMoney(f.totals.cost)}</div>
            <div className="k-extra">{f.window.start} ~ {f.window.end}</div>
          </div>
          <div className="kpi">
            <div className="k-label">销售额</div>
            <div className="k-value">{fmtMoney(f.totals.sales)}</div>
            <div className="k-extra">{fmtInt(f.totals.orders)} 单</div>
          </div>
          <div className={`kpi ${f.totals.acos != null && f.totals.acos > 0.35 ? 'alert' : ''}`}>
            <div className="k-label">ACOS</div>
            <div className="k-value">{fmtPct(f.totals.acos)}</div>
            <div className="k-extra">{f.totals.acos != null && f.totals.acos > 0.35 ? '高于 35%' : '在目标内'}</div>
          </div>
          <div className="kpi">
            <div className="k-label">ROAS</div>
            <div className="k-value">{roas == null ? '—' : `${roas.toFixed(2)}×`}</div>
            <div className="k-extra">每花 1 块回来多少</div>
          </div>
          <div className="kpi">
            <div className="k-label">CVR</div>
            <div className="k-value">{fmtPct(f.totals.cvr)}</div>
            <div className="k-extra">{fmtInt(f.totals.clicks)} 击</div>
          </div>
          <div className="kpi">
            <div className="k-label">CPC</div>
            <div className="k-value">{f.totals.cpc == null ? '—' : `$${Number(f.totals.cpc).toFixed(2)}`}</div>
            <div className="k-extra">每点击成本</div>
          </div>
          <div className="kpi">
            <div className="k-label">待处理</div>
            <div className="k-value">{isErr(a) ? '—' : a.open_count}</div>
            <div className="k-extra">条归因窗在跟踪</div>
          </div>
        </div>
      )}
      {/* ① 账户漏斗趋势 */}
      <div className="card">
        <h2>
          账户漏斗趋势
          <span className="sub">
            {isErr(f) ? '取不到' : `${f.window.start} ~ ${f.window.end}`}
          </span>
        </h2>
        {isErr(f) ? (
          <div className="err">{f.error}</div>
        ) : (
          <>
            <div className="row" style={{ marginBottom: 8 }}>
              <span className="tag">花费 {fmtMoney(f.totals.cost)}</span>
              <span className="tag">点击 {fmtInt(f.totals.clicks)}</span>
              <span className="tag">订单 {fmtInt(f.totals.orders)}</span>
              <span className="tag">销售 {fmtMoney(f.totals.sales)}</span>
              <span className="tag">CVR {fmtPct(f.totals.cvr)}</span>
              <span className="tag">ACOS {fmtPct(f.totals.acos)}</span>
              <span className="tag">CPC {f.totals.cpc == null ? '—' : `$${Number(f.totals.cpc).toFixed(2)}`}</span>
            </div>
            <div className="small muted">
              每日花费
              {dates.length > IMMATURE_DAYS && (
                <span className="muted"> · 右侧灰区是还没结算的最后 {IMMATURE_DAYS} 天</span>
              )}
            </div>
            <svg className="chart" viewBox={`0 0 ${W} ${70}`}>
              <Line values={costs} width={W} height={70} color="#2563eb" labels={dates} format={fmtMoney} immature={IMMATURE_DAYS} />
            </svg>
            {/* ACOS 与 CVR 并排：它们共用一个横轴、量级都是百分比，竖着叠三张
                等于让眼睛上下跑三趟才能把同一天的三个数对上。宽屏两列，窄屏自动回单列。 */}
            <div className="mini-grid">
              <div>
                <div className="small muted">每日 ACOS</div>
                <svg className="chart" viewBox={`0 0 ${MINI_W} ${MINI_H}`}>
                  <Line values={acoss} width={MINI_W} height={MINI_H} color="#d97706" labels={dates} format={(v) => fmtPct(v)} immature={IMMATURE_DAYS} />
                </svg>
              </div>
              <div>
                <div className="small muted">每日 CVR</div>
                <svg className="chart" viewBox={`0 0 ${MINI_W} ${MINI_H}`}>
                  <Line values={cvrs} width={MINI_W} height={MINI_H} color="#0d9488" labels={dates} format={(v) => fmtPct(v)} immature={IMMATURE_DAYS} />
                </svg>
              </div>
            </div>
          </>
        )}
      </div>

      {/* ② 投放组合对比 */}
      <div className="card">
        <h2>
          投放组合对比
          <span className="sub">点估计 + 95% 区间 —— 区间不重叠才算真差别</span>
        </h2>
        {isErr(p) ? (
          <div className="err">{p.error}</div>
        ) : (
          <svg className="chart" viewBox={`0 0 ${W} ${(p.portfolios || []).length * 24 + 8}`}>
            {(p.portfolios || []).map((x: any, i: number) => (
              <g key={x.name} transform={`translate(0, ${i * 24 + 2})`}>
                <IntervalBar label={x.name} lo={x.cvr_lo} hi={x.cvr_hi} point={x.cvr} max={pfMax} width={W} />
              </g>
            ))}
          </svg>
        )}
      </div>

      {/* ③ 结构冲突 */}
      <div className="card">
        <h2>
          结构冲突（同词多活动）
          <span className="sub">
            {isErr(c) ? '取不到' : `${c.conflict_count} 组 · 异常 ${c.abnormal_count} 个`}
          </span>
        </h2>
        {isErr(c) ? (
          <div className="err">{c.error}</div>
        ) : (c.conflicts || []).length === 0 ? (
          <div className="note">没有同词多活动。</div>
        ) : (
          <table>
            <thead>
              <tr>
                <th>关键词 / 活动</th>
                <th>最紧位次</th>
                <th className="num">该位次点击</th>
                <th className="num">该位次 CPC</th>
                <th className="num">当日 bid × 溢价</th>
                <th className="num">占该位次上限</th>
                <th>判定</th>
              </tr>
            </thead>
            <tbody>
              {(c.conflicts || []).map((g: any) =>
                (g.campaigns || []).map((cp: CampaignInConflict, i: number) => (
                  <tr key={`${g.keyword_text}-${cp.campaign_id}`}>
                    <td>
                      {i === 0 ? <b>{g.keyword_text}</b> : ''}
                      <div className="small muted">{cp.campaign_name}</div>
                      <div className="small muted">
                        全位次均价 {cp.cpc_all_placements == null ? '—' : `$${cp.cpc_all_placements.toFixed(2)}`}
                        {' · '}
                        {fmtInt(cp.clicks)} 击 / {fmtInt(cp.orders)} 单 / {fmtMoney(cp.cost)}
                      </div>
                    </td>
                    <td>
                      {cp.tightest ? (
                        <>
                          <div className="small">{cp.tightest.placement.replace(' on-Amazon', '')}</div>
                          <div className="small muted">{cp.tightest.report_date}</div>
                        </>
                      ) : (
                        <span className="muted">—</span>
                      )}
                    </td>
                    <td className="num">{cp.tightest ? fmtInt(cp.tightest.clicks) : '—'}</td>
                    <td className="num">
                      {cp.tightest ? `$${cp.tightest.cpc.toFixed(2)}` : '—'}
                    </td>
                    <td className="num">
                      {cp.tightest ? (
                        <>
                          ${cp.tightest.bid.toFixed(2)} × {cp.tightest.bid_adjustment}%
                          <span className="small muted"> = ${cp.tightest.cap.toFixed(2)}</span>
                        </>
                      ) : (
                        '—'
                      )}
                    </td>
                    <td className="num">{cp.tightest ? fmtPct(cp.tightest.ratio) : '—'}</td>
                    <td>
                      <span className={`tag ${cp.flag || 'unknown'}`}>
                        {cp.flag === 'mismatch'
                          ? '配置对不上'
                          : cp.flag === 'near'
                            ? '接近上限'
                            : cp.flag === 'unknown'
                              ? '无可判数据'
                              : '正常'}
                      </span>
                      {cp.config_changed && (
                        <div className="small muted">窗口内改过配置</div>
                      )}
                    </td>
                  </tr>
                )),
              )}
            </tbody>
          </table>
        )}
        <div className="spacer" />
        <div className="note">
          逐日 × 逐位次算：<span className="mono">上限 = 当天 bid × (1 + 当天溢价%)</span>，
          再拿那天的 <span className="mono">花费 ÷ 点击</span> 比它。<b>不用活动全位次均价比单个位次的上限</b>
          ——混合均价会把风险稀释成假安全（`lat pulldown` 主活动全位次均价只有上限的 41%，它 TOS 那档是 100%）。
          「配置对不上」= 比例超出 1：可能是当天改过配置，也可能是报表的溢价字段与当天实际配置不同步，
          <b>不是「买超了平台上限」</b>，去核那一天。活动名里的「溢价1.4」是过期标签，真实值取
          <span className="mono">fact_placement_daily.bid_adjustment</span>。
        </div>
      </div>

      {/* ④ 关键词词族排行 */}
      <div className="card">
        <h2>
          关键词词族排行
          <span className="sub">按花费</span>
        </h2>
        {isErr(k) ? (
          <div className="err">{k.error}</div>
        ) : (
          <svg className="chart" viewBox={`0 0 ${W} ${(k.keywords || []).length * 22 + 8}`}>
            {(k.keywords || []).map((x: any, i: number) => (
              <g key={x.keyword_text} transform={`translate(0, ${i * 22 + 2})`}>
                <BarRow
                  label={x.keyword_text}
                  value={x.cost}
                  max={kwMax}
                  width={W}
                  right={`${fmtMoney(x.cost)} / ${x.orders} 单`}
                />
              </g>
            ))}
          </svg>
        )}
      </div>

      {/* ⑤ 外部市场变动 */}
      <div className="card">
        <h2>
          外部市场变动
          <span className="sub">
            {isErr(m) ? '取不到' : m.status === 'connected' ? m.note || 'sorftime' : 'sorftime'}
          </span>
        </h2>
        {isErr(m) ? (
          <div className="err">{m.error}</div>
        ) : m.status !== 'connected' ? (
          // 表没建 / 表建了但没采到 —— 两种都如实说，别画空表假装有数据
          <div className="note">{m.note || '还没接外部市场数据。'}</div>
        ) : (
          <>
            <table>
              <thead>
                <tr>
                  <th>对象</th>
                  <th>动作</th>
                  <th className="num">数值变化</th>
                  <th>观测</th>
                </tr>
              </thead>
              <tbody>
                {(m.watched || []).map((w: MarketMove, i: number) => (
                  <tr key={`${w.kind}-${w.subject}-${w.field}-${w.observed_at}-${i}`}>
                    <td>
                      <b>{w.subject}</b>
                      <div className="small muted">
                        {w.kind === 'competitor' ? '竞品' : 'ABA'}
                        {w.label ? ` · ${w.label}` : ''}
                      </div>
                    </td>
                    <td>
                      <span className="tag">{w.move_label}</span>
                      <div className="small muted">{w.field_label}</div>
                    </td>
                    <td className="num">
                      {fmtMove(w.field, w.old_value)} → <b>{fmtMove(w.field, w.new_value)}</b>
                      <div className="small muted">
                        {fmtDelta(w.field, w.delta, w.move_type)}
                        {/* ABA 名次必须带相对幅度：绝对值在长尾段没有可比性 */}
                        {w.rel_pct != null ? ` · 占当前名次 ${w.rel_pct}%` : ''}
                      </div>
                    </td>
                    <td>
                      <div className="small">{w.observed_date || '—'}</div>
                      {w.note ? <div className="small muted">{w.note}</div> : null}
                    </td>
                  </tr>
                ))}
                {(m.watched || []).length === 0 ? (
                  <tr>
                    <td colSpan={4} className="muted small">
                      有数据但这两天竞品没动、名次也没跳 —— 空表本身是结论。
                    </td>
                  </tr>
                ) : null}
              </tbody>
            </table>

            {(m.aba || []).length > 0 ? (
              <>
                <div className="small muted" style={{ margin: '12px 0 4px' }}>
                  自家搜索词的 ABA 名次（{m.latest_week || '—'}）· 前 {(m.aba || []).length} / 共 {m.aba_total ?? (m.aba || []).length} 个词
                </div>
                <table>
                  <thead>
                    <tr>
                      <th className="num">ABA 名次</th>
                      <th>搜索词</th>
                      <th className="num">周变动</th>
                      <th className="num">月搜索量</th>
                      <th className="num">点击占比</th>
                      <th className="num">Top3 品牌</th>
                      <th className="num">建议竞价</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(m.aba || []).map((a: MarketAbaRow) => (
                      <tr key={`${a.week}-${a.term}`}>
                        <td className="num">{a.rank == null ? '—' : a.rank}</td>
                        <td>
                          <b>{a.term}</b>
                          {a.term_cn ? <div className="small muted">{a.term_cn}</div> : null}
                        </td>
                        <td className="num">{rankMove(a.rank_gain)}</td>
                        <td className="num">{fmtInt(a.search_volume)}</td>
                        <td className="num">
                          {a.click_share == null ? '—' : `${a.click_share.toFixed(1)}%`}
                        </td>
                        <td className="num">
                          {a.top3_brand_share == null ? '—' : `${a.top3_brand_share.toFixed(1)}%`}
                        </td>
                        <td className="num">{fmtMoney(a.cpc_bid)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </>
            ) : null}

            {(m.markets || []).length > 0 ? (
              <div className="small muted" style={{ marginTop: 10 }}>
                最近行情：
                {(m.markets || [])
                  .slice(0, 4)
                  .map((k: MarketDailyRow) => {
                    const bits: string[] = []
                    if (k.avg_price != null) bits.push(`均价 ${fmtMoney(k.avg_price)}`)
                    if (k.price != null) bits.push(`售价 ${fmtMoney(k.price)}`)
                    if (k.top3_brand_share != null) bits.push(`Top3 品牌 ${k.top3_brand_share.toFixed(1)}%`)
                    if (k.subcategory_rank != null) bits.push(`子类第 ${k.subcategory_rank} 名`)
                    if (k.top100_sales_volume != null) bits.push(`Top100 月销 ${fmtInt(k.top100_sales_volume)}`)
                    return `${k.subject_name || k.subject_id}（${k.report_date}）${bits.join(' · ')}`
                  })
                  .join('；')}
              </div>
            ) : null}
          </>
        )}
      </div>

      {/* ⑥ 位次（卡位） */}
      <RankPanel data={rank} />

      {/* ⑦ 归因窗追踪 */}
      <div className="card">
        <h2>
          归因窗追踪
          <span className="sub">
            {isErr(a) ? '取不到' : `在跟踪 ${a.open_count} 条 · 已到期 ${a.due_count} 条`}
          </span>
        </h2>
        {isErr(a) ? (
          <div className="err">{a.error}</div>
        ) : (a.open || []).length === 0 ? (
          <div className="note">
            没有正在跟踪的动作。归因窗从「标已改」那一刻开始算，不是从打钩开始。
          </div>
        ) : (
          <table>
            <thead>
              <tr>
                <th>对象</th>
                <th>动作</th>
                <th>改于</th>
                <th>窗末</th>
                <th>可结算</th>
                <th className="num">距结算</th>
                <th>状态</th>
              </tr>
            </thead>
            <tbody>
              {(a.open || []).map((d: any) => (
                <tr key={d.decision_id}>
                  <td>{d.entity_label}</td>
                  <td className="small">{d.action_type}</td>
                  <td className="small muted">{(d.executed_at || '').slice(0, 16)}</td>
                  {/* 窗末 ≠ 可结算：报表要过几天才把窗口最后几天回传齐，
                      归因 agent 等到 settle_at 才动手。分两列写，免得
                      「到期了怎么还没回填」这种误读。 */}
                  <td className="small muted">{(d.attribution_due_at || '').slice(0, 16)}</td>
                  <td className="small muted">{(d.settle_at || '—').slice(0, 16)}</td>
                  <td className="num">{d.days_left ?? '—'}</td>
                  <td>
                    <span className={`tag ${d.overdue ? 'over' : 'ok'}`}>
                      {d.overdue
                        ? d.seen_7d
                          ? '该看 14 天终局'
                          : '该回填 7 天'
                        : d.seen_7d
                          ? '7 天已看，等终局'
                          : '观察中'}
                    </span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}

        {/* 已回填：执行前 vs 执行后，扣掉同期全店那层 */}
        {!isErr(a) && (a.filled || []).length > 0 && (
          <>
            <div className="small muted" style={{ margin: '14px 0 6px' }}>
              已回填 {a.filled_count ?? (a.filled || []).length} 条 ——
              比的是「相对全店」的变化，不是绝对数字。执行前后对比不是 A/B 测试。
            </div>
            <table>
              <thead>
                <tr>
                  <th>对象</th>
                  <th>动作</th>
                  <th>回填于</th>
                  <th>判定</th>
                  <th>证据</th>
                  <th>一句话</th>
                </tr>
              </thead>
              <tbody>
                {(a.filled || []).map((d: any) => {
                  const o = d.outcome_14d || d.outcome_7d || {}
                  const v = o.verdict || 'no_data'
                  return (
                    <tr key={d.decision_id}>
                      <td>{d.entity_label}</td>
                      <td className="small">{d.action_type}</td>
                      <td className="small muted">
                        {(d.outcome_filled_at || '').slice(0, 16)}
                      </td>
                      <td>
                        <span className={`tag ${VERDICT_TAG[v] || ''}`}>
                          {VERDICT_LABEL[v] || v}
                        </span>
                      </td>
                      <td>
                        {o.evidence_level
                          ? (
                            <span className={`tag ${LEVEL_TAG[o.evidence_level] || 'low'}`}>
                              {LEVEL_LABEL[o.evidence_level] || o.evidence_level}
                            </span>
                          )
                          : <span className="small muted">—</span>}
                      </td>
                      <td className="small">{o.why || o.basis || '—'}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </>
        )}
      </div>
    </div>
  )
}

const VERDICT_LABEL: Record<string, string> = {
  improved: '变好',
  worsened: '变差',
  flat: '看不出方向',
  insufficient: '证据不够',
  no_data: '还没数据',
}

const LEVEL_LABEL: Record<string, string> = {
  strong: '强',
  medium: '中',
  weak: '弱',
}

const LEVEL_TAG: Record<string, string> = {
  strong: 'high',
  medium: 'medium',
  weak: 'low',
}

const VERDICT_TAG: Record<string, string> = {
  improved: 'ok',
  worsened: 'over',
}