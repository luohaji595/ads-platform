import { useCallback, useEffect, useState } from 'react'
import AgentPanel from './pages/AgentPanel'
import Approval from './pages/Approval'
import Chat from './pages/Chat'
import Cockpit from './pages/Cockpit'
import { EXPECT_API, get, getHealth, type Freshness } from './lib/api'

type Tab = 'approval' | 'cockpit' | 'chat' | 'agent'

const TABS: { key: Tab; label: string }[] = [
  { key: 'approval', label: '审批台' },
  { key: 'cockpit', label: '驾驶舱' },
  { key: 'chat', label: '对话' },
  { key: 'agent', label: 'Agent' },
]

/** 允许用 ?tab=cockpit 直接打开某一页 —— 省得每次都点一下，也方便截图和分享链接。 */
function initialTab(): Tab {
  const q = new URLSearchParams(window.location.search).get('tab')
  return q === 'cockpit' || q === 'chat' || q === 'agent' ? q : 'approval'
}

/** 轮询间隔。数据变旧、新建议进来都该自己浮出来，而不是等人手动刷新。 */
const POLL_MS = 60_000

export default function App() {
  const [tab, setTab] = useState<Tab>(initialTab)
  const [fresh, setFresh] = useState<Freshness | null>(null)
  // 后端可达性与接口版本：分开两个 state，因为处置方式不同
  // （一个去启动服务，一个去重新 build），混成一个「出错了」等于没说。
  const [down, setDown] = useState<string | null>(null)
  const [apiVersion, setApiVersion] = useState<number | null>(null)
  const [signal, setSignal] = useState(0)

  const loadFresh = useCallback(() => {
    get<Freshness>('/meta/freshness')
      .then((f) => {
        setFresh(f)
        setDown(null)
        // 每次新鲜度变化就让常驻页面重拉一次 —— 否则切回审批台看到的
        // 还是挂载那一刻的数据，而 tab 常驻的代价正是「不会自动重载」。
        setSignal((n) => n + 1)
      })
      .catch((e: unknown) => setDown(e instanceof Error ? e.message : String(e)))
  }, [])

  // 只在挂载时建一次轮询；tab 变化**不**触发请求（旧代码把两者写在同一个
  // effect 里，每点一次 Tab 就多打一次 /meta/freshness）。
  useEffect(() => {
    getHealth()
      .then((h) => setApiVersion(h.api_version ?? null))
      .catch(() => setApiVersion(null))
    loadFresh()
    const t = window.setInterval(loadFresh, POLL_MS)
    return () => window.clearInterval(t)
  }, [loadFresh])

  // 切页写历史（pushState），并监听 popstate —— 这样浏览器后退键能回到上一页。
  // 旧代码用 replaceState，后退直接退出站点：人被弹出去一次就不再敢用后退了。
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

  // 数字键 1~4 切页。改价时手在键盘上，不该为了换页去摸鼠标。
  // 输入框里打字不拦（否则备注里打「1」就跳页）。
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.metaKey || e.ctrlKey || e.altKey) return
      const el = e.target as HTMLElement | null
      const tag = el?.tagName
      if (tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || el?.isContentEditable) return
      const i = Number(e.key)
      if (i >= 1 && i <= TABS.length) {
        e.preventDefault()
        goto(TABS[i - 1].key)
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
      <div className="topbar">
        <h1>广告工作台</h1>
        <nav>
          {TABS.map((t, i) => (
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
              {t.key === 'agent' && (c?.pending_policy ?? 0) > 0 && (
                <span className="nav-dot">{c?.pending_policy}</span>
              )}
            </button>
          ))}
        </nav>
        <div className="grow" />
        {(c?.pending_policy ?? 0) > 0 && (
          <button className="badge badge-btn" onClick={() => goto('agent')}>
            {c?.pending_policy} 条策略待认领
          </button>
        )}
        {(c?.open_objection ?? 0) > 0 && (
          <button className="badge badge-btn" onClick={() => goto('agent')}>
            {c?.open_objection} 条反对待兑现
          </button>
        )}
        {fresh && (
          <div className="badge">
            {fresh.last_memorial ? (
              <>
                数据至 <b>{fresh.last_memorial.business_day}</b> · 奏折 run #
                {fresh.last_run?.run_id ?? '—'}
              </>
            ) : (
              '还没有奏折'
            )}
            {stale && <span className="warn-text"> · 数据变旧</span>}
          </div>
        )}
      </div>

      {down && (
        <div className="banner err">
          连不上后端：{down} —— 先在 adsdesk/api 下
          <span className="mono"> python3 -m uvicorn main:app --port 5273 </span>
          再刷新。
        </div>
      )}
      {versionMismatch && (
        <div className="banner warn">
          前端要接口 v{EXPECT_API}，后端在 v{apiVersion}：两侧不同步。改完后端接口就
          <span className="mono"> npm run build </span>
          重建前端，改了前端就先重启后端。
        </div>
      )}

      <div className="wrap">
        {/* 四个页面常驻挂载，用 hidden 切显隐：卸载会让聊天记录、滚动位置、
            勾了一半的审批全部丢掉。代价是各自要能被 signal 唤醒重拉（见下）。 */}
        <div hidden={tab !== 'approval'}>
          <Approval freshness={fresh} signal={signal} />
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
