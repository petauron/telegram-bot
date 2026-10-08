import { MessageSquareText, Star } from './Icons'

const EMPTY_SUMMARY = {
  total: 0,
  up: 0,
  down: 0,
  event_count: 0,
  positive_rate: null,
  minimum_samples: 3,
}

function formatDateTime(value) {
  return value ? new Date(value).toLocaleString('zh-CN') : '—'
}

export function FeedbackView({ data, loading }) {
  const summary = data?.summary || EMPTY_SUMMARY
  const items = data?.items || []
  const byKind = data?.by_content_kind || []
  return (
    <main className="page feedback-view" aria-busy={loading}>
      <header className="page-heading compact-heading">
        <div>
          <span className="eyebrow">FEEDBACK LOOP</span>
          <h2>反馈记录</h2>
          <p>查看 ntfy 点赞与点踩；反馈作为后续模型评分的弱校准信号，不会绕过分类、安全和去重门控。</p>
        </div>
      </header>

      <section className="feedback-metrics" aria-label="最近 90 天反馈汇总">
        <article><span>有效反馈</span><strong>{summary.total}</strong><small>{summary.event_count} 次操作</small></article>
        <article className="positive"><span>有用</span><strong>{summary.up}</strong><small>{summary.positive_rate == null ? '暂无比例' : `${summary.positive_rate}% 好评`}</small></article>
        <article className="negative"><span>无用</span><strong>{summary.down}</strong><small>每条通知只计最新选择</small></article>
        <article><span>学习门槛</span><strong>{summary.minimum_samples}</strong><small>相关样本不足时不调整</small></article>
      </section>

      {byKind.length ? (
        <section className="feedback-kind-strip" aria-label="按内容类型反馈">
          {byKind.map((item) => (
            <span key={item.content_kind}><b>{item.label}</b>{item.up} 赞 / {item.down} 踩</span>
          ))}
        </section>
      ) : null}

      <section className="surface feedback-records">
        <div className="section-heading-row">
          <div><Star size={18} /><h2>最近记录</h2></div>
          <span>最近 90 天</span>
        </div>
        <div className="feedback-table-wrap">
          <table className="feedback-table">
            <thead><tr><th>反馈</th><th>通知精华</th><th>类型</th><th>AI 分</th><th>时间</th></tr></thead>
            <tbody>
              {items.map((item) => (
                <tr key={`${item.message_row_id || 'expired'}-${item.voted_at}`}>
                  <td><b className={`feedback-vote ${item.vote}`}>{item.vote === 'up' ? '👍 有用' : '👎 无用'}</b>{item.event_count > 1 ? <small>修改 {item.event_count - 1} 次</small> : null}</td>
                  <td><strong>{item.title}</strong><small>{item.source_name}</small></td>
                  <td><span className={`category-label ${item.content_kind}`}>{item.content_kind_label}</span></td>
                  <td>{item.ai_score ?? '—'}</td>
                  <td>{formatDateTime(item.voted_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {!loading && items.length === 0 ? (
            <div className="quiet-empty"><MessageSquareText size={24} /><strong>还没有反馈记录</strong><span>在 ntfy 通知中点击“有用”或“无用”后会显示在这里。</span></div>
          ) : null}
          {loading ? <div className="quiet-loading">正在加载反馈…</div> : null}
        </div>
      </section>
    </main>
  )
}

export { EMPTY_SUMMARY }
