import { Send } from './Icons'
import { categoryLabel } from '../model'
import { semanticDedupeLabel } from '../model'

function scoreTone(score) {
  if (score >= 80) return 'high'
  if (score >= 50) return 'medium'
  return 'low'
}

function formatDate(value) {
  if (!value) return ['—', '']
  const date = new Date(value)
  return [
    new Intl.DateTimeFormat('zh-CN', { hour: '2-digit', minute: '2-digit', hour12: false }).format(date),
    new Intl.DateTimeFormat('zh-CN', { month: '2-digit', day: '2-digit' }).format(date),
  ]
}

function pushState(row) {
  const deliveries = row.deliveries || []
  const succeeded = deliveries.filter((item) => item.state === 'succeeded').length
  const retrying = deliveries.some((item) => ['queued', 'processing', 'retry'].includes(item.state))
  const failed = deliveries.some((item) => item.state === 'failed')
  if (succeeded > 0 && succeeded < deliveries.length) return [`部分送达 ${succeeded}/${deliveries.length}`, 'error']
  if (retrying) return ['投递处理中', 'pending']
  if (failed) return ['投递失败', 'error']
  if (row.immediate_pushed_at) return ['已推送', 'sent']
  if (row.digest_pushed_at) return ['历史摘要已推送', 'digest']
  if (row.push_eligible) return ['待推送', 'eligible']
  return ['未推送', 'blocked']
}

function TableRow({ row, selected, onSelect }) {
  const [time, day] = formatDate(row.sent_at)
  const [pushLabel, pushClass] = pushState(row)
  const isDuplicate = row.prefilter_reason_code === 'recent_exact_duplicate'
  const showSemantic = ['suppressed', 'suppressed_unverified_update', 'material_update', 'failed_open', 'representative_replaced', 'superseded'].includes(row.semantic_dedupe_status)
  const choose = () => onSelect(row)
  return (
    <tr
      className={selected ? 'selected' : ''}
      onClick={choose}
      onKeyDown={(event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault()
          choose()
        }
      }}
      tabIndex="0"
      aria-selected={selected}
    >
      <td className="time-cell"><strong>{time}</strong><span>{day}</span></td>
      <td className="message-cell">
        <span className="message-title">{row.ai_summary || row.text || '（无文字内容）'}</span>
        <span className="message-meta">
          <b>{row.chat_name || '未知来源'}</b>
          {row.source_type === 'rss' ? <span className="source-type-label">RSS</span> : null}
          {row.sender_name ? <span>· {row.sender_name}</span> : null}
          <time className="mobile-time">· {day} {time}</time>
          <span className={`category-label ${row.content_kind || row.ai_category || 'unclassified'}`}>
            {row.content_kind === 'community_signal'
              ? row.community_signal_type === 'product_review' ? '产品口碑' : '社区线索'
              : row.content_kind === 'benefit_deal'
                ? '福利羊毛'
                : (row.ai_category_label || categoryLabel(row.ai_category))}
          </span>
          {row.prefilter_status === 'filtered' ? (
            <span className="prefilter-label">{isDuplicate ? '重复过滤' : '前置过滤'}</span>
          ) : null}
          {showSemantic ? (
            <span className={`semantic-label ${row.semantic_dedupe_status}`}>{semanticDedupeLabel(row.semantic_dedupe_status)}</span>
          ) : null}
          {Number(row.similar_count) > 0 ? (
            <button
              className="similar-count-button"
              type="button"
              onClick={(event) => {
                event.stopPropagation()
                onSelect(row)
              }}
              aria-label={`查看 ${row.similar_count} 条相似资讯`}
            >
              相似 {row.similar_count}
            </button>
          ) : null}
        </span>
      </td>
      <td className="score-cell"><strong className={`score-value ${scoreTone(row.ai_score ?? row.score)}`}>{row.ai_score ?? '—'}</strong></td>
      <td className="delivery-cell"><span className={`delivery-state ${pushClass}`}><Send size={14} />{pushLabel}</span></td>
    </tr>
  )
}

export function MessageTable({ rows, total, selectedId, onSelect, page, pageSize, onPage, loading }) {
  const pageCount = Math.max(1, Math.ceil(total / pageSize))
  return (
    <section className="message-list-surface" aria-busy={loading}>
      <div className="table-scroll">
        <table className="message-table">
          <thead>
            <tr><th>时间</th><th>消息内容</th><th>AI 评分</th><th>推送状态</th></tr>
          </thead>
          <tbody>
            {rows.map((row) => <TableRow row={row} key={row.id} selected={selectedId === row.id} onSelect={onSelect} />)}
          </tbody>
        </table>
        {!loading && rows.length === 0 ? (
          <div className="quiet-empty"><strong>当前筛选下没有消息</strong><span>调整筛选条件后再试</span></div>
        ) : null}
        {loading ? <div className="quiet-loading">正在加载消息…</div> : null}
      </div>
      <footer className="list-footer">
        <span>共 {total} 条</span>
        <div className="pagination">
          <button type="button" disabled={page <= 1} onClick={() => onPage(page - 1)}>上一页</button>
          <strong>{page}</strong><span>/ {pageCount}</span>
          <button type="button" disabled={page >= pageCount} onClick={() => onPage(page + 1)}>下一页</button>
        </div>
      </footer>
    </section>
  )
}

export { pushState, scoreTone }
