import { useCallback, useEffect, useState } from 'react'
import { activatePolicy, getPolicyPending, rejectPolicy, type PolicyRow } from '../lib/api'

export type PolicyListProps = {
  /** App 轮询递增信号：页面常驻挂载时靠它刷新待认领策略 */
  signal?: number
  /** 为空时直接隐藏整张卡（今日一键台默认只在有待认领策略时浮出） */
  hideWhenEmpty?: boolean
  /** 认领或驳回成功后的回调 */
  onChanged?: () => void
  title?: string
  subtitle?: string
}

/**
 * 待认领策略列表（可复用组件）：
 * 归因实测有效才提案；人点「认领」后才会写库生效，引擎下次运行按它行事。
 */
export function PolicyList({
  signal,
  hideWhenEmpty = false,
  onChanged,
  title = '待认领的策略',
  subtitle = '归因实测有效才提案 · 认领后下次自动按此执行',
}: PolicyListProps) {
  const [rows, setRows] = useState<PolicyRow[] | null>(null)
  const [note, setNote] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [busy, setBusy] = useState<number | null>(null)
  const [msg, setMsg] = useState<string | null>(null)

  const load = useCallback(() => {
    setErr(null)
    getPolicyPending()
      .then((r) => {
        setRows(r.policies)
        setNote(r.note)
      })
      .catch((e: unknown) => setErr(String((e as Error)?.message || e)))
  }, [])

  useEffect(() => {
    load()
  }, [load, signal])

  async function act(id: number, kind: 'activate' | 'reject') {
    setBusy(id)
    setMsg(null)
    try {
      if (kind === 'activate') await activatePolicy(id)
      else await rejectPolicy(id)
      setMsg(`策略 #${id} 已${kind === 'activate' ? '认领生效' : '驳回'}`)
      load()
      onChanged?.()
    } catch (e: unknown) {
      setMsg(`#${id} 失败：${String((e as Error)?.message || e)}`)
    } finally {
      setBusy(null)
    }
  }

  if (hideWhenEmpty && !err && rows && rows.length === 0 && !msg) {
    return null
  }

  return (
    <div className="card">
      <h2>
        {title}
        <span className="sub">{subtitle}</span>
      </h2>
      {err && <div className="err">读不到策略：{err}</div>}
      {!err && !rows && <div className="note">读取中…</div>}
      {rows && rows.length === 0 && <div className="note">{note || '暂无待认领的策略。'}</div>}
      {rows &&
        rows.map((r) => (
          <div key={r.policy_id} style={{ borderTop: '1px solid var(--line)', paddingTop: 10, marginTop: 10 }}>
            <div className="row">
              <b>{r.type_label}</b>
              <span className="tag">{r.object_id}</span>
              {r.decision_id != null && <span className="tag">来自决策 #{r.decision_id}</span>}
            </div>
            {r.evidence_text && (
              <div className="small muted" style={{ margin: '6px 0' }}>
                {r.evidence_text}
              </div>
            )}
            <div className="small muted">来源 {r.source}</div>
            <div className="row" style={{ marginTop: 8 }}>
              <button
                className="btn primary"
                disabled={busy === r.policy_id}
                onClick={() => act(r.policy_id, 'activate')}
              >
                认领（以后按这个来）
              </button>
              <button
                className="btn"
                disabled={busy === r.policy_id}
                onClick={() => act(r.policy_id, 'reject')}
              >
                驳回
              </button>
            </div>
          </div>
        ))}
      {msg && (
        <div className="note" style={{ marginTop: 8 }}>
          {msg}
        </div>
      )}
    </div>
  )
}

export default PolicyList
