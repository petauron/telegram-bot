import { ArrowRight, Newspaper } from './Icons'
import { MetricsBand } from './MetricsBand'
import { PageHeading } from './PageHeading'
import { categoryLabel } from '../model'

function formatTime(value) {
  if (!value) return '—'
  const date = new Date(value)
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(date)
}

function auditState(row) {
  const deliveries = row.deliveries || []
  const succeeded = deliveries.filter((item) => item.state === 'succeeded').length
  if (succeeded > 0 && succeeded < deliveries.length) return ['部分送达', 'error']
  if (deliveries.some((item) => ['queued', 'processing', 'retry'].includes(item.state))) return ['投递中', 'pending']
  if (deliveries.some((item) => item.state === 'failed')) return ['投递失败', 'error']
  if (row.immediate_pushed_at) return ['已推送', 'sent']
  if (row.digest_pushed_at) return ['历史摘要已推送', 'digest']
  if (row.push_eligible) return ['待推送', 'eligible']
  if (row.prefilter_status === 'filtered') return ['本地过滤', 'filtered']
  if (row.ai_status === 'filtered_non_information') return ['非资讯', 'blocked']
  if (row.ai_status === 'error') return ['分析失败', 'error']
  if (row.ai_status === 'pending') return ['分析中', 'pending']
  return ['未推送', 'blocked']
}

function PipelineStep({ label, value, note }) {
  return (
    <li>
      <span><strong>{label}</strong><small>{note}</small></span>
      <b>{value ?? '—'}</b>
    </li>
  )
}

export function OverviewView({ stats, rows, online, loading, onOpenMessage, onOpenMessages }) {
  return (
    <main className="page overview-view">
      <PageHeading
        title="资讯总览"
        description="了解资讯采集、处理与推送的总体情况。"
      />
      <MetricsBand stats={stats} online={online} />

      <div className="overview-layout">
        <section className="surface attention-surface" aria-labelledby="attention-title">
          <header className="surface-heading">
            <h3 id="attention-title">值得关注</h3>
            <button className="text-action" type="button" onClick={onOpenMessages}>
              查看全部消息 <ArrowRight size={15} />
            </button>
          </header>
          <div className="attention-list" aria-busy={loading}>
            {rows.map((row) => {
              const [state, tone] = auditState(row)
              return (
                <button className="attention-row" type="button" key={row.id} onClick={() => onOpenMessage(row)}>
                  <span className="attention-marker" aria-hidden="true" />
                  <span className="attention-copy">
                    <strong>{row.ai_summary || row.text || '（无文字内容）'}</strong>
                    <small>
                      {row.chat_name || '未知来源'}
                      {row.sender_name ? ` · ${row.sender_name}` : ''}
                      <time>{formatTime(row.sent_at)}</time>
                    </small>
                  </span>
                  <span className={`attention-score score-${row.ai_score >= 80 ? 'high' : row.ai_score >= 50 ? 'medium' : 'low'}`}>{row.ai_score ?? '—'}</span>
                  <span className={`delivery-state ${tone}`}>{state}</span>
                </button>
              )
            })}
            {!loading && rows.length === 0 ? (
              <div className="quiet-empty"><Newspaper size={25} /><strong>最近 24 小时没有值得关注的已完成资讯</strong></div>
            ) : null}
            {loading ? <div className="quiet-loading">正在加载最近消息…</div> : null}
          </div>
        </section>

        <aside className="surface overview-rail">
          <section aria-labelledby="pipeline-title">
            <header className="surface-heading"><h3 id="pipeline-title">处理概况</h3></header>
            <ol className="pipeline-list">
              <PipelineStep label="本地过滤" value={stats?.window_messages} note={`过滤 ${stats?.prefiltered_messages ?? '—'} 条`} />
              <PipelineStep label="资讯分类" value={stats ? Math.max(0, Number(stats.window_messages || 0) - Number(stats.prefiltered_messages || 0)) : null} note={`非资讯 ${stats?.non_information_messages ?? '—'} 条`} />
              <PipelineStep label="资讯评分" value={stats?.eligible_messages} note="严格成功" />
              <PipelineStep label="推送" value={stats ? Number(stats.immediate_pushes || 0) + Number(stats.digest_pushes || 0) : null} note="60 分实时送达" />
            </ol>
          </section>
          <section className="service-summary" aria-labelledby="service-title">
            <header className="surface-heading">
              <h3 id="service-title">服务状态</h3>
              <span className={`health-dot ${online ? 'online' : 'offline'}`}><i />{online ? '正常' : '离线'}</span>
            </header>
            <dl>
              <div><dt>活跃会话</dt><dd>{stats?.active_chats ?? '—'}</dd></div>
              <div><dt>实时推送线</dt><dd>{stats?.immediate_score ?? '—'} 分</dd></div>
              <div><dt>最近采集</dt><dd>{formatTime(stats?.latest_message_at)}</dd></div>
            </dl>
          </section>
        </aside>
      </div>
    </main>
  )
}

export { auditState, formatTime }
