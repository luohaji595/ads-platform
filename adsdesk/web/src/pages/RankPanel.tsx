/**
 * 位次面板 —— 只负责把 `GET /api/cockpit/rank` 画出来。
 *
 * 三个刻意的选择：
 *
 * 1. **不自己取数**。props 收整个接口返回，取数交给接它的那一层
 *    （和 Cockpit.tsx 里 `data.market` 那种用法一致）。这样这个文件不碰网络，
 *    也就不需要往 lib/api.ts 里加类型 —— 类型就声明在这里，接口变了自己改一处。
 *
 * 2. **当前位次只认广告位（sponsored）**。后端 `_rank_latest()` 也是这个口径，
 *    因为 engine 的 `rank_state()` 只读 `placement='sponsored'` —— 卡位加价打的是
 *    关键词竞价位，自然位再高也不受 bid 影响。面板要是显示自然位，就会出现
 *    「面板说第 37 位、引擎说第 7 位」两套数字打架。自然位在库里都留着，
 *    这里只在「这个词压根没有广告位观测」时退回显示，并把 placement 如实标出来。
 *
 * 3. **走向用文字不用图**。位次是 5 个点的小序列，画折线要引入坐标系、刻度、
 *    悬停提示，收益远不如「23 → 19 → 34 → 7」一行字看得快。跨页时给每个点标
 *    页码（`p1:23 → p3:34`）—— 第 3 页第 34 位和第 1 页第 19 位不是一回事，
 *    不标页码的箭头会骗人。
 *
 * 缺值一律显示 `—`，不显示 0：位次 0 会被 engine 的 `in_goal()` 判成「已到位」，
 * 那是把「没数据」当成「好消息」。
 */

/** 一行卡位目标。字段名与 `/api/cockpit/rank` 的 targets[] 一一对应。 */
export type RankTargetRow = {
  keyword_id: string
  keyword_text: string
  goal_page: number
  goal_position: number
  /** 最新一次「广告位」观测的位次；null = 还没采到 / 源端没有这个词的广告位数据 */
  latest_position: number | null
  latest_page: number | null
  latest_placement: string | null
  latest_asin: string | null
  observed_at: string | null
  /** 同页才有意义：position − goal_position。跨页时后端给 null（页容量不同，不可相减） */
  gap_vs_goal: number | null
  on_goal: boolean
  pushes_used: number
  max_pushes: number
  status: string
}

/** 走势上的一个观测点。 */
export type RankSeriesPoint = {
  observed_at: string
  page: number | null
  position: number | null
}

export type RankPanelData = {
  status?: string
  note?: string | null
  targets?: RankTargetRow[]
  /** key 是 keyword_text（后端用词做 key，因为面板要按词读） */
  series?: Record<string, RankSeriesPoint[]>
  summary?: { n_targets: number; n_have_data: number; n_on_goal: number }
}

/** 和 engine 的 `RANK_SNAPSHOT_MAX_AGE_HOURS` 同值。超时的位次不许再据此加价，
 *  所以面板上要能一眼看出「这个数字是旧的」，而不是等引擎静默抑制。 */
const MAX_AGE_HOURS = 6

const EMPTY_HINT = '位次快照还没采集，先跑 `python3 ingest/rank.py --dataset snapshot`'

function num(v: number | null | undefined): string {
  return v == null ? '—' : String(v)
}

/** 「第 1 页第 4 位」。分页位次要连页一起说，单说「第 4 位」会被读成第 1 页的。 */
function goalText(page: number, position: number): string {
  return `第 ${page} 页第 ${position} 位`
}

/** 观测时间裁到分钟。秒对位次判断没意义，反而把表格撑宽。 */
function fmtAt(v: string | null | undefined): string {
  if (!v) return '—'
  return v.replace('T', ' ').slice(0, 16)
}

/** 距今多少小时。解析不出来就给 null —— 不猜，前端显示「—」。 */
function ageHours(v: string | null | undefined): number | null {
  if (!v) return null
  const t = new Date(v.replace(' ', 'T')).getTime()
  if (!Number.isFinite(t)) return null
  return (Date.now() - t) / 3600000
}

/** 距目标差几位。
 *  - 跨页：后端已经给 null，这里也返回 null（页容量不同，相减没有意义）
 *  - 同页：正数 = 还差几位要往上爬，0 = 正好在目标位，负数 = 比目标还靠前
 *  `unseen`（position 为 null）不算「差 0」，算「不知道」。
 */
function gapText(row: RankTargetRow): string {
  if (row.latest_position == null) return '—'
  if (row.gap_vs_goal == null) return '跨页'
  const g = row.gap_vs_goal
  if (g === 0) return '正好'
  return g > 0 ? `差 ${g}` : `超 ${-g}`
}

