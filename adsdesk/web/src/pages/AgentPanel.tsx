import { useEffect, useState } from 'react'
import {
  getAgentAssumptions,
  getAgentEvals,
  getAgentObjections,
  getAgentRuns,
  getAgentSummary,
  type AgentEvalByAgent,
  type AgentObjectionRow,
  type AgentObjectionSummary,
  type AgentRunRow,
  type AgentSummary,
} from '../lib/api'
import { PolicyList } from '../components/PolicyList'

/**
 * Agent 账本。
 *
 * 四块：最近运行 / 反对与兑现 / 断言兑现率 / 待复核假设。
 * 只读 —— 这里没有任何能改动投放的按钮，它只是把 agent 说过的话摊开，等时间到了判对错。
 */

type Load<T> = { data: T | null; err: string | null }

/** 每块自己取数、自己报错：一块挂了不影响其它块（和后端 summary 的容错对齐）。 */
function useLoad<T>(run: () => Promise<T>, deps: unknown[] = []): Load<T> {
  const [s, setS] = useState<Load<T>>({ data: null, err: null })
  useEffect(() => {
    let alive = true
    run()
      .then((d) => {
        if (alive) setS({ data: d, err: null })
      })
      .catch((e: unknown) => {
        if (alive) setS({ data: null, err: String((e as Error)?.message || e) })
      })
    return () => {
      alive = false
    }
    // run 是每次渲染新建的闭包，不能直接进依赖；要重取就传 deps（例如筛选/排序）。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps)
  return s
}

const when = (s: string | null | undefined) => (s ? s.slice(0, 16).replace('T', ' ') : '—')
const day = (s: string | null | undefined) => (s ? s.slice(0, 10) : '—')

function dur(sec: number | null) {
  if (sec == null) return '—'
  if (sec < 60) return `${sec.toFixed(1)}s`
  const m = Math.floor(sec / 60)
  const r = Math.round(sec - m * 60)
  return `${m}m${r}s`
}

const pct = (rate: number | null) => (rate == null ? null : `${(rate * 100).toFixed(0)}%`)

/** 还剩几天到期。反对的「对不对」是到期才判的，光看日期还得自己数天数。 */
function daysLeft(s: string | null | undefined): string {
  if (!s) return ''
  const t = new Date(s).getTime()
  if (!Number.isFinite(t)) return ''
  const d = Math.ceil((t - Date.now()) / 86_400_000)
  if (d > 0) return `还有 ${d} 天`
  if (d === 0) return '今天判定'
  return `已过 ${-d} 天`
}

type Tone = { cls: string; label: string }

const STATUS: Record<string, Tone> = {
  ok: { cls: 'ok', label: '成功' },
  failed: { cls: 'mismatch', label: '失败' },
  running: { cls: 'medium', label: '运行中' },
}

const OUTCOME: Record<string, Tone> = {
  pending: { cls: 'unknown', label: '待兑现' },
  correct: { cls: 'ok', label: '正确' },
  wrong: { cls: 'mismatch', label: '打脸' },
  unverifiable: { cls: 'low', label: '无法判定' },
}

const VERDICT: Record<string, Tone> = {
  pending: { cls: 'unknown', label: '待验' },
  correct: { cls: 'ok', label: '对' },
  wrong: { cls: 'mismatch', label: '错' },
  unverifiable: { cls: 'low', label: '无法判定' },
}

const STANCE: Record<string, Tone> = {
  oppose: { cls: 'high', label: 'oppose 反对' },
  doubt: { cls: 'medium', label: 'doubt 存疑' },
}

function Tag({ map, value }: { map: Record<string, Tone>; value: string }) {
  const t = map[value] || { cls: 'unknown', label: value || '—' }
  return <span className={`tag ${t.cls}`}>{t.label}</span>
}

function Stat({ label, value, extra }: { label: string; value: number; extra: string }) {
  return (
    <div>
      <div className="muted small">{label}</div>
      <div style={{ fontSize: 22, fontWeight: 600, lineHeight: 1.2 }}>{value}</div>
      <div className="muted small">{extra}</div>
    </div>
  )
}

// ───────────────────────── 顶部汇总 ─────────────────────────

function SummaryStrip() {
  const { data, err } = useLoad<AgentSummary>(() => getAgentSummary())
  if (err) return <div className="err">读不到汇总：{err}</div>
  if (!data) return <div className="note">读取中…</div>
  const partial = [data.runs.error, data.objections.error, data.evals.error, data.assumptions.error]
    .filter(Boolean)
    .join('；')
  return (
    <>
      {data.note && <div className="warn">{data.note}</div>}
      <div className="row" style={{ gap: 32, marginTop: 10 }}>
        <Stat
          label="运行"
          value={data.runs.total}
          extra={`成功 ${data.runs.ok} · 失败 ${data.runs.failed}${data.runs.running ? ` · 在跑 ${data.runs.running}` : ''}`}
        />
        <Stat
          label="反对"
          value={data.objections.total}
          extra={`待兑现 ${data.objections.pending} · 已兑现 ${data.objections.checked} · 打脸 ${data.objections.wrong}`}
        />
        <Stat
          label="断言"
          value={data.evals.total}
          extra={
            data.evals.hit_rate == null
              ? '暂无到期断言'
              : `命中率 ${pct(data.evals.hit_rate)}（${data.evals.correct} 对 / ${data.evals.wrong} 错）`
          }
        />
        <Stat
          label="待复核假设"
          value={data.assumptions.open}
          extra={data.assumptions.open ? '需要人看一眼' : '暂无'}
        />
      </div>
      {partial && <div className="err small">部分块读取失败：{partial}</div>}
    </>
  )
}

// ───────────────────────── ① 最近运行 ─────────────────────────

function RunsCard() {
  const { data, err } = useLoad(() => getAgentRuns(20))
  return (
    <div className="card">
      <h2>
        最近运行
        <span className="sub">每条 run 留下了多少条断言、多少条反对</span>
      </h2>
      {err && <div className="err">读不到运行记录：{err}</div>}
      {!err && !data && <div className="note">读取中…</div>}
      {data && data.note && <div className="note">{data.note}</div>}
      {data && data.runs.length === 0 && !data.note && <div className="note">还没跑过 agent，这里暂时是空的。</div>}
      {data && data.runs.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>job</th>
              <th>状态</th>
              <th>开始</th>
              <th className="num">耗时</th>
              <th className="num">断言</th>
              <th className="num">反对</th>
              <th>模型 / tokens</th>
            </tr>
          </thead>
          <tbody>
            {data.runs.map((r: AgentRunRow) => (
              <tr key={r.agent_run_id}>
                <td className="mono">
                  {r.job}
                  <span className="muted small"> #{r.agent_run_id}</span>
                </td>
                <td>
                  <Tag map={STATUS} value={r.status} />
                  {r.error && (
                    <div className="small err" title={r.error}>
                      {r.error.slice(0, 40)}
                    </div>
                  )}
                </td>
                <td className="small">{when(r.started_at)}</td>
                <td className="num">{dur(r.elapsed_s)}</td>
                <td className="num">{r.n_evals}</td>
                <td className="num">{r.n_objections}</td>
                <td className="small">
                  {r.model || '—'}
                  {r.tokens_in != null || r.tokens_out != null ? (
                    <span className="muted">
                      {' '}
                      · {r.tokens_in ?? '—'}/{r.tokens_out ?? '—'}
                    </span>
                  ) : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}

// ───────────────────────── ② 反对与兑现 ─────────────────────────

function PredictCell({ o }: { o: AgentObjectionRow }) {
  if (!o.predicts.length) {
    return <span className="muted small">没写可验证预测</span>
  }
  return (
    <div>
      {o.predicts.map((p, i) => (
        <div key={i} className="small" style={{ marginBottom: 4 }}>
          <div>{p.claim || p.expected || '（没有描述）'}</div>
          {p.verifiable_at && (
                <div className="muted">
                  到期 {day(p.verifiable_at)} · {daysLeft(p.verifiable_at)}
                </div>
              )}
        </div>
      ))}
    </div>
  )
}

function ObjectionsCard() {
  // 排序是给人换视角用的：'recent' 看 agent 最近在担心什么，
  // 'due' 看「这几天该判的判了没」。表里 35 条有 26 条没写兑现日，
  // 所以到期日排序必须让后端排（前端只能排已返回的 20 条）。
  const [sort, setSort] = useState<'recent' | 'due'>('recent')
  const { data, err } = useLoad(() => getAgentObjections(20, 'all', sort), [sort])
  const sum: AgentObjectionSummary | null = data?.summary ?? null
  // 已到期但还没判的：这是唯一需要人动手的队列，值得单独报个数。
  const overdue = (data?.objections ?? []).filter(
    (o) => o.outcome === 'pending' && o.verifiable_at && daysLeft(o.verifiable_at).startsWith('已过'),
  ).length
  return (
    <div className="card">
      <h2>
        反对与兑现
        <span className="sub">agent 当时反对了什么，到期后对不对</span>
        <span className="segmented" style={{ marginLeft: 'auto' }}>
          <button className={sort === 'recent' ? 'on' : ''} onClick={() => setSort('recent')}>
            最近
          </button>
          <button className={sort === 'due' ? 'on' : ''} onClick={() => setSort('due')}>
            按到期日
          </button>
        </span>
      </h2>
      {overdue > 0 && (
        <div className="warn">
          本页有 {overdue} 条已过兑现日但还没判定 —— 判定由流程做，这里只是提醒你它已经过期了。
        </div>
      )}
      {err && <div className="err">读不到反对记录：{err}</div>}
      {!err && !data && <div className="note">读取中…</div>}
      {sum && (
        <div className="note">
          反对 {sum.total} 条，已兑现 {sum.checked} 条，其中打脸 {sum.wrong} 条
          {sum.pending > 0 ? `（还有 ${sum.pending} 条没到期）` : ''}
        </div>
      )}
      {data && data.objections.length === 0 && <div className="note">{data.note || '还没有反对记录。'}</div>}
      {data && data.objections.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>decision</th>
              <th>立场</th>
              <th>为什么反对</th>
              <th>预测</th>
              <th>兑现</th>
            </tr>
          </thead>
          <tbody>
            {data.objections.map((o) => (
              <tr key={o.objection_id}>
                <td className="mono small">{o.decision_id}</td>
                <td>
                  <Tag map={STANCE} value={o.verdict} />
                </td>
                <td className="small">
                  {o.why}
                  {o.evidence && (
                    <div className="muted" title={o.evidence}>
                      证据：{o.evidence.slice(0, 60)}
                      {o.evidence.length > 60 ? '…' : ''}
                    </div>
                  )}
                </td>
                <td>
                  <PredictCell o={o} />
                  {o.verifiable_at && (
                    <div className="muted small">
                      判定日 {day(o.verifiable_at)}
                      {o.outcome && o.outcome !== 'pending' ? '' : ` · ${daysLeft(o.verifiable_at)}`}
                    </div>
                  )}
                </td>
                <td>
                  <Tag map={OUTCOME} value={o.outcome} />
                  {o.actual && <div className="small muted">{o.actual.slice(0, 60)}</div>}
                  {o.judged_by && <div className="small muted">判定：{o.judged_by}</div>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}

// ───────────────────────── ③ 断言兑现率 ─────────────────────────

function RatioBar({ a }: { a: AgentEvalByAgent }) {
  const total = a.total || 0
  if (!total) return <span className="muted small">暂无到期断言</span>
  const seg = (n: number, color: string, label: string) =>
    n > 0 ? (
      <span
        title={`${label} ${n}`}
        style={{ width: `${(n / total) * 100}%`, background: color, display: 'block' }}
      />
    ) : null
  return (
    <div
      style={{
        display: 'flex',
        height: 10,
        minWidth: 220,
        borderRadius: 5,
        overflow: 'hidden',
        background: 'var(--line)',
      }}
    >
      {seg(a.correct, 'var(--green)', '对')}
      {seg(a.wrong, 'var(--red)', '错')}
      {seg(a.unverifiable, 'var(--amber)', '无法判定')}
      {seg(a.pending, 'var(--muted)', '待验')}
    </div>
  )
}

function EvalsCard() {
  const { data, err } = useLoad(() => getAgentEvals(30, 'all'))
  return (
    <div className="card">
      <h2>
        断言兑现率
        <span className="sub">按 agent 分组：对 / 错 / 无法判定 / 还没到期</span>
      </h2>
      {err && <div className="err">读不到断言记录：{err}</div>}
      {!err && !data && <div className="note">读取中…</div>}
      {data && data.note && <div className="note">{data.note}</div>}
      {data && data.by_agent.length === 0 && !data.note && (
        <div className="note">还没跑过 agent，这里暂时是空的。</div>
      )}
      {data && data.by_agent.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>agent</th>
              <th>分布</th>
              <th className="num">对 / 错 / 无法判定 / 待验</th>
              <th className="num">命中率</th>
            </tr>
          </thead>
          <tbody>
            {data.by_agent.map((a) => (
              <tr key={a.agent}>
                <td className="mono">{a.agent}</td>
                <td>
                  <RatioBar a={a} />
                </td>
                <td className="num small">
                  {a.correct} / {a.wrong} / {a.unverifiable} / {a.pending}
                  <span className="muted"> （共 {a.total}）</span>
                </td>
                <td className="num">
                  {a.hit_rate == null ? (
                    <span className="muted small">暂无到期断言</span>
                  ) : (
                    <strong>{pct(a.hit_rate)}</strong>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {data && data.by_agent.length > 0 && (
        <div className="small muted" style={{ marginTop: 8 }}>
          命中率 = 对 /（对 + 错），不含无法判定与待验。
        </div>
      )}
    </div>
  )
}

// ───────────────────────── ④ 待复核假设 ─────────────────────────

function AssumptionsCard() {
  const { data, err } = useLoad(() => getAgentAssumptions())
  return (
    <div className="card">
      <h2>
        待复核假设
        <span className="sub">跳过默认执行的建议，到期复核</span>
      </h2>
      {err && <div className="err">读不到假设记录：{err}</div>}
      {!err && !data && <div className="note">读取中…</div>}
      {data && data.assumptions.length === 0 && (
        <div className="note">还没有被跳过的建议，暂无假设待复核。</div>
      )}
      {data && data.assumptions.length > 0 && (
        <table>
          <thead>
            <tr>
              <th>对象</th>
              <th>问题代码</th>
              <th>默认动作</th>
              <th>复核日</th>
            </tr>
          </thead>
          <tbody>
            {data.assumptions.map((a) => (
              <tr key={a.assumption_id}>
                <td className="mono small">{a.object_id}</td>
                <td className="mono small">{a.question_code}</td>
                <td className="small">{a.default_action}</td>
                <td className="small">{day(a.review_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  )
}

// ───────────────────────── 页面 ─────────────────────────

export default function AgentPanel({ signal }: { signal?: number }) {
  return (
    <div>
      <div className="card tight">
        <h2>
          Agent 账本
          <span className="sub">自动诊断运行、反对复盘与策略认领台账（不直接改动投放）</span>
        </h2>
        <SummaryStrip />
      </div>
      <PolicyList signal={signal} />
      <ObjectionsCard />
      <AssumptionsCard />
      <details className="disclosure">
        <summary>
          系统运行与准确率明细（默认收起）
          <span className="disclosure-sub">最近运行记录（模型 / tokens）· 各 Agent 断言命中率</span>
        </summary>
        <div className="drawer-box">
          <RunsCard />
          <EvalsCard />
        </div>
      </details>
    </div>
  )
}
