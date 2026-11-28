import { useEffect, useMemo, useState } from 'react'
import PolicyList from '../components/PolicyList'
import {
  get,
  post,
  type Decision,
  type Freshness,
  type MemorialToday,
  type Question,
} from '../lib/api'
import { AgentCard } from './approval/AgentCard'
import { ConclusionCard } from './approval/ConclusionCard'
import { DecisionRow } from './approval/DecisionRow'
import { ExecOrderCard } from './approval/ExecOrderCard'
import { ObservingCard } from './approval/ObservingCard'
import { QuestionsCard } from './approval/QuestionsCard'
import { SideCards } from './approval/SideCards'
import { SuppressedCard } from './approval/SuppressedCard'
import { DEST_LABEL, qkeyOf, type Mark } from './approval/shared'

export default function Approval({
  freshness,
  signal,
  onRefreshFresh,
}: {
  freshness: Freshness | null
  /** App 的轮询计数器：页面常驻挂载，靠它知道「该重拉账本了」 */
  signal?: number
  onRefreshFresh?: () => void
}) {
  const [mem, setMem] = useState<MemorialToday | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [marks, setMarks] = useState<Mark>({})
  const [reasons, setReasons] = useState<Record<string, string>>({})
  const [answers, setAnswers] = useState<Record<string, number[]>>({})
  const [crossed, setCrossed] = useState<Record<string, number[]>>({})
  const [freeText, setFreeText] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const [order, setOrder] = useState<any>(null)
  const [orderErr, setOrderErr] = useState<string | null>(null)

  const load = () => {
    setErr(null)
    get<MemorialToday>('/memorial/today')
      .then((m) => {
        setMem(m)
        const init: Mark = {}
        for (const d of m.decisions) {
          if (d.decision_id == null) continue
          if (d.status === 'approved') init[d.decision_id] = 'approve'
          else if (d.status === 'rejected') init[d.decision_id] = 'reject'
        }
        setMarks(init)
      })
      .catch((e) => setErr(String(e.message || e)))
  }

  const loadOrder = () => {
    get<{ ok: boolean; detail: any }>('/approve/exec-order/latest')
      .then((r) => {
        setOrder(r.detail)
        setOrderErr(null)
      })
      .catch((e: any) => {
        setOrderErr(String(e?.message || e))
      })
  }

  useEffect(load, [])
  useEffect(loadOrder, [])

  useEffect(() => {
    if (signal) {
      load()
      loadOrder()
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [signal])

  const decisions: Decision[] = useMemo(() => {
    if (!mem) return []
    const byId = new Map<number, Decision>()
    for (const d of mem.decisions) if (d.decision_id != null) byId.set(d.decision_id, d)
    return mem.pack.front.map((d) => (d.decision_id != null && byId.get(d.decision_id)) || d)
  }, [mem])

  const observing: Decision[] = useMemo(() => {
    if (!mem) return []
    const byId = new Map<number, Decision>()
    for (const d of mem.decisions) if (d.decision_id != null) byId.set(d.decision_id, d)
    return (mem.pack.observing ?? []).map(
      (d) => (d.decision_id != null && byId.get(d.decision_id)) || d,
    )
  }, [mem])

  const pendingCount = decisions.filter((d) => d.decision_id != null && !marks[d.decision_id]).length

  const unsubmitted = decisions.filter((d) => {
    if (d.decision_id == null) return false
    const want = marks[d.decision_id]
    if (!want) return false
    return d.status !== (want === 'approve' ? 'approved' : 'rejected')
  }).length

  async function submitDecision(d: Decision) {
    if (d.decision_id == null) return
    const mark = marks[d.decision_id]
    if (!mark) return
    setBusy(true)
    setMsg(null)
    try {
      await post('/approve/decision', {
        decision_id: d.decision_id,
        status: mark === 'approve' ? 'approved' : 'rejected',
        reject_reason: mark === 'reject' ? reasons[d.decision_id] || '' : undefined,
        reject_reason_code: mark === 'reject' ? 'owner_reject' : undefined,
      })
      setMsg(`#${d.decision_id} 已记录`)
      load()
      onRefreshFresh?.()
    } catch (e: any) {
      setMsg(`#${d.decision_id} 失败：${e.message}`)
    } finally {
      setBusy(false)
    }
  }

  async function submitAll() {
    const items = decisions
      .filter((d) => d.decision_id != null && marks[d.decision_id!])
      .filter((d) => {
        const want = marks[d.decision_id!] === 'approve' ? 'approved' : 'rejected'
        return d.status !== want
      })
      .map((d) => {
        const id = d.decision_id!
        const approve = marks[id] === 'approve'
        return approve
          ? { decision_id: id, status: 'approved' as const }
          : {
              decision_id: id,
              status: 'rejected' as const,
              reject_reason_code: 'owner_reject',
              reject_reason: reasons[id] || '',
            }
      })
    if (!items.length) return
    setBusy(true)
    setMsg(null)
    try {
      await post('/approve/batch', { items })
      setMsg(`${items.length} 条已一次提交`)
      load()
      onRefreshFresh?.()
    } catch (e: any) {
      setMsg(`批量提交失败（整批回滚，一条都没写）：${e.message}`)
    } finally {
      setBusy(false)
    }
  }

  async function markExecuted(d: Decision, target?: string | number | null) {
    if (d.decision_id == null) return
    setBusy(true)
    setMsg(null)
    try {
      const r = await post<{ ok: boolean; detail: any }>('/approve/executed', {
        decision_id: d.decision_id,
        executed_value: String(target ?? d.proposed_value ?? ''),
        executed_by: 'owner',
      })
      const done = r.detail?.exec_orders?.[0]
      if (done) {
        setOrder((prev: any) => {
          if (!prev || prev.order?.order_id !== done.order_id) return prev
          const items = (prev.items || []).map((it: any) =>
            it.decision_id === d.decision_id && !it.executed_at
              ? { ...it, executed_at: new Date().toISOString() }
              : it,
          )
          return { ...prev, items }
        })
      }
      setMsg(
        done
          ? `#${d.decision_id} 已标已改 · 执行单 ${done.status === 'done' ? '已做完' : `${done.executed}/${done.total}`}`
          : `#${d.decision_id} 已标已改`,
      )
      load()
      loadOrder()
      onRefreshFresh?.()
    } catch (e: any) {
      setMsg(`#${d.decision_id} 标已改失败：${e.message}`)
    } finally {
      setBusy(false)
    }
  }

  async function submitOverride(d: Decision, reason: string) {
    if (d.decision_id == null) return
    setBusy(true)
    setMsg(null)
    try {
      await post('/approve/decision', {
        decision_id: d.decision_id,
        status: 'approved',
        override: true,
        override_reason: reason,
      })
      setMsg(`#${d.decision_id} 已强制批准`)
      load()
      onRefreshFresh?.()
    } catch (e: any) {
      setMsg(`#${d.decision_id} 强制批准失败：${e.message}`)
    } finally {
      setBusy(false)
    }
  }

  async function submitQuestion(q: Question, skipped = false) {
    if (!mem) return
    const k = qkeyOf(q)
    const sel = answers[k] || []
    const cross = crossed[k] || []
    setBusy(true)
    setMsg(null)
    try {
      const r = await post<{ ok: boolean; detail: any }>('/approve/question', {
        question_id: k,
        run_id: mem.run_id,
        object_id: q.object,
        selected: sel,
        excluded: cross,
        free_text: freeText[k] || '',
        skipped,
      })
      const sed = (r?.detail?.sediment || []) as any[]
      const went = sed
        .map((x) => DEST_LABEL[x.table] || x.table)
        .filter(Boolean)
      if (skipped) setMsg(`${q.id} 已跳过：按默认假设执行，14 天后复核`)
      else if (!sel.length && !cross.length) setMsg(`${q.id} 没有表态，未写任何沉淀`)
      else setMsg(`${q.id} 已记录${went.length ? '：' + went.join(' / ') : ''}`)
    } catch (e: any) {
      setMsg(`${q.id} 失败：${e.message}`)
    } finally {
      setBusy(false)
    }
  }

  /**
   * 点击「生成执行单」时，先自动提交本地刚勾选但尚未提交的「批」条目，
   * 再创建持久化执行单，避免因未先点提交而导致执行单创建失败。
   */
  async function makeOrder(allApproved: Decision[]) {
    const ids = allApproved.map((d) => d.decision_id!).filter(Boolean)
    if (!ids.length) return
    setBusy(true)
    setMsg(null)
    try {
      const needSubmit = allApproved.filter(
        (d) => d.decision_id != null && d.status !== 'approved',
      )
      if (needSubmit.length > 0) {
        await post('/approve/batch', {
          items: needSubmit.map((d) => ({
            decision_id: d.decision_id!,
            status: 'approved' as const,
          })),
        })
      }
      const r = await post<{ ok: boolean; detail: any }>('/approve/exec-order', {
        decision_ids: ids,
        run_id: mem?.run_id,
      })
      setOrder(r.detail)
      setMsg(
        needSubmit.length > 0
          ? `已自动提交 ${needSubmit.length} 条并生成执行单：${r.detail.ticket_id}`
          : `执行单已生成：${r.detail.ticket_id}`,
      )
      load()
      onRefreshFresh?.()
    } catch (e: any) {
      if (String(e.message || '').includes('已经在执行单')) {
        loadOrder()
        setMsg(`这批里有条目已在别的执行单上：${e.message}`)
      } else {
        setMsg(`执行单失败：${e.message}`)
      }
    } finally {
      setBusy(false)
    }
  }

  if (err) return <div className="err">打不开奏折：{err}</div>
  if (!mem) return <div className="note">读取中…</div>

  const pack = mem.pack
  const who = pack.who_manages ?? []
  const alerts = pack.alerts ?? []
  const suppressed = pack.suppressed ?? []
  const approved = decisions.filter(
    (d) => d.decision_id != null && marks[d.decision_id] === 'approve',
  )
  const abnormalConflicts = pack.conflicts.reduce(
    (acc, c) =>
      acc + c.campaigns.filter((cp) => cp.flag === 'mismatch' || cp.flag === 'near').length,
    0,
  )
  const hasDiagWarn =
    Boolean(freshness?.stale_any) || abnormalConflicts > 0 || mem.agent?.state === 'failed'

  return (
    <div className="cols">
      <div>
        <ConclusionCard pack={pack} runId={mem.run_id} who={who} alerts={alerts} />

        <PolicyList signal={signal} hideWhenEmpty onChanged={onRefreshFresh} />

        {decisions.length === 0 ? (
          <div className="card tight row">
            <b style={{ fontSize: 13, color: 'var(--ink)' }}>待批 0 条</b>
            <span className="small muted">今日暂无达到判定线的建议</span>
          </div>
        ) : (
          <div className="card">
            <h2>
              待批
              <span className="sub">
                {pendingCount === 0 ? '今天没有待批建议' : `${pendingCount} 条待决定`}
              </span>
              {unsubmitted > 0 && (
                <button
                  className="btn primary"
                  disabled={busy}
                  style={{ marginLeft: 'auto' }}
                  title="一次提交所有打好的钩；后端整批一个事务，任一条失败全回滚"
                  onClick={submitAll}
                >
                  提交全部（{unsubmitted}）
                </button>
              )}
            </h2>
            {decisions.map((d) => (
              <DecisionRow
                key={`${d.rule_id}-${d.entity_id}-${d.action_type}`}
                d={d}
                mark={d.decision_id != null ? marks[d.decision_id] : undefined}
                reason={d.decision_id != null ? reasons[d.decision_id] || '' : ''}
                busy={busy}
                onMark={(id, verdict) => setMarks({ ...marks, [id]: verdict })}
                onReason={(id, text) => setReasons({ ...reasons, [id]: text })}
                onSubmit={submitDecision}
                onExecuted={markExecuted}
              />
            ))}
          </div>
        )}

        <QuestionsCard
          questions={pack.questions}
          answers={answers}
          crossed={crossed}
          freeText={freeText}
          setAnswers={setAnswers}
          setCrossed={setCrossed}
          setFreeText={setFreeText}
          busy={busy}
          onSubmit={submitQuestion}
        />

        <details className="disclosure">
          <summary>
            {hasDiagWarn && <span className="disclosure-dot" title="存在数据变旧或冲突异常" />}
            <span>展开诊断与证据</span>
            <span className="disclosure-sub">
              观测 {observing.length} · 被拦下 {suppressed.length} · 冲突 {pack.conflicts.length}
              {abnormalConflicts > 0 ? `（异常 ${abnormalConflicts}）` : ''}
              {freshness?.stale_any ? ' · 数据变旧' : ''}
            </span>
            <span className="disclosure-right">默认收起</span>
          </summary>
          <div className="drawer-box">
            <ObservingCard
              observing={observing}
              notes={pack.notes ?? []}
              busy={busy}
              onOverride={submitOverride}
              onExecuted={markExecuted}
            />
            <SuppressedCard suppressed={suppressed} />
            <AgentCard agent={mem.agent} />
            <SideCards pack={pack} freshness={freshness} />
          </div>
        </details>
      </div>

      <div className="sticky-rail">
        <ExecOrderCard
          approved={approved}
          order={order}
          decisions={decisions}
          busy={busy}
          msg={msg}
          orderErr={orderErr}
          onMakeOrder={makeOrder}
          onExecuted={markExecuted}
        />
      </div>
    </div>
  )
}
