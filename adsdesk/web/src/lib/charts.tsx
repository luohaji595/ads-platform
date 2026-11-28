/** 手写 SVG 的图元。不引图表库：六面板、十几条线，多 30 个依赖不值。 */

export const fmtMoney = (v: number | null | undefined) =>
  v == null ? '—' : `$${v.toFixed(2)}`

export const fmtPct = (v: number | null | undefined, d = 2) =>
  v == null ? '—' : `${(v * 100).toFixed(d)}%`

export const fmtInt = (v: number | null | undefined) =>
  v == null ? '—' : v.toLocaleString('en-US')

/** 折线。points 已归一化到 [0,1]。 */
export function Line({
  values,
  width,
  height,
  color = 'var(--chart-1)',
  fill = true,
  immature = 0,
  labels,
  format,
}: {
  values: (number | null)[]
  width: number
  height: number
  color?: string
  fill?: boolean
  /** 末尾还有几天报表没回填归因：留白不画线，画成虚线区，别让人把半截数据当结论 */
  immature?: number
  /** 每个点的横轴标签。给了才出原生 hover 提示，不给就纯图 */
  labels?: string[]
  /** hover 提示里的数值格式 */
  format?: (v: number) => string
}) {
  const nums = values as (number | null)[]
  const valid = nums.filter((v): v is number => v != null)
  if (valid.length < 2) return <text x={4} y={height / 2} className="svg-empty">数据不足</text>
  const min = Math.min(...valid)
  const max = Math.max(...valid)
  const span = max - min || 1
  // 末尾留白也占横轴：否则最后一天紧贴右边框，看不出「还有两天没结算」。
  const slots = nums.length + Math.max(0, immature)
  const step = width / Math.max(1, slots - 1)
  const yOf = (v: number) => height - ((v - min) / span) * (height - 8) - 4
  // 遇到 null 就断开，不要一条线跨过缺口 —— 跨过去等于替缺数据的那几天编了个值。
  const segs: string[] = []
  let cur: string[] = []
  nums.forEach((v, i) => {
    if (v == null) {
      if (cur.length) {
        segs.push(`M ${cur.join(' L ')}`)
        cur = []
      }
      return
    }
    cur.push(`${(i * step).toFixed(1)},${yOf(v).toFixed(1)}`)
  })
  if (cur.length) segs.push(`M ${cur.join(' L ')}`)
  const d = segs[0] || ''
  // 未成熟区从「最后一个点的半步之后」起算到右边框。
  const bandX = Math.max(0, (nums.length - 1 + 0.5) * step)
  const fillRight = immature > 0 ? bandX : width
  return (
    <>
      {fill && d && (
        <path
          d={`${d} L ${fillRight.toFixed(1)},${height} L 0,${height} Z`}
          fill={color}
          opacity={0.08}
        />
      )}
      {segs.map((seg, i) => (
        <path key={i} d={seg} fill="none" stroke={color} strokeWidth={1.6} />
      ))}
      {immature > 0 && (
        <>
          <rect x={bandX} y={0} width={Math.max(0, width - bandX)} height={height} fill="var(--chart-mute)" opacity={0.1}>
            <title>{`末 ${immature} 天报表还在回传归因，这几天不进判断`}</title>
          </rect>
          <line className="dash" x1={bandX} y1={0} x2={bandX} y2={height} stroke="var(--chart-mute)" strokeWidth={1} />
        </>
      )}
      {labels &&
        nums.map((v, i) =>
          v == null ? null : (
            <circle key={`h${i}`} cx={i * step} cy={yOf(v)} r={5} fill="transparent">
              <title>{`${labels[i] ?? ''} ${format ? format(v) : v}`}</title>
            </circle>
          )
        )}
    </>
  )
}

/** 区间条：点估计 + 置信区间，用来一眼看出「差是真的还是样本不够」。 */
export function IntervalBar({
  label,
  lo,
  hi,
  point,
  max,
  width,
  color = 'var(--chart-3)',
}: {
  label: string
  lo: number
  hi: number
  point: number | null
  max: number
  width: number
  color?: string
}) {
  const H = 22
  const labelW = 150
  const plotW = width - labelW - 60
  const x = (v: number) => labelW + (v / (max || 1)) * plotW
  return (
    <g>
      <text x={0} y={H / 2 + 4} className="svg-label">
        {label.length > 18 ? label.slice(0, 18) + '…' : label}
      </text>
      <line x1={x(lo)} y1={H / 2} x2={x(hi)} y2={H / 2} stroke={color} strokeWidth={3} opacity={0.35} />
      <line x1={x(lo)} y1={H / 2 - 4} x2={x(lo)} y2={H / 2 + 4} stroke={color} strokeWidth={1.2} opacity={0.6} />
      <line x1={x(hi)} y1={H / 2 - 4} x2={x(hi)} y2={H / 2 + 4} stroke={color} strokeWidth={1.2} opacity={0.6} />
      {point != null && <circle cx={x(point)} cy={H / 2} r={3.2} fill={color} />}
      <text x={width - 54} y={H / 2 + 4} className="svg-label">
        {(point == null ? '—' : (point * 100).toFixed(2)) + '%'}
      </text>
    </g>
  )
}

/** 单值条形（词族排行用）。 */
export function BarRow({
  label,
  value,
  max,
  width,
  right,
  color = 'var(--chart-1)',
}: {
  label: string
  value: number
  max: number
  width: number
  right: string
  color?: string
}) {
  const H = 20
  const labelW = 200
  const barW = width - labelW - 78
  const w = max ? Math.max(1, (value / max) * barW) : 1
  return (
    <g>
      <text x={0} y={H / 2 + 4} className="svg-label">
        {label.length > 26 ? label.slice(0, 26) + '…' : label}
      </text>
      <rect x={labelW} y={H / 2 - 5} width={barW} height={10} fill="var(--bar-track)" rx={2} />
      <rect x={labelW} y={H / 2 - 5} width={w} height={10} fill={color} rx={2} />
      <text x={width - 70} y={H / 2 + 4} className="svg-label">
        {right}
      </text>
    </g>
  )
}