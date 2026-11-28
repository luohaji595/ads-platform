import { useCallback, useEffect, useState } from 'react'
import AgentPanel from './pages/AgentPanel'
import Approval from './pages/Approval'
import Chat from './pages/Chat'
import Cockpit from './pages/Cockpit'
import { EXPECT_API, get, getHealth, type Freshness } from './lib/api'

type Tab = 'approval' | 'cockpit' | 'chat' | 'agent'

const PRIMARY_TABS: { key: Tab; label: string }[] = [
  { key: 'approval', label: '今日一键台' },
  { key: 'cockpit', label: '看板' },
  { key: 'chat', label: '问账' },
]

const ALL_TABS: { key: Tab; label: string }[] = [
  ...PRIMARY_TABS,
  { key: 'agent', label: '诊断与日志' },
]

/** 允许用 ?tab=cockpit 直接打开某一页 —— 保留各页直达链接兼容性。 */
function initialTab(): Tab {
  const q = new URLSearchParams(window.location.search).get('tab')
  return q === 'cockpit' || q === 'chat' || q === 'agent' ? q : 'approval'
}

/** 轮询间隔。数据变旧、新建议进来都该自己浮出来，而不是等人手动刷新。 */
const POLL_MS = 60_000

export default function App() {
  const [tab, setTab] = useState<Tab>(initialTab)
  const [fresh, setFresh] = useState<Freshness | null>(null)
  const [down, setDown] = useState<string | null>(null)
  const [apiVersion, setApiVersion] = useState<number | null>(null)
  const [signal, setSignal] = useState(0)

  const loadFresh = useCallback(() => {
    get<Freshness>('/meta/freshness')
      .then((f) => {
        setFresh(f)
        setDown(null)
        setSignal((n) => n + 1)
      })
      .catch((e: unknown) => setDown(e instanceof Error ? e.message : String(e)))
  }, [])

  useEffect(() => {
    getHealth()
      .then((h) => setApiVersion(h.api_version ?? null))
      .catch(() => setApiVersion(null))
    loadFresh()
    const t = window.setInterval(loadFresh, POLL_MS)
    return () => window.clearInterval(t)
  }, [loadFresh])

  useEffect(() => {
    const onPop = () => setTab(initialTab())
    window.addEventListener('popstate', onPop)
    return () => window.removeEventListener('popstate', onPop)
  }, [])

  const goto = useCallback((next: Tab) => {
    setTab((prev) => {
      if (prev === next) return prev
      window.history.pushState({ tab: next }, '', `?tab=${next}`)
      return next
    })
  }, [])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.metaKey || e.ctrlKey || e.altKey) return
      const el = e.target as HTMLElement | null
      const tag = el?.tagName
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || el?.isContentEditable) return
      const i = Number(e.key)
      if (i >= 1 && i <= ALL_TABS.length) {
        e.preventDefault()
        goto(ALL_TABS[i - 1].key)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [goto])

  const stale = fresh?.stale_any
  const c = fresh?.counts
  const versionMismatch = apiVersion != null && apiVersion !== EXPECT_API

  return (
    <>
      <div className="bg-grid" aria-hidden="true" />
      <div className="bg-grain" aria-hidden="true" />

      <div className="topbar">
        <h1>广告工作台</h1>
        <nav>
          {PRIMARY_TABS.map((t, i) => (
            <button
              key={t.key}
              className={tab === t.key ? 'on' : ''}
              onClick={() => goto(t.key)}
              title={`${t.label}（按 ${i + 1}）`}
            >
              {t.label}
              {t.key === 'approval' && (c?.proposed ?? 0) > 0 && (
                <span className="nav-dot">{c?.proposed}</span>
              )}
            </button>
          ))}
        </nav>
        <button
          className={`nav-secondary ${tab === 'agent' ? 'on' : ''}`}
          onClick={() => goto('agent')}
          title="诊断与日志（按 4）"
        >
          诊断与日志
        </button>
        <div className="grow" />
        {(c?.pending_policy ?? 0) > 0 && (
          <button
            className="badge badge-btn"
            onClick={() => goto('approval')}
            title="前往今日一键台认领策略"
          >
            {c?.pending_policy} 条策略待认领
          </button>
        )}
        {fresh && (
          <div className="badge">
            {fresh.last_memorial ? (
              <>
                数据至 <b>{fresh.last_memorial.business_day}</b>
              </>
            ) : (
              '暂无数据'
            )}
            {stale && <span className="warn-text"> · 数据变旧</span>}
          </div>
        )}
      </div>

      {down && (
        <div className="banner err">
          连不上后端：{down} —— 请先启动 API 服务后刷新。
        </div>
      )}
      {versionMismatch && (
        <div className="banner warn">
          前端需要接口 v{EXPECT_API}，后端当前为 v{apiVersion}：两侧版本不一致，请重建前端或重启后端。
        </div>
      )}

      <div className="wrap">
        <div hidden={tab !== 'approval'}>
          <Approval freshness={fresh} signal={signal} onRefreshFresh={loadFresh} />
        </div>
        <div hidden={tab !== 'cockpit'}>
          <Cockpit signal={signal} />
        </div>
        <div hidden={tab !== 'chat'}>
          <Chat />
        </div>
        <div hidden={tab !== 'agent'}>
          <AgentPanel signal={signal} />
        </div>
      </div>
    </>
  )
}
