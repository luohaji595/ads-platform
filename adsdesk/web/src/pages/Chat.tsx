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

/** 把库里的一轮转成气泡。溯源压成一行小字 —— 答案本身之外，「它凭什么这么说」才是要留的东西。 */
function toBubble(t: ChatTurn): Bubble {
  const bits: string[] = []
  if (t.cite?.run_id) bits.push(`奏折 #${t.cite.run_id}`)
  if (t.cite?.cited_decision_ids?.length) {
    bits.push(`decision_id=${t.cite.cited_decision_ids.join(',')}`)
  }
  if (t.cite?.null_decision) bits.push('含「没落库」那条')
  return {
    key: `h-${t.turn_id}`,
    role: t.role === 'user' ? 'user' : 'assistant',
    text: t.content,
    meta: bits.length ? bits.join(' · ') : undefined,
  }
}

/**
 * 对话页。
 *
 * 这里只负责「把话递出去、把回答摆出来」，不解释事实 —— 每条结论都得能指回奏折或
 * decision_id，指不回去的回答要在小字里露出来（没有锚点 = 回看时该打折扣）。
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
        if (alive) setNote(`历史没读到：${e instanceof Error ? e.message : String(e)}`)
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

  async function send() {
    const text = draft.trim()
    if (!text || busy) return
    setDraft('')
    setNote(null)
    setBubbles((b) => [...b, { key: `u-${Date.now()}`, role: 'user', text }])
    setBusy(true)
    startedAt.current = Date.now()
    setWaited(0)
    try {
      const r = await sayChat({ text, as_of: asOf.trim() || null })
      const meta = [
        r.model ?? '模型不明',
        `${(r.elapsed_ms / 1000).toFixed(1)}s`,
        r.tokens ? `tokens ${r.tokens.in}/${r.tokens.out}` : null,
        r.business_day ? `奏折 ${r.business_day}` : null,
        r.cited_decision_ids?.length ? `decision_id=${r.cited_decision_ids.join(',')}` : null,
      ]
        .filter(Boolean)
        .join(' · ')
      setBubbles((b) => [...b, { key: `a-${Date.now()}`, role: 'assistant', text: r.reply, meta }])
      // 回答是真的、只是没落库：话照给，但要明说这回看时查不到它。
      if (!r.ok) setNote(r.warning ?? '这轮回答没写进台账，事后回看看不到它。')
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
          <span className="sub">
            答案只用库里的事实拼；指不到奏折或 decision_id 的，气泡下面那行会是空的
          </span>
        </h2>

        <div className="chat-box" ref={scroller}>
          {!loaded && <div className="muted small">正在读历史……</div>}
          {loaded && bubbles.length === 0 && (
            <div className="chat-empty muted small">
              还没有对话。可以这样问：「今天为什么降 lat pulldown 的价」「上周那条判断后来对了吗」
              「这个词的钱花在哪了」。一轮要十几到上百秒，发送后会有秒数在走。
            </div>
          )}
          {bubbles.map((b) => (
            <div key={b.key} className={`bubble ${b.role}`}>
              <div className="who">
                {b.role === 'user' ? '你' : b.role === 'assistant' ? 'agent' : '出错'}
              </div>
              <div className="body">
                <Rich text={b.text} />
              </div>
              {b.meta && <div className="meta">{b.meta}</div>}
            </div>
          ))}
          {busy && (
            <div className="bubble assistant">
              <div className="who">agent</div>
              <div className="body chat-wait">
                正在翻库并回答…… 已等 {waited} 秒
              </div>
            </div>
          )}
        </div>

        {note && <div className="warn small">{note}</div>}

        <div className="chat-input">
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={onKeyDown}
            placeholder="问一句。Enter 发送，Shift+Enter 换行"
            rows={3}
          />
          <div className="row">
            <input
              type="text"
              value={asOf}
              onChange={(e) => setAsOf(e.target.value)}
              placeholder="用哪天的奏折（YYYY-MM-DD，留空=最新）"
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