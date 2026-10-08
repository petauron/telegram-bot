import { Activity, CheckCircle2, MessageSquareText, Send } from './Icons'

function Signal({ icon: Icon, label, value, tone = '' }) {
  return (
    <div className={`signal-item ${tone}`}>
      <Icon size={20} aria-hidden="true" />
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  )
}

export function MetricsBand({ stats, online }) {
  const pushed = Number(stats?.immediate_pushes || 0) + Number(stats?.digest_pushes || 0)
  return (
    <section className="signal-strip" aria-label="运行概览">
      <Signal icon={MessageSquareText} label="今日采集" value={stats?.window_messages ?? '—'} />
      <Signal icon={CheckCircle2} label="资讯合格" value={stats?.eligible_messages ?? '—'} />
      <Signal icon={Send} label="已推送" value={stats ? pushed : '—'} />
      <Signal
        icon={Activity}
        label="监听状态"
        value={online ? '正常' : '离线'}
        tone={online ? 'healthy' : 'danger'}
      />
    </section>
  )
}
