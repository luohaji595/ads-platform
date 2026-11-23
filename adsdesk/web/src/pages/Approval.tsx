import { useEffect, useMemo, useState } from 'react'
import {
  get,
  post,
  type Decision,
  type Freshness,
  type AgentView,
  type MemorialToday,
  type Question,
  type Suppressed,
} from '../lib/api'
import { fmtMoney, fmtPct } from '../lib/charts'

/** 报告正文是 markdown，网页里只把 **粗体** 认出来，别把星号露给用户看。 */
function Rich({ text }: { text: string }) {
  const parts = String(text).split(/\*\*(.+?)\*\*/g)
  return (
    <>
      {parts.map((part, i) =>
        i % 2 === 1 ? <b key={i}>{part}</b> : <span key={i}>{part}</span>,
      )}
    </>
  )
}

/**
 * 四类去向的人话名。engine/reason_routes.py 的 destination() 给的是表名，
 * 这里只做翻译 —— 不要在别处再写一套映射，否则改了词表两头对不上。
 */
const DEST_LABEL: Record<string, string> = {
  policy: '记住这条（策略）',
  param_override: '调参数',
  observation_task: '排观察任务',
  experiment: '开实验',
  reason_hit: '只留痕',
}

const CONF_LABEL: Record<string, string> = {
  high: '证据够',
  medium: '证据一般',
  low: '证据不足',
}

/**
 * 证据标签。跟 engine/report.py 的 `_evidence_label` 同一个口径 ——
 * 报告和网页说两套话，主人会以为是两件事。
 *
 * R32（止损线）的 confidence 必然是 low：它明说了「统计上判不了」，
 * 依据是「已花掉几单的钱」。直接印「证据不足」正好把它的依据说反了。
 */
function evidenceLabel(d: Decision): string {
  const sig: any = d.significance || {}
  if (sig.basis === 'cost_ceiling') {
    const units = sig.cost_evidence?.units
    return units != null ? `止损线（已花 ${Number(units).toFixed(2)} 单的钱）` : '止损线'
  }
  return CONF_LABEL[d.confidence] || d.confidence
}

/**
 * 动作枚举的人话名。
 *
 * 引擎在「不是改一个数」的动作上会把枚举直接写进 proposed_value：
 * pause_or_restructure / consider_new_ad_group / pause / increase / reduce。
 * 原来前端一路纯文本插值，主人看到的是 `0.8811 → pause_or_restructure` ——
 * 意思是「这个价位的词建议停掉或重构」，但读的人得先猜这个词是什么意思。
 *
 * 只翻译「整个值就是这个枚举」的情况；像「20%（从 0% 提）」这种已经把话说清了的
 * 中文串原样放行，不去动它 —— 翻译的边界是「英文枚举 → 中文」，不是「改写引擎的话」。
 */
const ACTION_TEXT: Record<string, string> = {
  pause: '停用',
  pause_or_restructure: '停用或重构（否词 / 换匹配方式）',
  consider_new_ad_group: '考虑拆到独立广告组',
  increase: '提高',
  reduce: '降低',
}

function showValue(v: string | number | null | undefined): string {
  if (v == null || v === '') return '—'
  const s = String(v)
  return ACTION_TEXT[s] || s
}

type Mark = Record<string, 'approve' | 'reject'>

