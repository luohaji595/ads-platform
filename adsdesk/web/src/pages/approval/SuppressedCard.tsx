/** 被拦下的动作。每条都有具体原因（不是「模型觉得不好」），
 *  原因是给人复核用的：如果理由不成立，那是规则该改，不是这条该批。
 */
import { useMemo, useState } from 'react'
import type { Suppressed } from '../../lib/api'
import { Rich, suppressedWhy } from './shared'

export function SuppressedCard({ suppressed }: { suppressed: Suppressed[] }) {
  const [open, setOpen] = useState<string | null>(null)

  // 按 (rule, reason) 归组：19 条散着列没人看，归成 9 组才知道「今天主要被什么挡住」。
  // 这个 useMemo 必须在任何 early return 之前 —— 否则 suppressed 从 0 变非 0 时
  // hook 数量变化，React 会直接抛错。
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
        {suppressed.length} 条 · {groups.length} 种原因 · 全部没进待批
      </span>
    </h2>
    <div className="note">
      规则算出来了，但被自己的保护闸门掐掉：花钱还没到该动的时候、改了等于没改、
      或者这一项本身不该动。它们不计入建议数，也不占审批流 —— 但今天到底挡住了什么，写在这里。
    </div>

    <div className="spacer" />
    <table className="tbl">
      <tbody>
        {groups.map((g) => {
          const k = `${g.rule_id}|${g.reason}`
          const isOpen = open === k
          return (
            <>
              <tr
                key={k}
                style={{ cursor: 'pointer' }}
                onClick={() => setOpen(isOpen ? null : k)}
              >
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
            </>
          )
        })}
      </tbody>
    </table>
  </div>
  )
}

/** 把 detail 翻成一句人话。每个 reason 的字段不一样，逐个认。 */
