import { useEffect, useState } from 'react'
import {
  ChevronLeft,
  ChevronRight,
  Clock3,
  ExternalLink,
  Filter,
  RefreshCw,
  Star,
  Tag,
  UserRound,
  X,
} from './Icons'
import { SectionTabs } from './SectionTabs'
import {
  categoryLabel,
  prefilterStatusLabel,
  pushGateLabel,
  reasoningEffortLabel,
  deliveryStateLabel,
  semanticDedupeLabel,
  semanticUpdateTypeLabel,
  notificationPrepareLabel,
  communitySignalLabel,
  benefitTypeLabel,
} from '../model'

const AI_STATUS = {
  not_analyzed: '历史消息未分析',
  disabled: '模型分析已关闭',
  unavailable: '配置不可用',
  pending: '分析中',
  queued: '已进入分析队列',
  processing: '分析处理中',
  retry: '等待自动重试',
  success: '分析成功',
  error: '分析失败',
  prefiltered: '本地前置过滤',
  filtered_non_information: '分类后过滤：非资讯',
}

const AI_ERRORS = {
  configuration: '配置不完整',
  busy: '请求繁忙',
  timeout: '响应超时',
  network_error: '网络不可用',
  rate_limited: '服务限流',
  upstream_error: '上游服务异常',
  request_rejected: '请求被拒绝',
  redirect_blocked: '重定向被阻止',
  response_too_large: '响应过大',
  invalid_response: '响应格式无效',
  interrupted: '进程中断',
  internal_error: '内部处理异常',
  model_disabled: '模型分析未启用',
  message_missing: '消息记录不存在',
}

const ERROR_STAGES = { classification: '分类阶段', scoring: '评分阶段', community: '社区线索阶段', benefit: '福利羊毛阶段' }
const DETAIL_TABS = [
  { id: 'overview', label: '概览' },
  { id: 'analysis', label: '分析' },
  { id: 'raw', label: '原始响应' },
]

function formatDateTime(value) {
  return value ? new Date(value).toLocaleString('zh-CN') : '—'
}

function AuditRow({ icon: Icon, title, summary, state, tone = '', expandable = false }) {
  return (
    <div className="audit-row">
      <Icon size={21} aria-hidden="true" />
      <span><strong>{title}</strong><small>{summary}</small></span>
      {state ? <b className={tone}>{state}</b> : null}
      {expandable ? <ChevronRight size={17} aria-hidden="true" /> : null}
    </div>
  )
}