export default function Approval({ freshness }: { freshness: Freshness | null }) {
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
        <div className="card">
          <h2>
            今日结论
            <span className="sub">
              数据窗口 {pack.window_start} ~ {pack.window_end} · run #{mem.run_id}
            </span>
          </h2>
          <div className="row" style={{ marginBottom: 8 }}>
            <span className="tag">建议 {pack.front.length} 条</span>
            {(pack.observing?.length ?? 0) > 0 && (
              <span className="tag near">证据不够、等你定 {pack.observing?.length}</span>
            )}
            {(pack.suppressed?.length ?? 0) > 0 && (
              <span className="tag">被拦下 {pack.suppressed?.length}</span>
            )}
            {pack.overflow_count > 0 && <span className="tag">溢出 {pack.overflow_count}</span>}
            {pack.below_count > 0 && <span className="tag">不够线 {pack.below_count}</span>}
            <span className="tag">可省 {fmtMoney(pack.saved)}</span>
            <span className="tag">多花 {fmtMoney(pack.spent)}</span>
            {!pack.target_reachable && <span className="tag over">目标不可达</span>}
          </div>
          {pack.portfolio_headline && (
            <div className="warn"><Rich text={pack.portfolio_headline} /></div>
          )}
          {who.length > 0 && (
            <>
              <div className="spacer" />
              <div className="sub" style={{ marginBottom: 6 }}>
                今天最贵的三笔钱，各自谁在管
              </div>
              <table className="tbl">
                <tbody>
                  {who.map((w) => (
                    <tr key={w.campaign_id}>
                      <td title={w.campaign}>{w.campaign.slice(0, 30)}</td>
                      <td className="num">{fmtMoney(w.cost)}</td>
                      <td className="num">{w.orders} 单</td>
                      <td>
                        <span className={`tag ${w.kind === '被锁' || w.kind === '无人管' ? 'over' : w.kind === '有动作' ? 'ok' : ''}`}>
                          {w.how}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {who.every((w) => w.kind !== '有动作') && (
                <div className="note">
                  这三笔钱今天一条可执行动作都没有。规则各自都有理由，但叠起来的结果是最大的花费没人动 ——
                  「今天没动作」不等于「这些钱没问题」。
                </div>
              )}
            </>
          )}
          {alerts.length > 0 && (
            <>
              <div className="spacer" />
              <div className="sub" style={{ marginBottom: 6 }}>
                账户级告警 · 只留痕，不可审批
              </div>
              {alerts.map((a) => (
                <div key={`${a.rule_id}-${a.entity_id}`} className="warn" style={{ marginBottom: 8 }}>
                  <div className="row" style={{ marginBottom: 4 }}>
                    <span className="tag">{a.rule_id}</span>
                    <span className="small muted">{a.entity_label}</span>
                  </div>
                  <Rich text={a.rationale ?? ''} />
                </div>
              ))}
            </>
          )}
          <div className="spacer" />
          <div className="note">
            打钩只记台账，不会替你改广告后台。改完回来点「已改」，归因窗从那一刻开始算。
          </div>
        </div>

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
            <span className="sub">{pendingCount === 0 ? '今天没有待批的' : `${pendingCount} 条还没决定`}</span>
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
          {decisions.map((d) => {
            const id = d.decision_id
            const m = id != null ? marks[id] : undefined
            return (
              <div key={`${d.rule_id}-${d.entity_id}-${d.action_type}`} style={{ borderTop: '1px solid var(--line)', paddingTop: 10, marginTop: 10 }}>
                <div className="row">
                  <b>{d.entity_label}</b>
                  <span className="tag">{d.rule_id}</span>
                  <span className="tag">{d.action_label || d.action_type}</span>
                  <span className={`tag ${d.confidence}`}>{evidenceLabel(d)}</span>
                  {d.spend_impact != null && (
                    <span className="small muted mono">影响 {fmtMoney(d.spend_impact)}</span>
                  )}
                  {d.status && d.status !== 'proposed' && <span className="tag ok">{d.status}</span>}
                </div>
                <div className="small" style={{ margin: '6px 0' }}>
                  {showValue(d.current_value)} → <b>{showValue(d.proposed_value)}</b>
                </div>
                <div className="small muted">
                  <Rich text={d.rationale ?? ''} />
                </div>
                {(d.rule_record || d.object_record) && (
                  <div className="small muted" style={{ marginTop: 4 }}>
                    {d.rule_record && <div>· {d.rule_record}</div>}
                    {d.object_record && <div>· {d.object_record}</div>}
                  </div>
                )}
                {d.objections && d.objections.length > 0 && (
                  <div className="warn" style={{ marginTop: 6 }}>
                    agent 反对：{d.objections.map((o) => o.why).join('；')}
                  </div>
                )}
                <div className="row" style={{ marginTop: 8 }}>
                  <button
                    className={`btn ${m === 'approve' ? 'primary' : ''}`}
                    disabled={busy}
                    onClick={() => id != null && setMarks({ ...marks, [id]: 'approve' })}
                  >
                    批
                  </button>
                  <button
                    className={`btn ${m === 'reject' ? 'danger' : ''}`}
                    disabled={busy}
                    onClick={() => id != null && setMarks({ ...marks, [id]: 'reject' })}
                  >
                    不批
                  </button>
                  <button className="btn" disabled={busy || !m} onClick={() => submitDecision(d)}>
                    提交
                  </button>
                  {d.status === 'approved' && (
                    <button className="btn primary" disabled={busy} onClick={() => markExecuted(d)}>
                      已改
                    </button>
                  )}
                  {d.status === 'executed' && (
                    <span className="tag ok">
                      已改 · 归因至 {d.attribution_due_at ? d.attribution_due_at.slice(0, 10) : '—'}
                    </span>
                  )}
                  {m === 'reject' && (
                    <input
                      type="text"
                      placeholder="为什么不批（可选，会进 reason_hit）"
                      value={id != null ? reasons[id] || '' : ''}
                      onChange={(e) => id != null && setReasons({ ...reasons, [id]: e.target.value })}
                      style={{ flex: 1, minWidth: 200 }}
                    />
                  )}
                </div>
              </div>
            )
          })}
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

        <div className="card">
          <h2>
            需要你判断的问题
            <span className="sub">打钩就行，不问开放题</span>
          </h2>
          {pack.questions.length === 0 && <div className="note">今天没有问题。</div>}
          {pack.questions.map((q) => {
            const sel = answers[q.id] || []
            const cross = crossed[q.id] || []
            const metas = q.option_meta || []
            return (
              <div key={q.id} style={{ borderTop: '1px solid var(--line)', paddingTop: 10, marginTop: 10 }}>
                <div className="row">
                  <span className="tag">{q.id}</span>
                  <b>{q.object}</b>
                </div>
                <div className="small" style={{ margin: '6px 0' }}>{q.ask}</div>
                <div>
                  {q.options.map((opt, i) => {
                    const m = metas[i] || null
                    const dest = m?.destination
                    const done = Boolean(m?.settled_table)
                    const dead = Boolean(m?.excluded) || cross.includes(i)
                    const tag = done
                      ? `已在生效（到 ${m?.settled_until || '未设'}）`
                      : dead
                        ? `已划掉（到 ${m?.exclude_until || '90 天后'}）`
                        : dest
                          ? DEST_LABEL[dest] || dest
                          : ''
                    return (
                      <div key={i} className="row" style={{ padding: '2px 0', opacity: done ? 0.55 : 1 }}>
                        <label className="small" style={{ flex: 1 }}>
                          <input
                            type="checkbox"
                            checked={sel.includes(i)}
                            onChange={() => {
                              const next = sel.includes(i) ? sel.filter((x) => x !== i) : [...sel, i]
                              // 勾了就不能同时算划掉：两种表态互相矛盾
                              setAnswers({ ...answers, [q.id]: next })
                              if (!sel.includes(i)) {
                                const c = (crossed[q.id] || []).filter((x) => x !== i)
                                setCrossed({ ...crossed, [q.id]: c })
                              }
                            }}
                          />{' '}
                          {opt}
                          {tag && <span className="small muted"> · {tag}</span>}
                        </label>
                        <button
                          className="small"
                          style={{ background: 'none', border: 'none', cursor: 'pointer', opacity: 0.7 }}
                          title="这个方向以后别再问（90 天内）"
                          onClick={() => {
                            const c = cross.includes(i) ? cross.filter((x) => x !== i) : [...cross, i]
                            setCrossed({ ...crossed, [q.id]: c })
                            // 划掉的同时取消勾选，避免「又要又不要」
                            setAnswers({ ...answers, [q.id]: sel.filter((x) => x !== i) })
                          }}
                        >
                          {cross.includes(i) ? '撤销划掉' : '划掉'}
                        </button>
                      </div>
                    )
                  })}
                </div>
                <div className="row" style={{ marginTop: 6 }}>
                  <input
                    type="text"
                    placeholder="补一句依据（可选，写数字会被读成参数）"
                    value={freeText[q.id] || ''}
                    onChange={(e) => setFreeText({ ...freeText, [q.id]: e.target.value })}
                    style={{ flex: 1 }}
                  />
                  <button className="btn" disabled={busy} onClick={() => submitQuestion(q, true)}>
                    跳过
                  </button>
                  <button className="btn primary" disabled={busy} onClick={() => submitQuestion(q)}>
                    提交
                  </button>
                </div>
              </div>
            )
          })}
        </div>
      </div>

      <div>
        <div className="card">
          <h2>
            执行单
            <span className="sub">改后台时照着抄</span>
          </h2>
          {approved.length === 0 ? (
            <div className="note">还没有批的动作。</div>
          ) : (
            <>
              <div className="small muted" style={{ marginBottom: 6 }}>{approved.length} 条</div>
              {approved.map((d) => (
                <div key={d.decision_id} className="small" style={{ padding: '3px 0' }}>
                  {d.entity_label}：{showValue(d.current_value)} → <b>{showValue(d.proposed_value)}</b>
                </div>
              ))}
              <div className="spacer" />
              <button className="btn primary" disabled={busy} onClick={() => makeOrder(approved)}>
                生成执行单
              </button>
              {order && (
                <div style={{ marginTop: 10 }}>
                  <div className="small muted">执行单 {order.order?.ticket_id}</div>
                  {(order.items || []).map((it: any) => (
                    <div key={it.item_id} className="small" style={{ padding: '4px 0', borderTop: '1px solid var(--line)' }}>
                      <div>
                        {it.campaign_name || it.entity_label}：{it.current_value ?? '—'} →{' '}
                        <b>{it.target_value ?? '—'}</b>
                      </div>
                      <div className="row" style={{ marginTop: 4 }}>
                        {it.executed_at ? (
                          <span className="tag ok">已改 {it.executed_at.slice(0, 10)}</span>
                        ) : (
                          <button
                            className="btn primary"
                            disabled={busy}
                            onClick={() => {
                              const dec = decisions.find((x) => x.decision_id === it.decision_id)
                              if (dec) markExecuted(dec, it.target_value)
                            }}
                          >
                            已改
                          </button>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </>
          )}
          {msg && <div className="note" style={{ marginTop: 8 }}>{msg}</div>}
        </div>

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
      </div>
    </div>
  )
}

/** 引擎算出了具体动作，但证据不够线 —— 要不要照样做，由人拍板。
 *
 * 为什么单列一张卡、而且必须紧跟「今日结论」：以前「待批 0 条」就等于整页空白，
 * 看着像系统没跑。其实引擎是跑了的：它算出了带目标价的动作（比如 0.4 → 0.32），
 * 只是判定线要求 901 击、窗口里只有 63 击，数学上还不敢拍。
 * 这类条目以前排在「待批」之后、和 suppressed 挨着，视觉上像「被否掉的东西」；
 * 它实际是「等你拍板的东西」，所以提到结论正下方（比 suppressed、比问题卡都高）。
 * 引擎自己把这两档分开了（run.py 的 gate_reason：人按住 / 数学按住），
 * 网页用同一口径，不然会把「你自己决定先看着」说成「没有规则管」，那是冤枉规则。
 */

/** 一条落库的 observing 可能没有 gate_reason（早于这个字段的老账本行）。
 *  这时用 significance 里的击数现拼一句「差多少证据」：
 *  留白会让人以为规则没跑，而「看不出差多少」正是这次要修的病。 */
function gateText(d: Decision): string {
  if (d.gate_reason) return d.gate_reason
  const s = d.significance || {}
  const clicks = s.clicks
  const orders = s.orders
  const need = s.min_clicks_needed
  if (clicks != null && need != null) {
    return `证据不够：${clicks} 击${orders != null ? ` ${orders} 单` : ''}；判到判定线需 ${need} 击`
  }
  return '证据不够、未达判定线（这条是旧账本，当时没记下差多少）'
}

/** 置信度的人话。别直接印 low/medium —— 那是给代码看的。 */
const CONF_TEXT: Record<string, string> = {
  high: '置信度高',
  medium: '置信度中',
  low: '置信度低',
}

function ObservingCard({
  observing,
  notes,
  busy,
  onOverride,
  onExecuted,
}: {
  observing: Decision[]
  notes: string[]
  busy: boolean
  onOverride: (d: Decision, reason: string) => void
  onExecuted: (d: Decision, target?: string | number | null) => void
}) {
  // hooks 必须在任何 early return 之前
  const [openId, setOpenId] = useState<number | null>(null)
  const [draft, setDraft] = useState('')
  if (observing.length === 0) return null

  // 「你按住」（观察期内，本轮不落库、不可操作）与「数学按住」（证据不够，可强制批准）
  // 是两件事：前者是你已经拍过板了，后者是引擎在等样本。混在一起会让人以为
  // 观察期内的东西还能再批一次。
  const isHeld = (d: Decision) =>
    d.held === true || (d.rationale || '').includes('已批先观察')
  const held = observing.filter(isHeld)
  const weak = observing.filter((d) => !isHeld(d))

  return (
    <div className="card">
      <h2>
        引擎算出了动作，但证据不够
        <span className="sub">
          要不要照样做，由你定 · {observing.length} 条
          {held.length > 0 && ` · ${held.length} 条是你在观察期`}
          {weak.length > 0 && ` · ${weak.length} 条是数学按住`}
        </span>
      </h2>
      <div className="note">
        这些不是「没发现问题」，是引擎已经算出了目标价、只是样本还没到判定线 ——
        每条都写着「差多少证据」（现在多少击、需要多少击）。
        等样本攒够它自己会升进待批；你判断等不起，也可以现在拍板：
        强制批准理由必填，账本里会分清「等到证据」和「人拍的板」。
      </div>

      {held.length > 0 && (
        <>
          <div className="spacer" />
          <div className="small" style={{ marginBottom: 4 }}>
            <b>你按住</b>（批过「先观察」，观察期内不再进待批）
          </div>
          <table className="tbl">
            <tbody>
              {held.map((d) => (
                <tr key={`lk-${d.rule_id}-${d.entity_id}`}>
                  <td title={d.entity_label}>{d.entity_label.slice(0, 26)}</td>
                  <td>
                    <span className="tag">{d.rule_id}</span>
                  </td>
                  <td className="num">
                    {showValue(d.current_value)} → {showValue(d.proposed_value)}
                  </td>
                  <td className="num">{fmtMoney(d.spend_impact)}</td>
                  <td className="small muted">
                    {d.gate_reason ||
                      (d.rationale || '').split('；').filter((s) => s.includes('先观察')).join('；') ||
                      '你在观察期内批过「先观察」，本轮不再作为待批'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}

      {weak.length > 0 && (
        <>
          <div className="spacer" />
          <div className="small" style={{ marginBottom: 4 }}>
            <b>数学按住</b>（置信度不够，不是没人管）
          </div>
          <table className="tbl">
            <tbody>
              {weak.map((d) => {
                const id = d.decision_id
                const st = d.status || 'observing'
                const opened = id != null && openId === id
                return (
                  <tr key={`wk-${d.rule_id}-${d.entity_id}`}>
                    <td title={d.entity_label}>{d.entity_label.slice(0, 26)}</td>
                    <td>
                      <span className="tag">{d.rule_id}</span>
                    </td>
                    <td className="num">
                      {showValue(d.current_value)} → {showValue(d.proposed_value)}
                    </td>
                    <td className="num">{fmtMoney(d.spend_impact)}</td>
                    <td>
                      <span className={`tag ${d.confidence}`}>
                        {CONF_TEXT[d.confidence] || d.confidence}
                      </span>
                    </td>
                    <td className="small muted" style={{ maxWidth: 260 }}>
                      <div>{gateText(d)}</div>
                      {d.object_record && <div style={{ marginTop: 4 }}>· {d.object_record}</div>}
                      {d.rule_record && (
                        <div style={{ marginTop: 4, fontSize: 11, lineHeight: 1.5, opacity: 0.8 }}>
                          · {d.rule_record}
                        </div>
                      )}
                    </td>
                    <td style={{ minWidth: 220 }}>
                      {st === 'executed' && (
                        <span className="tag ok">
                          已改 · 归因至{' '}
                          {d.attribution_due_at ? d.attribution_due_at.slice(0, 10) : '—'}
                        </span>
                      )}
                      {st === 'approved' && (
                        <>
                          <span className="tag ok">已强制批准</span>{' '}
                          <button className="btn primary" disabled={busy} onClick={() => onExecuted(d)}>
                            已改
                          </button>
                          {d.override_reason && (
                            <div className="small muted" style={{ marginTop: 4 }}>
                              你当时的理由：{d.override_reason}
                            </div>
                          )}
                        </>
                      )}
                      {st !== 'approved' && st !== 'executed' && (
                        <>
                          {!opened && (
                            <button
                              className="btn"
                              disabled={busy || id == null}
                              title={id == null ? '这条不在账本里（上一轮的快照），不能批' : '证据不够，但你可以拍板'}
                              onClick={() => {
                                if (id == null) return
                                setOpenId(id)
                                setDraft('')
                              }}
                            >
                              强制批准
                            </button>
                          )}
                          {opened && (
                            <div style={{ marginTop: 4 }}>
                              <input
                                type="text"
                                placeholder="为什么现在就动（至少 8 字，会进 reason_hit）"
                                value={draft}
                                onChange={(e) => setDraft(e.target.value)}
                                style={{ width: '100%' }}
                              />
                              <div className="row" style={{ marginTop: 4 }}>
                                <button
                                  className="btn primary"
                                  disabled={busy || draft.trim().length < 8}
                                  onClick={() => onOverride(d, draft.trim())}
                                >
                                  确认强制批准
                                </button>
                                <button className="btn" disabled={busy} onClick={() => setOpenId(null)}>
                                  取消
                                </button>
                              </div>
                              <div className="small muted" style={{ marginTop: 2 }}>
                                引擎的判断不会改：这条仍然记着「证据不够」，只是加了「你拍的板」。
                              </div>
                            </div>
                          )}
                        </>
                      )}
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </>
      )}

      {notes.length > 0 && (
        <div className="small muted" style={{ marginTop: 8 }}>
          {notes.map((n, i) => (
            <div key={`note-${i}`}>· {n}</div>
          ))}
        </div>
      )}
    </div>
  )
}

/** 被规则自己的保护闸门掐掉的动作 —— 连建议都不算。
 *
 * 这张卡是「今天什么都没有」的直接解药：这一天正文 0 条，可 suppressed 有 19 条，
 * 每条都写着为什么不算。以前这些只写进 md 底稿，网页上一片空白，
 * 人就以为引擎今天什么都没干。
 */
function SuppressedCard({ suppressed }: { suppressed: Suppressed[] }) {
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
function suppressedWhy(s: Suppressed): string {
  const d = s.detail || {}
  const num = (v: any) => (typeof v === 'number' ? v : null)

  switch (s.reason) {
    case '空转': {
      const bid = num(d.bid)
      const computed = num(d.computed)
      return `当前 $${bid?.toFixed(2)}，算出来 $${computed?.toFixed(2)}，差不到一个步长。${d.note || ''}`
    }
    case '花费闸门': {
      const cost = num(d.cost)
      const gate = num(d.gate)
      const gi = d.gate_inputs || {}
      return `花了 ${fmtMoney(cost)}，一单允许的花费上限是 ${fmtMoney(gate)}（客单价 ${fmtMoney(gi.aov)} × 目标 ACOS ${fmtPct(gi.target_acos, 0)}）。还没花到该动的时候。`
    }
    case 'CVR 低于盈亏平衡': {
      return `这个词的 CVR ${fmtPct(num(d.kw_cvr))}，盈亏平衡要 ${fmtPct(num(d.breakeven_cvr))}。加价只会放大亏损，不是放量。`
    }
    case '证据不足': {
      const clicks = num(d.clicks)
      const min = num(d.raise_min_clicks)
      const need = num(d.need_for_orders)
      return `只有 ${clicks} 击（加价下限 ${min} 击），要到 ${need} 击才够判。小样本下 p 值会虚低，不能只看 p。`
    }
    case '活动整体无销售额': {
      return `这个活动整体 ROAS ${num(d.campaign_roas)?.toFixed(2)}，花了 ${fmtMoney(num(d.cost))}，一单没有。活动本身的问题，不是某一个词的问题。`
    }
    case '位次加价已为 0，无法再降': {
      return `位次加价已经是 ${num(d.bid_adjustment)?.toFixed(0)}%，降无可降。${d.note || ''}`
    }
    case '活动整体不如全店平均，不该加价': {
      return `活动 ROAS ${num(d.campaign_roas)?.toFixed(2)} 低于全店 ${num(d.account_roas)?.toFixed(2)}。${d.note || ''}`
    }
    case '占活动花费比例过小': {
      return `只占活动花费 ${fmtPct(num(d.share), 1)}（${fmtMoney(num(d.cost))}），动它影响太小。`
    }
    default:
      return d.note || '（这条没写原因细节）'
  }
}

/** 诊断 agent 的输出。
 *
 * 影子模式：它提的问题不进 `pack.questions`、不占审批流，只在这里给人看。
 * 一周后拿 agent_eval 的兑现率决定要不要让它真的挡建议。
 */
function AgentCard({
  agent,
  answers,
  onToggle,
}: {
  agent: AgentView | undefined
  answers: Record<string, number[]>
  onToggle: (qid: string, i: number) => void
}) {
  if (!agent) return null

  // 可验证率：到期了却一条都判不出来，说明断言写成了没法验的形式 ——
  // 那和「答错」是两件事，但它同样说明现在还不能拿它的话当依据。
  // 分母 < 5 时不算，1 条判不了就喊「可验证率 0%」是在拿噪声吓人。
  const sc = agent.score
  const scTouched = sc ? (sc.verifiable ?? sc.judged + sc.unverifiable) : 0
  const scLowVerifiable = scTouched >= 5 && (sc?.verifiable_rate ?? 1) < 0.6

  if (agent.state === 'never_ran') {
    return (
      <div className="card">
        <h2>
          诊断 agent
          <span className="sub">今天没跑</span>
        </h2>
        <div className="note">
          跑一次 `python3 adsdesk/agents/diagnose.py` 就会有。它不影响上面的奏折。
        </div>
      </div>
    )
  }

  if (agent.state === 'running') {
    return (
      <div className="card">
        <h2>
          诊断 agent
          <span className="sub">正在跑</span>
        </h2>
        <div className="note">
          从 {agent.started_at?.slice(11, 19)} 开始，还没返回。正常一次 2–4 分钟；
          超过 30 分钟会被判为进程已死。规则引擎的奏折不受影响。
        </div>
      </div>
    )
  }

  if (agent.state === 'failed') {
    return (
      <div className="card">
        <h2>
          诊断 agent
          <span className="sub">跑失败了</span>
        </h2>
        <div className="err">{agent.error || '没拿到错误信息'}</div>
        <div className="note" style={{ marginTop: 6 }}>
          规则引擎的奏折照常，agent 只是附加价值。上面那些建议不受影响。
        </div>
      </div>
    )
  }

  const qs = agent.questions || []
  const obs = agent.objections || []
  const evals = agent.evals || []

  return (
    <div className="card">
      <h2>
        诊断 agent
        <span className="sub">
          {agent.model} · {qs.length} 个疑问 · {obs.length} 条反对 · {evals.length} 条待验证
          {agent.score && agent.score.judged > 0 && (
            <> · 兑现 {agent.score.correct}/{agent.score.judged}</>
          )}
        </span>
      </h2>
      <div className="note">
        影子模式：它提的问题不进上面的待批，也不占审批流。只在这里给你看。
        {agent.fallback && (
          <>
            {' '}
            <b>
              这条评的是 run #{agent.based_on_run} 的输入
            </b>
            ，不是现在这一版 —— 引擎今天重跑过，agent 还没在最新那版上跑。
          </>
        )}
      </div>

      {agent.score && (
        <div className="small muted" style={{ marginTop: 6 }}>
          {agent.score.judged === 0 ? (
            <>
              它过去的断言还没有一条到判定日：{agent.score.pending} 条在等
              {agent.score.unverifiable > 0 ? `，${agent.score.unverifiable} 条到期却判不了` : ''}
              。
              {scLowVerifiable
                ? `到期的那 ${agent.score.unverifiable} 条一条都没判出来 —— 不是它答错，是断言写成了没法验的形式。先让它把断言改成可验证的，再谈准不准。`
                : '现在还不能说它准不准。'}
            </>
          ) : (
            <>
              它过去的 {agent.score.judged} 条断言已判：{` `}
              <b>对 {agent.score.correct} / 错 {agent.score.wrong}</b>
              （{(agent.score.rate! * 100).toFixed(0)}% 兑现）
              {agent.score.pending > 0 ? `；另有 ${agent.score.pending} 条还在等判定日` : ''}
              {agent.score.unverifiable > 0 ? `、${agent.score.unverifiable} 条判不了` : ''}
              。样本 {agent.score.judged} 条，先别当成定论。
              {scLowVerifiable &&
                `另 ${agent.score.unverifiable} 条到期却判不了（可验证率 ${Math.round((agent.score.verifiable_rate ?? 0) * 100)}%）—— 断言写太大，先改断言。`}
            </>
          )}
        </div>
      )}

      {obs.length > 0 && (
        <div style={{ marginTop: 10 }}>
          <div className="small" style={{ marginBottom: 4 }}>
            <b>反对</b>（对已经存在的建议说的，不是新建议）
          </div>
          {obs.map((o) => (
            <div key={o.objection_id} className="warn" style={{ marginBottom: 6 }}>
              <span className={`tag ${o.verdict === 'oppose' ? 'over' : 'medium'}`}>
                #{o.decision_id} {o.verdict === 'oppose' ? '反对' : '存疑'}
              </span>
              <span className="small">{o.why}</span>
            </div>
          ))}
        </div>
      )}

      {qs.length > 0 && (
        <div style={{ marginTop: 10 }}>
          <div className="small" style={{ marginBottom: 4 }}>
            <b>疑问</b>
          </div>
          {qs.map((q) => {
            const sel = answers[q.id] || []
            return (
              <div
                key={q.id}
                style={{ borderTop: '1px solid var(--line)', paddingTop: 8, marginTop: 8 }}
              >
                <div className="row">
                  <span className="tag">{q.id}</span>
                  <b className="small">{q.object}</b>
                  {q.impact != null && (
                    <span className="small muted mono">影响 {fmtMoney(q.impact)}</span>
                  )}
                  {q.uncertainty != null && (
                    <span className="small muted mono">不确定 {q.uncertainty}</span>
                  )}
                </div>
                <div className="small" style={{ margin: '6px 0' }}>{q.ask}</div>
                {(q.options || []).map((opt, i) => (
                  <label
                    key={i}
                    className="small"
                    style={{ display: 'block', padding: '2px 0' }}
                  >
                    <input
                      type="checkbox"
                      checked={sel.includes(i)}
                      onChange={() => onToggle(q.id, i)}
                    />{' '}
                    {opt}
                  </label>
                ))}
              </div>
            )
          })}
        </div>
      )}

      {evals.length > 0 && (
        <details style={{ marginTop: 10 }}>
          <summary className="small muted">
            它的 {evals.length} 条断言，到期会自动判
            {evals.some((e) => e.verdict === 'unverifiable')
              ? '；判不了的会写清楚为什么判不了（那是断言写法的问题，不是它答错）'
              : '对错'}
          </summary>
          {evals.map((e) => (
            <div key={e.eval_id} className="small" style={{ padding: '3px 0' }}>
              <span className="tag">{e.verdict}</span> {e.claim}
              <span className="muted"> · 到期 {e.verifiable_at.slice(0, 10)}</span>
            </div>
          ))}
        </details>
      )}
    </div>
  )
}
