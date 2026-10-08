import { Activity, BellPlus, CircleUserRound, Home, MessageSquareText, Rss, Settings2, Star } from './Icons'

const NAV = [
  { id: 'overview', label: '总览', icon: Home },
  { id: 'messages', label: '消息流', icon: MessageSquareText },
  { id: 'sources', label: '信息源', icon: Rss },
  { id: 'config', label: '规则配置', icon: Settings2 },
  { id: 'push', label: '推送渠道', icon: BellPlus },
  { id: 'feedback', label: '反馈记录', icon: Star },
  { id: 'status', label: '运行状态', icon: Activity },
]

export function Sidebar({ active, onNavigate, username }) {
  return (
    <aside className="sidebar">
      <div className="brand">
        <span>群讯雷达</span>
      </div>
      <nav className="primary-nav" aria-label="主导航">
        {NAV.map(({ id, label, icon: Icon }) => (
          <button
            className={`nav-item ${active === id ? 'active' : ''}`}
            key={id}
            onClick={() => onNavigate(id)}
            type="button"
            aria-label={label}
            aria-current={active === id ? 'page' : undefined}
          >
            <Icon size={19} />
            <span>{label}</span>
          </button>
        ))}
      </nav>
      <div className="sidebar-foot">
        <div className="admin-row">
          <CircleUserRound size={24} />
          <span>{username}</span>
        </div>
      </div>
    </aside>
  )
}
