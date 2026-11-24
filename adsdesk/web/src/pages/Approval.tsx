/** 审批台：一屏回答「今天动哪笔钱、为什么、我批了没」。
 *
 *  这个文件原来有 1213 行，五个组件挤在一起，改一处要滚三屏。现在它只负责
 *  状态与请求编排，具体渲染分到 ./approval/ 下：
 *    ConclusionCard 今日结论 / ObservingCard 证据不够 / DecisionRow 待批一条
 *    SuppressedCard 被拦下 / AgentCard 诊断 agent / QuestionsCard 待答问题
 *    ExecOrderCard 执行单 / SideCards 冲突·证据·新鲜度
 *  shared.tsx 放常量和人话映射 —— 它们被 5 个组件共用，留在这里会形成循环依赖。
 */
import { useEffect, useMemo, useState } from 'react'
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
import { DEST_LABEL, type Mark } from './approval/shared'

export default function Approval({
  freshness,
  signal,
}: {
  freshness: Freshness | null
  /** App 的轮询计数器：页面常驻挂载，靠它知道「该重拉账本了」 */
  signal?: number
}) {
  const [mem, setMem] = useState<MemorialToday | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [marks, setMarks] = useState<Mark>({})
  const [reasons, setReasons] = useState<Record<string, string>>({})
  const [answers, setAnswers] = useState<Record<string, number[]>>({})
  // 「划掉」与「没勾」是两件事：没勾＝今天没表态（什么都不写），
  // 划掉＝这个方向不要（写 reason_excluded，90 天内不再问）。
  const [crossed, setCrossed] = useState<Record<string, number[]>>({})
  const [freeText, setFreeText] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const [order, setOrder] = useState<any>(null)
  // agent 的问题单独存：它不进 pack.questions（影子模式），也不该混进人的审批流。
  const [agentAns, setAgentAns] = useState<Record<string, number[]>>({})

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

  useEffect(load, [])

  // App 每 60 秒轮询一次新鲜度，每次变化都把 signal 加一。页面是常驻挂载的
  // （切 Tab 不卸载），没有这一句，切回来看到的还是挂载那一刻的账本。
  useEffect(() => {
    if (signal) load()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [signal])

  const decisions: Decision[] = useMemo(() => {
    if (!mem) return []
    // pack.front 是给人看的顺序，库里 decisions 带有最新审批状态。
    const byId = new Map<number, Decision>()
    for (const d of mem.decisions) if (d.decision_id != null) byId.set(d.decision_id, d)
    return mem.pack.front.map((d) => (d.decision_id != null && byId.get(d.decision_id)) || d)
  }, [mem])

  /**
   * 观察区同理：pack 里带的 status 是引擎写奏折那一刻的快照（永远是 observing），
   * 真正的状态（你强制批过没有、改掉没有）只存在库里。不覆盖的话，强制批准完
   * 页面还是那条「强制批准」按钮，人会以为没点成功，反复点。
   */
  const observing: Decision[] = useMemo(() => {
    if (!mem) return []
    const byId = new Map<number, Decision>()
    for (const d of mem.decisions) if (d.decision_id != null) byId.set(d.decision_id, d)
    return (mem.pack.observing ?? []).map(
      (d) => (d.decision_id != null && byId.get(d.decision_id)) || d,
    )
  }, [mem])

  const pendingCount = decisions.filter((d) => d.decision_id != null && !marks[d.decision_id]).length

  /** 打了钩但还没提交的条数（和 pendingCount 是两件事：那个是「还没表态」）。 */
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
    } catch (e: any) {
      setMsg(`#${d.decision_id} 失败：${e.message}`)
    } finally {
      setBusy(false)
    }
  }

  /**
   * 一次提交所有打好的钩。
   *
   * 为什么要有这条：今天 26 条待批时，逐条点「提交」是 26 次往返，
   * 手快一点就会漏一条 —— 而漏掉的那条既不在待批里（已打钩）、
   * 也不在执行单里（没批），第二天看账本会当成引擎没算出来。
   *
   * 只提交「标记与库里状态不一致」的：load() 已经把 approved/rejected 预填进
   * marks，直接全量提交会把这些条目再写一遍，白刷 checked_at。
   * 后端 /approve/batch 是整批一个事务，任一条失败全回滚 —— 半批状态最难查。
   */
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
    } catch (e: any) {
      // 整批回滚，所以这里不能说「部分成功」—— 一条都没落库。
      setMsg(`批量提交失败（整批回滚，一条都没写）：${e.message}`)
    } finally {
      setBusy(false)
    }
  }

  /**
   * 标「已改」= 你已经在广告后台把这条改掉了。
   * 归因窗从这一刻开始算，不是从打钩那一刻 —— 打钩只是记台账，
   * 批了没改是常态，从打钩起算会把「没改」混进「改了没效果」。
   */
  async function markExecuted(d: Decision, target?: string | number | null) {
    if (d.decision_id == null) return
    setBusy(true)
    setMsg(null)
    try {
      await post('/approve/executed', {
        decision_id: d.decision_id,
        executed_value: String(target ?? d.proposed_value ?? ''),
        executed_by: 'owner',
      })
      setMsg(`#${d.decision_id} 已标已改，归因窗开始`)
      load()
    } catch (e: any) {
      setMsg(`#${d.decision_id} 标已改失败：${e.message}`)
    } finally {
      setBusy(false)
    }
  }

  /**
   * 强制批准：引擎说「证据不够」，但你看完原因决定现在就动。
   *
   * 这不是给引擎开后门 —— 引擎的判断原样留在 gate_reason 里，账本同时记下
   * override_by / override_reason / overridden_at。三个月后复盘时，
   * 「这条当时是等证据，还是人拍的板」必须一眼能分出来。
   * 理由必填，后端也卡 8 字下限。
   */
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
      setMsg(`#${d.decision_id} 已强制批准（证据不够，你拍了板）`)
      load()
    } catch (e: any) {
      setMsg(`#${d.decision_id} 强制批准失败：${e.message}`)
    } finally {
      setBusy(false)
    }
  }

  async function submitQuestion(q: Question, skipped = false) {
    if (!mem) return
    const sel = answers[q.id] || []
    const cross = crossed[q.id] || []
    setBusy(true)
    setMsg(null)
    try {
      const r = await post<{ ok: boolean; detail: any }>('/approve/question', {
        question_id: q.id,
        run_id: mem.run_id,
        object_id: q.object,
        selected: sel,
        excluded: cross,
        free_text: freeText[q.id] || '',
        // 只有明确点了「跳过」才算跳过。没勾没划就提交＝没表态，
        // 一个字都不写（不能拿「没选」冒充「授权默认假设」）。
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

  async function makeOrder(allApproved: Decision[]) {
    const ids = allApproved.map((d) => d.decision_id!).filter(Boolean)
    if (!ids.length) return
    setBusy(true)
    try {
      const r = await post<{ ok: boolean; detail: any }>('/approve/exec-order', {
        decision_ids: ids,
        run_id: mem?.run_id,
      })
      setOrder(r.detail)
      setMsg(`执行单已生成：${r.detail.ticket_id}`)
    } catch (e: any) {
      setMsg(`执行单失败：${e.message}`)
    } finally {
      setBusy(false)
    }
  }

  if (err) return <div className="err">打不开奏折：{err}</div>
  if (!mem) return <div className="note">读取中…</div>

  const pack = mem.pack
  const who = pack.who_manages ?? []
  const alerts = pack.alerts ?? []
  const approved = decisions.filter((d) => d.decision_id != null && marks[d.decision_id] === 'approve')

  return (
    <div className="cols">
      <div>
        <ConclusionCard pack={pack} runId={mem.run_id} who={who} alerts={alerts} />

        {/* 引擎算出来的动作紧跟在「今日结论」下面。
            以前它排在「待批」之后、和 suppressed 挨着，于是「待批 0 条」时整页像
            「今天没事可做」—— 可引擎其实算出了带目标价的动作，只是证据不够。
            这些是「等你拍板的东西」，不是「被否掉的东西」，位置必须说清这件事。 */}
        <ObservingCard
          observing={observing}
          notes={pack.notes ?? []}
          busy={busy}
          onOverride={submitOverride}
          onExecuted={markExecuted}
        />

        <div className="card">
          <h2>
            待批
            <span className="sub">
              {pendingCount === 0 ? '今天没有待批的' : `${pendingCount} 条还没决定`}
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
          {decisions.length === 0 && (
            <div className="note">
              今天没有达到判定线的动作。规则跑了，产出在上面的「引擎算出了动作，但证据不够」
              和下面的「被拦下」两张卡里：
              {(pack.observing?.length ?? 0) > 0 &&
                ` 证据不够 ${pack.observing?.length} 条（有目标价、等你拍板），`}
              {(pack.suppressed?.length ?? 0) > 0 && ` 被拦下 ${pack.suppressed?.length} 条（各有原因）。`}
              {decisions.length === 0 &&
                (pack.suppressed?.length ?? 0) === 0 &&
                (pack.observing?.length ?? 0) === 0 &&
                ' 连被拦下的也没有 —— 今天确实干净。'}
            </div>
          )}
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

        <SuppressedCard suppressed={pack.suppressed ?? []} />

        <AgentCard
          agent={mem.agent}
          answers={agentAns}
          onToggle={(qid, i) => {
            const sel = agentAns[qid] || []
            const next = sel.includes(i) ? sel.filter((x) => x !== i) : [...sel, i]
            setAgentAns({ ...agentAns, [qid]: next })
          }}
        />

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
      </div>

      {/* 右栏粘性：批了十几条之后要往下滚着抄数字，执行单跟着走才不会来回滚 */}
      <div className="sticky-rail">
        <ExecOrderCard
          approved={approved}
          order={order}
          decisions={decisions}
          busy={busy}
          msg={msg}
          onMakeOrder={makeOrder}
          onExecuted={markExecuted}
        />
        <SideCards pack={pack} freshness={freshness} />
      </div>
    </div>
  )
}
