/** 被拦下的动作：收在诊断抽屉中，按 (rule_id, reason) 分组折叠展示。 */
import { Fragment, useMemo, useState } from 'react'
import type { Suppressed } from '../../lib/api'
import { suppressedWhy } from './shared'

export function SuppressedCard({ suppressed }: { suppressed: Suppressed[] }) {
  const [open, setOpen] = useState<string | null>(null)

  const groups = useMemo(() => {
    const m = new Map<string, { rule_id: string; reason: string; items: Suppressed[] }>()
    for (const s of suppressed) {
      const k = `${s.rule_id}|${s.reason}`
      const g = m.get(k) || { rule_id: s.rule_id, reason: s.reason, items: [] }
      g.items.push(s)
      m.set(k, g)
    }
    return [...m.values()].sort((a, b) => b.items.length - a.items.length)
  }, [suppressed])

  if (suppressed.length === 0) return null

  return (
    <div className="card">
      <h2>
        被拦下
        <span className="sub">
          {suppressed.length} 条 · {groups.length} 种原因 · 全部未进待批
        </span>
      </h2>
      <table className="tbl">
        <tbody>
          {groups.map((g) => {
            const k = `${g.rule_id}|${g.reason}`
            const isOpen = open === k
            return (
              <Fragment key={k}>
                <tr style={{ cursor: 'pointer' }} onClick={() => setOpen(isOpen ? null : k)}>
                  <td style={{ width: 54 }}>
                    <span className="tag">{g.rule_id}</span>
                  </td>
                  <td>
                    <b>{g.reason}</b>
                    <span className="muted small"> {isOpen ? '▾' : '▸'}</span>
                  </td>
                  <td className="num" style={{ width: 60 }}>
                    {g.items.length} 条
                  </td>
                </tr>
                {isOpen &&
                  g.items.map((s) => (
                    <tr key={`${k}-${s.entity}`}>
                      <td />
                      <td colSpan={2} className="small" style={{ paddingLeft: 12 }}>
                        <b>{s.entity}</b>
                        <div className="muted">{suppressedWhy(s)}</div>
                      </td>
                    </tr>
                  ))}
              </Fragment>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
