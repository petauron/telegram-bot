import { useMemo, useState } from 'react'
import { CheckCircle2 } from './Icons'
import { LiveQueueChart } from './LiveQueueChart'
import { PageHeading } from './PageHeading'

function formatHeartbeat(value) {
  return value ? new Date(value).toLocaleString('zh-CN') : '尚无数据'
}

function fallbackHistory(stats) {
  const analysis = stats?.analysis_queue
  if (!analysis) return []
  const timestamp = new Date(stats?.heartbeat?.updated_at || Date.now()).getTime()
  return [{
    timestamp: Number.isFinite(timestamp) ? timestamp : Date.now(),
    analysisPending: Number(analysis.pending) || 0,
    analysisProcessing: Number(analysis.processing) || 0,
  }]
}

function Metric({ label, value, tone = '' }) {
  return <div><dt>{label}</dt><dd className={tone}>{value ?? '—'}</dd></div>
}

function ErrorList({ title, items }) {
  if (!items?.length) return null
  return (
    <div className="status-error-group">
      <dt>{title}</dt>
      <dd>{items.map((item) => <span key={item.category}>{item.category} <b>{item.count}</b></span>)}</dd>
    </div>
  )
}

export function StatusView({ stats, history = [], online, loading = false }) {
  const [range, setRange] = useState('15m')
  const heartbeat = stats?.heartbeat
  const analysis = stats?.analysis_queue || {}
  const delivery = stats?.delivery_queue || {}
  const analysisHealthy = analysis.health !== 'degraded'
  const deliveryHealthy = delivery.health !== 'degraded'
  const healthy = online && analysisHealthy && deliveryHealthy
  const chartSamples = useMemo(() => history.length ? history : fallbackHistory(stats), [history, stats])
  const analysisIssues = (analysis.retry || 0) + (analysis.failed || 0)
  const deliveryIssues = (delivery.retry || 0) + (delivery.failed || 0)
  const historyMeta = stats?.queue_history_meta || {}

  return (
    <main className="page status-view" aria-busy={loading}>
      <PageHeading
        title="运行状态"
        action={<span className={`status-freshness ${online ? 'online' : ''}`}><i />{online ? '刚刚更新' : '连接中断'}</span>}
      />

      <section className={`status-summary ${healthy ? 'healthy' : 'degraded'}`} aria-label="服务状态摘要">
        <header className="status-summary-copy">
          <span className="status-summary-icon"><CheckCircle2 size={28} /></span>
          <div>
            <h3>{healthy ? '服务运行正常' : '服务需要关注'}</h3>
            <p>{healthy ? '监听、分析与通知均在工作' : online ? '服务在线，但队列中存在重试或失败' : '监听连接已中断，请查看技术详情'}</p>
          </div>
        </header>
        <dl className="status-primary-facts">
          <Metric label="监听来源" value={heartbeat?.watch_count} />
          <Metric label="待分析" value={analysis.pending} tone={(analysis.pending || 0) > 0 ? 'accent' : ''} />
          <Metric label="待投递" value={delivery.pending} tone={(delivery.pending || 0) > 0 ? 'accent' : ''} />
        </dl>
      </section>

      {(analysisIssues || deliveryIssues) ? (
        <section className="status-attention" role="status">
          <strong>有项目需要关注</strong>
          <span>{analysisIssues ? `分析队列 ${analysisIssues} 个重试或失败` : ''}{analysisIssues && deliveryIssues ? ' · ' : ''}{deliveryIssues ? `通知队列 ${deliveryIssues} 个重试或失败` : ''}</span>
        </section>
      ) : null}

      <section className="status-chart-card" aria-labelledby="analysis-queue-title">
        <header className="status-section-heading">
          <div>
            <h3 id="analysis-queue-title">分析队列</h3>
            <p>当前积压与处理中的任务</p>
          </div>
          <div className="queue-range-control" role="group" aria-label="图表时间范围">
            <button type="button" className={range === '15m' ? 'active' : ''} aria-pressed={range === '15m'} onClick={() => setRange('15m')}>15 分钟</button>
            <button type="button" className={range === '1h' ? 'active' : ''} aria-pressed={range === '1h'} onClick={() => setRange('1h')}>1 小时</button>
          </div>
        </header>
        <LiveQueueChart
          samples={chartSamples}
          range={range}
          sampleIntervalSeconds={historyMeta.sample_interval_seconds || 10}
          retentionDays={historyMeta.retention_days || 3}
        />
      </section>

      <section className="status-delivery-card" aria-labelledby="delivery-queue-title">
        <h3 id="delivery-queue-title">通知投递</h3>
        <dl>
          <Metric label="待投递" value={delivery.pending} />
          <Metric label="重试" value={delivery.retry} tone={(delivery.retry || 0) > 0 ? 'warning' : ''} />
          <Metric label="成功率" value={delivery.success_rate == null ? '—' : `${delivery.success_rate}%`} tone={deliveryHealthy ? 'healthy' : 'warning'} />
        </dl>
        <span className={`delivery-health-mark ${deliveryHealthy ? 'healthy' : 'degraded'}`} aria-label={deliveryHealthy ? '投递正常' : '投递需要关注'}><CheckCircle2 size={23} /></span>
      </section>

      <details className="technical-details status-technical-details">
        <summary>查看技术详情</summary>
        <div className="status-technical-grid">
          <dl>
            <h4>连接</h4>
            <Metric label="连接状态" value={heartbeat?.connected ? '已连接' : '未连接'} tone={heartbeat?.connected ? 'healthy' : 'warning'} />
            <Metric label="最近心跳" value={formatHeartbeat(heartbeat?.updated_at)} />
            <Metric label="最近消息" value={formatHeartbeat(stats?.latest_message_at)} />
          </dl>
          <dl>
            <h4>分析队列</h4>
            <Metric label="处理中" value={analysis.processing} />
            <Metric label="重试 / 失败" value={`${analysis.retry ?? '—'} / ${analysis.failed ?? '—'}`} />
            <Metric label="滚动成功率" value={analysis.success_rate == null ? '—' : `${analysis.success_rate}%`} />
            <Metric label="滚动错误率" value={analysis.error_rate == null ? '—' : `${analysis.error_rate}%`} />
          </dl>
          <dl>
            <h4>投递队列</h4>
            <Metric label="投递中" value={delivery.processing} />
            <Metric label="重试 / 失败" value={`${delivery.retry ?? '—'} / ${delivery.failed ?? '—'}`} />
            <Metric label="滚动成功率" value={delivery.success_rate == null ? '—' : `${delivery.success_rate}%`} />
            <Metric label="滚动错误率" value={delivery.error_rate == null ? '—' : `${delivery.error_rate}%`} />
          </dl>
        </div>
        <dl className="status-error-details">
          <ErrorList title="分析错误" items={analysis.error_categories} />
          <ErrorList title="投递错误" items={delivery.error_categories} />
        </dl>
      </details>
    </main>
  )
}

export { fallbackHistory, formatHeartbeat }
