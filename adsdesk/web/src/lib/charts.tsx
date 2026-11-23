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
  color = '#2563eb',
  fill = true,
}: {
  values: (number | null)[]
  width: number
  height: number
  color?: string
  fill?: boolean
}) {
  const nums = values.map((v) => (v == null ? null : v)) as (number | null)[]
  const valid = nums.filter((v): v is number => v != null)
  if (valid.length < 2) return <text x={4} y={height / 2} className="svg-empty">数据不足</text>
  const min = Math.min(...valid)
  const max = Math.max(...valid)
  const span = max - min || 1
  const step = width / (nums.length - 1)
  const pts: string[] = []
  nums.forEach((v, i) => {
    if (v == null) return
    const x = i * step
    const y = height - ((v - min) / span) * (height - 8) - 4
    pts.push(`${x.toFixed(1)},${y.toFixed(1)}`)
  })
  const d = `M ${pts.join(' L ')}`
  return (
    <>
      {fill && (
        <path
          d={`${d} L ${width},${height} L 0,${height} Z`}
          fill={color}
          opacity={0.08}
        />
      )}
      <path d={d} fill="none" stroke={color} strokeWidth={1.6} />
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
  color = '#0d9488',
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
  color = '#2563eb',
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
      <rect x={labelW} y={H / 2 - 5} width={barW} height={10} fill="#eef1f5" rx={2} />
      <rect x={labelW} y={H / 2 - 5} width={w} height={10} fill={color} rx={2} />
      <text x={width - 70} y={H / 2 + 4} className="svg-label">
        {right}
      </text>
    </g>
  )
}