/** 一行走向：`p1:23 → p1:19 → p3:34 → p1:7`。
 *  页码全一致时省略页码，读起来就是需求里那句「位置 23 → 19 → 7」。
 */
function seriesText(points: RankSeriesPoint[]): string {
  const usable = points.filter((p) => p.position != null)
  if (!usable.length) return '—'
  const pages = new Set(usable.map((p) => p.page))
  const showPage = pages.size > 1
  return usable
    .map((p) => (showPage ? `p${p.page ?? '?'}:${p.position}` : String(p.position)))
    .join(' → ')
}

export default function RankPanel({ data }: { data: RankPanelData | null }) {
  const targets = data?.targets ?? []
  const series = data?.series ?? {}
  const summary = data?.summary ?? { n_targets: 0, n_have_data: 0, n_on_goal: 0 }
  const note = data?.note ?? null

  // 三态分开说，别把「表没建」「没采到」「采到了但没对上目标」混成一句：
  // 三种情况的下一步动作完全不同（migrate / 跑 snapshot / 查 keyword_id 口径）。
  if (!data) {
    return (
      <div className="card">
        <h2>
          位次卡位<span className="sub">读取中…</span>
        </h2>
        <div className="note">读取中…</div>
      </div>
    )
  }

  if (targets.length === 0) {
    return (
      <div className="card">
        <h2>
          位次卡位<span className="sub">暂无位次数据</span>
        </h2>
        <div className="note">
          {note || EMPTY_HINT}
          <div className="small muted" style={{ marginTop: 6 }}>
            先种卡位目标再采位次：`python3 ingest/rank.py --dataset target-seed` →
            `--dataset snapshot`（两个都能先加 --dry-run 看要写多少行）
          </div>
        </div>
      </div>
    )
  }

  return (
    <div className="card">
      <h2>
        位次卡位
        <span className="sub">
          {summary.n_have_data}/{summary.n_targets} 个词有位次 · 已在目标位 {summary.n_on_goal} 个
        </span>
      </h2>

      <table>
        <thead>
          <tr>
            <th>关键词</th>
            <th>目标位次</th>
            <th className="num">当前位次</th>
            <th className="num">当前页</th>
            <th>位次类型</th>
            <th>观测时间</th>
            <th className="num">差几个位次</th>
            <th className="num">已推次数</th>
          </tr>
        </thead>
        <tbody>
          {targets.map((t) => {
            const age = ageHours(t.observed_at)
            const stale = age != null && age > MAX_AGE_HOURS
            return (
              <tr key={t.keyword_id}>
                <td>
                  <b>{t.keyword_text}</b>
                  {t.status !== 'active' ? (
                    <div className="small muted">{t.status}</div>
                  ) : null}
                </td>
                <td className="small">{goalText(t.goal_page, t.goal_position)}</td>
                <td className="num">
                  {num(t.latest_position)}
                  {t.on_goal ? (
                    <span className="tag ok" style={{ marginLeft: 6 }}>
                      到位
                    </span>
                  ) : null}
                </td>
                <td className="num">{num(t.latest_page)}</td>
                <td className="small">
                  {/* 广告位才是卡位真正能影响的那条轨道；自然位只作对照。 */}
                  {t.latest_placement === 'sponsored'
                    ? '广告位'
                    : t.latest_placement === 'organic'
                      ? '自然位'
                      : '—'}
                </td>
                <td className="small muted">
                  {fmtAt(t.observed_at)}
                  {stale ? (
                    <span className="tag near" style={{ marginLeft: 6 }}>
                      已过期
                    </span>
                  ) : null}
                </td>
                <td className="num">{gapText(t)}</td>
                <td className="num">
                  {t.pushes_used}
                  <span className="small muted"> / {t.max_pushes}</span>
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>

      <div className="small muted" style={{ margin: '14px 0 4px' }}>
        位次走向（每个词最近 5 个观测点，从左到右由旧到新；数字越小越靠前）
      </div>
      {Object.keys(series).length === 0 ? (
        <div className="note">还没有可画的走向 —— 位次快照里没有这些词的广告位观测。</div>
      ) : (
        <table>
          <thead>
            <tr>
              <th>关键词</th>
              <th>最近走向</th>
            </tr>
          </thead>
          <tbody>
            {Object.entries(series).map(([kw, points]) => (
              <tr key={kw}>
                <td className="small">{kw}</td>
                <td className="mono">{seriesText(points)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {note ? (
        <div className="small muted" style={{ marginTop: 10 }}>
          {note}
        </div>
      ) : null}
    </div>
  )
}
