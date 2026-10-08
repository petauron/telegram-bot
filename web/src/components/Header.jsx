import { CircleUserRound, LogOut, RefreshCw } from './Icons'

export function Header({
  syncedLabel,
  refreshing,
  onRefresh,
  username,
  onLogout,
  loggingOut,
}) {
  return (
    <header className="topbar">
      <strong className="mobile-brand">群讯雷达</strong>
      <div className="topbar-actions">
        <span className="sync-label">最后同步 {syncedLabel}</span>
        <button className="header-action refresh-action" type="button" onClick={onRefresh} disabled={refreshing} aria-label="刷新数据">
          <RefreshCw size={17} className={refreshing ? 'spin' : ''} />
          <span>刷新</span>
        </button>
        <span className="user-chip" aria-label={`当前管理员 ${username}`}>
          <CircleUserRound size={18} />
          <span>{username}</span>
        </span>
        <button className="header-action logout-button" type="button" onClick={onLogout} disabled={loggingOut}>
          <LogOut size={17} />
          <span>{loggingOut ? '退出中…' : '退出'}</span>
        </button>
      </div>
    </header>
  )
}
