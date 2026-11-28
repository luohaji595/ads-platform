import { useEffect, useRef, useState, type KeyboardEvent } from 'react'
import { getChatHistory, sayChat, type ChatTurn } from '../lib/api'
import Rich from '../components/Rich'

type Bubble = {
  key: string
  role: 'user' | 'assistant' | 'error'
  text: string
  /** 气泡下面那行小字：这轮用哪个模型、多久、看的是哪份奏折、有没有指到 decision_id。 */
  meta?: string
}

const STARTER_QUESTIONS = [
  '今天为什么没有直接执行的调价建议？',
  '最近花了钱但没出单的词有哪些？',
  '上周批过的调价现在效果怎么样？',
  '目前最需要我人工拍板的问题是什么？',
]

/** 把库里的一轮转成气泡。溯源压成一行小字 —— 答案本身之外，「它凭什么这么说」才是要留的东西。 */
function toBubble(t: ChatTurn): Bubble {
  const bits: string[] = []
  if (t.cite?.run_id) bits.push(`奏折 #${t.cite.run_id}`)
  if (t.cite?.cited_decision_ids?.length) {
    bits.push(`决策 #${t.cite.cited_decision_ids.join(',')}`)
  }
  if (t.cite?.null_decision) bits.push('含未入账条目')
  return {
    key: `h-${t.turn_id}`,
    role: t.role === 'user' ? 'user' : 'assistant',
    text: t.content,
    meta: bits.length ? bits.join(' · ') : undefined,
  }
}

/**
 * 对话页（问账）。
 *
 * 只基于广告数据与决策台账作答，回答下方标注对应奏折与决策出处。
 */
export default function Chat() {
  const [bubbles, setBubbles] = useState<Bubble[]>([])
  const [draft, setDraft] = useState('')
  const [asOf, setAsOf] = useState('')
  const [busy, setBusy] = useState(false)
  const [waited, setWaited] = useState(0)
  const [note, setNote] = useState<string | null>(null)
  const [loaded, setLoaded] = useState(false)
  const startedAt = useRef(0)
  const scroller = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    let alive = true
    getChatHistory(50)
      .then((r) => {
        if (alive) setBubbles(r.turns.map(toBubble))
      })
      .catch((e) => {
        if (alive) setNote(`历史记录读取失败：${e instanceof Error ? e.message : String(e)}`)
      })
      .finally(() => {
        if (alive) setLoaded(true)
      })
    return () => {
      alive = false
    }
  }, [])

  // 一轮几十秒，新气泡会落到看不见的地方；每次动完把视口推到最底。
  useEffect(() => {
    const el = scroller.current
    if (el) el.scrollTop = el.scrollHeight
  }, [bubbles, busy])

  // 没有秒数打底，二三十秒的等待看起来和卡死一模一样。所以自己数秒，不用纯转圈。
  useEffect(() => {
    if (!busy) return
    const id = window.setInterval(
      () => setWaited(Math.round((Date.now() - startedAt.current) / 1000)),
      500,
    )
    return () => window.clearInterval(id)
  }, [busy])

  async function send(textOverride?: string) {
    const text = (textOverride ?? draft).trim()
    if (!text || busy) return
    if (textOverride === undefined) setDraft('')
    setNote(null)
    setBubbles((b) => [...b, { key: `u-${Date.now()}`, role: 'user', text }])
    setBusy(true)
    startedAt.current = Date.now()
    setWaited(0)
    try {
      const r = await sayChat({ text, as_of: asOf.trim() || null })
      const meta = [
        `${(r.elapsed_ms / 1000).toFixed(1)}s`,
        r.business_day ? `奏折 ${r.business_day}` : null,
        r.cited_decision_ids?.length ? `决策 #${r.cited_decision_ids.join(',')}` : null,
      ]
        .filter(Boolean)
        .join(' · ')
      setBubbles((b) => [...b, { key: `a-${Date.now()}`, role: 'assistant', text: r.reply, meta }])
      // 回答是真的、只是没落库：话照给，但要明说这回看时查不到它。
      if (!r.ok) setNote(r.warning ?? '本轮回答未写入台账，刷新后可能无法回看。')
    } catch (e) {
      setBubbles((b) => [
        ...b,
        {
          key: `e-${Date.now()}`,
          role: 'error',
          text: e instanceof Error ? e.message : String(e),
        },
      ])
    } finally {
      setBusy(false)
    }
  }

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    // 中文输入法选字时按回车是在确认候选词，不是发送；不挡掉会把人半句话发出去。
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault()
      void send()
    }
  }

  return (
    <div className="chat">
      <div className="card chat-panel">
        <h2>
          问账
          <span className="sub">基于数据与决策台账作答，回答下方标注出处</span>
        </h2>

        <div className="chat-box" ref={scroller}>
          {!loaded && <div className="muted small">正在读取历史对话…</div>}
          {loaded && bubbles.length === 0 && (
            <div className="chat-empty muted small">
              还没有对话。点击下方快捷问题可直接发问，或在输入框自由提问。
            </div>
          )}
          {bubbles.map((b) => (
            <div key={b.key} className={`bubble ${b.role}`}>
              <div className="who">
                {b.role === 'user' ? '你' : b.role === 'assistant' ? '助手' : '出错'}
              </div>
              <div className="body">
                <Rich text={b.text} />
              </div>
              {b.meta && <div className="meta">{b.meta}</div>}
            </div>
          ))}
          {busy && (
            <div className="bubble assistant">
              <div className="who">助手</div>
              <div className="body chat-wait">
                正在检索数据并整理回答… 已等 {waited} 秒
              </div>
            </div>
          )}
        </div>

        {note && <div className="warn small">{note}</div>}

        <div className="pill-row" aria-label="快捷问题">
          {STARTER_QUESTIONS.map((q) => (
            <button
              key={q}
              type="button"
              className="pill-btn"
              disabled={busy}
              onClick={() => void send(q)}
            >
              {q}
            </button>
          ))}
        </div>

        <div className="chat-input">
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={onKeyDown}
            placeholder="输入问题，Enter 发送，Shift+Enter 换行"
            rows={2}
          />
          <div className="row">
            <input
              type="text"
              value={asOf}
              onChange={(e) => setAsOf(e.target.value)}
              placeholder="指定复盘日期（YYYY-MM-DD，留空看最新）"
            />
            <button className="btn primary" disabled={busy || !draft.trim()} onClick={() => void send()}>
              {busy ? `发送中 ${waited}s` : '发送'}
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}