export function Inspector({ row, onReanalyze, analyzing, onClose = () => {}, initialTab = 'overview' }) {
  const [activeTab, setActiveTab] = useState(initialTab)

  useEffect(() => {
    const closeOnEscape = (event) => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', closeOnEscape)
    return () => window.removeEventListener('keydown', closeOnEscape)
  }, [onClose])

  if (!row) return null
  const isProductReview = row.content_kind === 'community_signal' && row.community_signal_type === 'product_review'
  const category = row.content_kind === 'community_signal'
    ? isProductReview ? '产品口碑' : '社区线索'
    : row.content_kind === 'benefit_deal'
      ? '福利羊毛'
    : (row.ai_category_label || categoryLabel(row.ai_category))
  const headline = row.notification_title || row.ai_summary || row.text || '（无文字内容）'
  const isEligible = Boolean(row.push_eligible)
  const queueActive = ['queued', 'processing', 'retry', 'pending'].includes(row.ai_status)
  const isDuplicate = row.prefilter_reason_code === 'recent_exact_duplicate'
  const semanticSuppressed = [
    'suppressed',
    'suppressed_unverified_update',
    'superseded',
  ].includes(row.semantic_dedupe_status)
  const semanticPassed = [
    'unique_no_candidates',
    'unique',
    'material_update',
    'low_confidence_pass',
    'failed_open',
    'representative_replaced',
  ].includes(row.semantic_dedupe_status)
  const ntfyDelivered = (row.deliveries || []).some(
    (delivery) => delivery.channel === 'ntfy' && delivery.state === 'succeeded',
  )
  const feedbackLabel = row.ntfy_feedback?.vote === 'up'
    ? '👍 有用'
    : row.ntfy_feedback?.vote === 'down'
      ? '👎 无用'
      : '尚未反馈'
  const similarCluster = row.similar_cluster || { representative: null, similar_count: 0, items: [] }

  return (
    <aside className="detail-drawer" role="dialog" aria-modal="true" aria-labelledby="message-detail-title">
      <header className="drawer-header">
        <button className="drawer-back" type="button" onClick={onClose}><ChevronLeft size={18} />消息流</button>
        <h2 id="message-detail-title">消息详情</h2>
        <button className="icon-button" type="button" onClick={onClose} aria-label="关闭消息详情"><X size={20} /></button>
      </header>

      <div className="drawer-scroll">
        <section className="drawer-summary">
          <h3>{headline}</h3>
          {row.ai_summary && row.text && row.ai_summary !== row.text ? <p>{row.text}</p> : null}
          <div className="summary-meta">
            <span><UserRound size={15} />{row.chat_name || '未知来源'}{row.sender_name ? ` · ${row.sender_name}` : ''}</span>
            <span><Clock3 size={15} />{formatDateTime(row.sent_at)}</span>
            {row.link ? <a href={row.link} target="_blank" rel="noreferrer">查看原文 <ExternalLink size={14} /></a> : null}
          </div>
          <dl className="summary-facts">
            <div><dt>AI 评分</dt><dd>{row.ai_score ?? row.score ?? '—'}</dd></div>
            <div><dt>分类</dt><dd>{category}</dd></div>
            <div><dt>推送资格</dt><dd className={isEligible ? 'eligible' : 'blocked'}>{isEligible ? '具备资格' : '不具备资格'}</dd></div>
          </dl>
        </section>

        {similarCluster.similar_count > 0 ? (
          <section className="similar-cluster-panel" aria-label="相似资讯关联">
            <header><div><h3>相似资讯</h3><p>已归并到同一代表消息，不会重复推送。</p></div><strong>{similarCluster.similar_count} 条</strong></header>
            {similarCluster.representative && similarCluster.representative.id !== row.id ? (
              <div className="similar-representative">
                <span>已推送代表</span>
                <strong>{similarCluster.representative.notification_title || similarCluster.representative.ai_summary || similarCluster.representative.text || '（无文字内容）'}</strong>
                <small>{similarCluster.representative.chat_name || '未知来源'} · {formatDateTime(similarCluster.representative.sent_at)}</small>
              </div>
            ) : null}
            <div className="similar-items">
              {similarCluster.items.map((item) => (
                <details key={item.id}>
                  <summary>
                    <span><strong>{item.notification_title || item.ai_summary || item.text || '（无文字内容）'}</strong><small>{item.chat_name || '未知来源'} · {formatDateTime(item.sent_at)}</small></span>
                    <b>{item.ai_score == null ? '—' : `${item.ai_score} 分`}</b>
                  </summary>
                  <p>{item.text || '（无文字内容）'}</p>
                  <small>{semanticDedupeLabel(item.semantic_dedupe_status)}{item.semantic_dedupe_reason ? ` · ${item.semantic_dedupe_reason}` : ''}</small>
                </details>
              ))}
            </div>
          </section>
        ) : null}

        <button
          className="reanalyze-action"
          type="button"
          onClick={onReanalyze}
          disabled={analyzing || queueActive}
        >
          <RefreshCw size={16} className={analyzing || queueActive ? 'spin' : ''} />
          {analyzing ? '正在入队…' : queueActive ? '分析任务处理中…' : '用当前模型重新分析'}
        </button>

        <SectionTabs items={DETAIL_TABS} active={activeTab} onChange={setActiveTab} label="消息详情视图" className="drawer-tabs" />

        {activeTab === 'overview' ? (
          <section className="drawer-pane" aria-label="消息审计概览">
            <AuditRow
              icon={Filter}
              title={isDuplicate ? '内容去重' : '本地过滤'}
              summary={row.prefilter_reason || prefilterStatusLabel(row.prefilter_status)}
              state={isDuplicate ? '已去重' : row.prefilter_status === 'filtered' ? '已过滤' : '通过'}
              tone={row.prefilter_status === 'filtered' ? 'blocked' : 'eligible'}
            />
            <AuditRow
              icon={Tag}
              title="分类结论"
              summary={row.ai_category_reason || row.ai_category_summary || AI_STATUS[row.ai_status] || '尚无分类结论'}
              state={category}
              expandable
            />
            {ntfyDelivered || row.ntfy_feedback ? (
              <AuditRow
                icon={Star}
                title="ntfy 有用性反馈"
                summary={row.ntfy_feedback
                  ? `已于 ${formatDateTime(row.ntfy_feedback.voted_at)} 收到反馈`
                  : '通知已送达，等待用户选择'}
                state={feedbackLabel}
                tone={row.ntfy_feedback?.vote === 'up' ? 'eligible' : row.ntfy_feedback?.vote === 'down' ? 'blocked' : ''}
              />
            ) : null}
            {row.ai_category === 'discussion' ? (
              <AuditRow
                icon={Star}
                title={isProductReview ? '产品口碑聚合' : '社区线索提炼'}
                summary={row.community_reason || '当前讨论未形成可推送线索'}
                state={row.community_status === 'valuable' ? communitySignalLabel(row.community_signal_type) : '未入选'}
                tone={row.community_status === 'valuable' ? 'eligible' : 'blocked'}
                expandable
              />
            ) : null}
            {row.ai_category === 'promotion_spam' ? (
              <AuditRow
                icon={Star}
                title="福利羊毛筛选"
                summary={row.benefit_reason || '当前推广未形成可信、可执行的福利'}
                state={row.benefit_status === 'valuable' ? benefitTypeLabel(row.benefit_type) : '未入选'}
                tone={row.benefit_status === 'valuable' ? 'eligible' : 'blocked'}
                expandable
              />
            ) : null}
            <AuditRow
              icon={Filter}
              title="跨来源资讯去重"
              summary={row.semantic_dedupe_update_rejection_reason || row.semantic_dedupe_reason || semanticDedupeLabel(row.semantic_dedupe_status)}
              state={semanticDedupeLabel(row.semantic_dedupe_status)}
              tone={semanticSuppressed ? 'blocked' : semanticPassed ? 'eligible' : ''}
              expandable
            />
            <AuditRow
              icon={Star}
              title="评分理由"
              summary={row.ai_reason || '尚无资讯评分理由'}
              state={row.ai_score == null ? '—' : `${row.ai_score} 分`}
              expandable
            />
            <AuditRow
              icon={Tag}
              title="客户通知整理"
              summary={row.notification_prepare_status === 'failed_fallback'
                ? '模型整理失败，已冻结确定性清洗结果并继续投递'
                : (row.notification_title || notificationPrepareLabel(row.notification_prepare_status))}
              state={notificationPrepareLabel(row.notification_prepare_status)}
              tone={row.notification_prepare_status === 'success' ? 'eligible' : row.notification_prepare_status === 'failed_fallback' ? 'warning' : ''}
              expandable
            />
            <div className="gate-note">
              <strong>{isEligible ? `该消息具备${category}推送资格` : '该消息不会进入推送'}</strong>
              <span>{pushGateLabel(row.push_gate_reason)}</span>
            </div>
          </section>
        ) : null}

        {activeTab === 'analysis' ? (
          <section className="drawer-pane analysis-pane" aria-label="模型分析详情">
            <div className="analysis-status-line"><span>分析状态</span><strong>{AI_STATUS[row.ai_status] || '未分析'}</strong></div>
            <dl className="analysis-grid">
              <div><dt>本地规则评分</dt><dd>{row.local_score ?? row.base_score ?? '—'} 分</dd></div>
              <div><dt>本地规则原因</dt><dd>{(row.local_reasons || []).join('；') || '无额外规则命中'}</dd></div>
              <div><dt>分类模型</dt><dd>{row.ai_classification_model || '—'}</dd></div>
              <div><dt>分类档位</dt><dd>{reasoningEffortLabel(row.ai_classification_effort)}</dd></div>
              <div><dt>分类置信度</dt><dd>{row.ai_category_confidence == null ? '—' : `${row.ai_category_confidence}%`}</dd></div>
              <div><dt>分类摘要</dt><dd>{row.ai_category_summary || '—'}</dd></div>
              <div><dt>分类理由</dt><dd>{row.ai_category_reason || '—'}</dd></div>
              <div><dt>评分模型</dt><dd>{row.ai_model || '—'}</dd></div>
              <div><dt>评分档位</dt><dd>{reasoningEffortLabel(row.ai_scoring_effort)}</dd></div>
              <div><dt>评分摘要</dt><dd>{row.ai_summary || '—'}</dd></div>
              <div><dt>评分理由</dt><dd>{row.ai_reason || '—'}</dd></div>
              <div><dt>反馈校准样本</dt><dd>{row.feedback_context_sample_count ?? 0} 条</dd></div>
              <div><dt>反馈校准说明</dt><dd>{row.feedback_context_summary || '分析时暂无相关反馈，未做偏好校准'}</dd></div>
              {row.feedback_context_applied_at ? <div><dt>反馈校准时间</dt><dd>{formatDateTime(row.feedback_context_applied_at)}</dd></div> : null}
              {row.ai_category === 'discussion' ? (
                <>
                  <div><dt>社区线索状态</dt><dd>{row.community_status === 'valuable' ? '已提炼' : row.community_status === 'error' ? '分析失败' : '普通讨论'}</dd></div>
                  <div><dt>线索类型</dt><dd>{communitySignalLabel(row.community_signal_type)}</dd></div>
                  <div><dt>线索模型</dt><dd>{row.community_model || '—（未调用）'}</dd></div>
                  <div><dt>线索档位</dt><dd>{reasoningEffortLabel(row.community_effort)}</dd></div>
                  <div><dt>线索置信度</dt><dd>{row.community_confidence == null ? '—' : `${row.community_confidence}%`}</dd></div>
                  <div><dt>支持消息</dt><dd>{row.community_evidence_count == null ? '—' : `${row.community_evidence_count} 条`}</dd></div>
                  <div><dt>线索摘要</dt><dd>{row.community_summary || '—'}</dd></div>
                  <div><dt>线索理由</dt><dd>{row.community_reason || '—'}</dd></div>
                  <div><dt>线索完成时间</dt><dd>{formatDateTime(row.community_checked_at)}</dd></div>
                </>
              ) : null}
              {row.ai_category === 'promotion_spam' ? (
                <>
                  <div><dt>福利筛选状态</dt><dd>{row.benefit_status === 'valuable' ? '已入选' : row.benefit_status === 'error' ? '分析失败' : '普通推广'}</dd></div>
                  <div><dt>福利类型</dt><dd>{benefitTypeLabel(row.benefit_type)}</dd></div>
                  <div><dt>福利模型</dt><dd>{row.benefit_model || '—（未调用）'}</dd></div>
                  <div><dt>福利档位</dt><dd>{reasoningEffortLabel(row.benefit_effort)}</dd></div>
                  <div><dt>福利置信度</dt><dd>{row.benefit_confidence == null ? '—' : `${row.benefit_confidence}%`}</dd></div>
                  <div><dt>福利标题</dt><dd>{row.benefit_title || '—'}</dd></div>
                  <div><dt>领取条件摘要</dt><dd>{row.benefit_summary || '—'}</dd></div>
                  <div><dt>筛选理由</dt><dd>{row.benefit_reason || '—'}</dd></div>
                  <div><dt>福利分析时间</dt><dd>{formatDateTime(row.benefit_checked_at)}</dd></div>
                </>
              ) : null}
              <div><dt>跨来源去重</dt><dd>{semanticDedupeLabel(row.semantic_dedupe_status)}</dd></div>
              <div><dt>去重模型</dt><dd>{row.semantic_dedupe_model || '—（未调用）'}</dd></div>
              <div><dt>去重档位</dt><dd>{reasoningEffortLabel(row.semantic_dedupe_effort)}</dd></div>
              <div><dt>去重置信度</dt><dd>{row.semantic_dedupe_confidence == null ? '—' : `${row.semantic_dedupe_confidence}%`}</dd></div>
              <div><dt>比较候选</dt><dd>{row.semantic_dedupe_candidate_count ?? 0} 条（最近 24 小时）</dd></div>
              <div><dt>匹配代表消息</dt><dd>{row.semantic_dedupe_matched_message_id ? `#${row.semantic_dedupe_matched_message_id}` : '—'}</dd></div>
              <div><dt>更新类型</dt><dd>{semanticUpdateTypeLabel(row.semantic_dedupe_update_type)}</dd></div>
              <div><dt>状态变化证据</dt><dd>{row.semantic_dedupe_update_type == null ? '—' : row.semantic_dedupe_update_validated ? '已通过程序校验' : '未通过程序校验'}</dd></div>
              {row.semantic_dedupe_update_rejection_reason ? <div><dt>抑制原因</dt><dd>{row.semantic_dedupe_update_rejection_reason}</dd></div> : null}
              <div><dt>去重结论</dt><dd>{row.semantic_dedupe_reason || '—'}</dd></div>
              {row.semantic_dedupe_error_category ? <div><dt>去重错误</dt><dd>{AI_ERRORS[row.semantic_dedupe_error_category] || '检查失败，已安全放行'}</dd></div> : null}
              <div><dt>去重完成时间</dt><dd>{formatDateTime(row.semantic_dedupe_checked_at)}</dd></div>
              <div><dt>通知整理状态</dt><dd>{notificationPrepareLabel(row.notification_prepare_status)}</dd></div>
              <div><dt>通知整理模型</dt><dd>{row.notification_prepare_model || '—（未调用）'}</dd></div>
              <div><dt>通知整理档位</dt><dd>{reasoningEffortLabel(row.notification_prepare_effort)}</dd></div>
              <div><dt>整理完成时间</dt><dd>{formatDateTime(row.notification_prepare_checked_at)}</dd></div>
              {row.notification_prepare_error_category ? <div><dt>整理错误</dt><dd>{AI_ERRORS[row.notification_prepare_error_category] || '整理失败，已安全回退'}</dd></div> : null}
              <div><dt>回复人数</dt><dd>{row.reply_count ?? 0} 人（仅审计）</dd></div>
              <div><dt>ntfy 用户反馈</dt><dd>{feedbackLabel}</dd></div>
              {row.ntfy_feedback?.voted_at ? <div><dt>反馈时间</dt><dd>{formatDateTime(row.ntfy_feedback.voted_at)}</dd></div> : null}
              {row.ai_error_stage ? <div><dt>错误阶段</dt><dd>{ERROR_STAGES[row.ai_error_stage] || '分析阶段'}</dd></div> : null}
              {row.ai_error_category ? <div><dt>错误类别</dt><dd>{AI_ERRORS[row.ai_error_category] || '分析未完成'}</dd></div> : null}
              <div><dt>完成时间</dt><dd>{formatDateTime(row.ai_completed_at)}</dd></div>
              <div><dt>队列状态</dt><dd>{row.analysis_queue_state || '—'}</dd></div>
              <div><dt>任务尝试</dt><dd>{row.analysis_queue_attempts ?? 0} 次</dd></div>
              {row.analysis_queue_error_category ? (
                <div><dt>队列错误类别</dt><dd>{AI_ERRORS[row.analysis_queue_error_category] || row.analysis_queue_error_category}</dd></div>
              ) : null}
              {row.analysis_queue_available_at ? <div><dt>下次尝试</dt><dd>{formatDateTime(row.analysis_queue_available_at)}</dd></div> : null}
            </dl>
            {row.notification_title || row.notification_body ? (
              <section className="prepared-notification-preview" aria-label="最终客户通知预览">
                <h3>最终客户通知预览</h3>
                <strong>{row.notification_title || '重要资讯提醒'}</strong>
                <small>来源：{row.chat_name || '未知来源'}</small>
                <p>{row.notification_body || '—'}</p>
              </section>
            ) : null}
            <section className="delivery-audit" aria-label="逐渠道投递状态">
              <h3>逐渠道投递</h3>
              {(row.deliveries || []).map((delivery) => (
                <div className="delivery-audit-row" key={`${delivery.delivery_type}-${delivery.channel}`}>
                  <span><strong>{delivery.channel === 'telegram' ? 'Telegram' : 'ntfy'}</strong><small>{delivery.delivery_type === 'immediate' ? '实时推送' : '历史摘要'}</small></span>
                  <b className={delivery.state}>{deliveryStateLabel(delivery.state)}</b>
                  <small>尝试 {delivery.attempts}/{delivery.max_attempts}{delivery.error_category ? ` · ${delivery.error_category}` : ''}</small>
                </div>
              ))}
              {(row.deliveries || []).length === 0 ? <p className="quiet-note">尚未创建渠道投递任务。</p> : null}
            </section>
          </section>
        ) : null}

        {activeTab === 'raw' ? (
          <section className="drawer-pane raw-pane" aria-label="模型原始响应">
            <div><h3>分类原始响应</h3>{row.ai_category_response_text ? <pre>{row.ai_category_response_text}</pre> : <p>无分类原始响应。</p>}</div>
            <div><h3>评分原始响应</h3>{row.ai_response_text ? <pre>{row.ai_response_text}</pre> : <p>无评分原始响应。</p>}</div>
            <div><h3>社区线索原始响应</h3>{row.community_response_text ? <pre>{row.community_response_text}</pre> : <p>未调用社区线索模型或无原始响应。</p>}</div>
            <div><h3>福利羊毛原始响应</h3>{row.benefit_response_text ? <pre>{row.benefit_response_text}</pre> : <p>未调用福利筛选模型或无原始响应。</p>}</div>
            <div><h3>跨来源去重原始响应</h3>{row.semantic_dedupe_response_text ? <pre>{row.semantic_dedupe_response_text}</pre> : <p>未调用去重模型或无原始响应。</p>}</div>
            <div><h3>通知内容整理原始响应</h3>{row.notification_prepare_response_text ? <pre>{row.notification_prepare_response_text}</pre> : <p>未调用整理模型或无原始响应。</p>}</div>
          </section>
        ) : null}
      </div>
    </aside>
  )
}

export { AI_STATUS, DETAIL_TABS }
