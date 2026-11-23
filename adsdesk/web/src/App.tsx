import { useEffect, useState } from 'react'
import AgentPanel from './pages/AgentPanel'
import Approval from './pages/Approval'
import Chat from './pages/Chat'
import Cockpit from './pages/Cockpit'
import { get, type Freshness } from './lib/api'

type Tab = 'approval' | 'cockpit' | 'chat' | 'agent'

/** 允许用 ?tab=cockpit 直接打开某一页 —— 省得每次都点一下，也方便截图和分享链接。 */
function initialTab(): Tab {
  const q = new URLSearchParams(window.location.search).get('tab')
  return q === 'cockpit' || q === 'chat' || q === 'agent' ? q : 'approval'
}

export default function App() {
  const [tab, setTab] = useState<Tab>(initialTab())
  const [fresh, setFresh] = useState<Freshness | null>(null)

  useEffect(() => {
    window.history.replaceState(null, '', `?tab=${tab}`)
    get<Freshness>('/meta/freshness').then(setFresh).catch(() => setFresh(null))
  }, [tab])

  const stale = fresh?.stale_any
  return (
    <>
      <div className="topbar">
        <h1>广告工作台</h1>
        <nav>
          <button className={tab === 'approval' ? 'on' : ''} onClick={() => setTab('approval')}>
            审批台
          </button>
          <button className={tab === 'cockpit' ? 'on' : ''} onClick={() => setTab('cockpit')}>
            驾驶舱
          </button>
          <button className={tab === 'chat' ? 'on' : ''} onClick={() => setTab('chat')}>
            对话
          </button>
          <button className={tab === 'agent' ? 'on' : ''} onClick={() => setTab('agent')}>
            Agent
          </button>
        </nav>
        <div className="grow" />
        {fresh && (
          <div className="badge">
            {fresh.last_memorial ? (
              <>
                数据至 <b>{fresh.last_memorial.business_day}</b> · 奏折 run #{fresh.last_run?.run_id ?? '—'}
              </>
            ) : (
              '还没有奏折'
            )}
            {stale && <span style={{ color: 'var(--amber)' }}> · 数据变旧</span>}
          </div>
        )}
      </div>
      <div className="wrap">
        {tab === 'approval' && <Approval freshness={fresh} />}
        {tab === 'cockpit' && <Cockpit />}
        {tab === 'chat' && <Chat />}
        {tab === 'agent' && <AgentPanel />}
      </div>
    </>
  )
}