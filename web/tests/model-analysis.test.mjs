import assert from 'node:assert/strict'
import test, { after } from 'node:test'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

const vite = await createServer({
  appType: 'custom',
  logLevel: 'silent',
  server: { middlewareMode: true },
})
const {
  ModelAnalysisSettings,
  isModelConfigDirty,
} = await vite.ssrLoadModule('/src/components/ModelAnalysisSettings.jsx')
const { MessageTable } = await vite.ssrLoadModule('/src/components/MessageTable.jsx')
const { Inspector } = await vite.ssrLoadModule('/src/components/Inspector.jsx')

after(async () => {
  await vite.close()
})

function render({ configured = false, status = { kind: 'idle', message: '' } } = {}) {
  return renderToStaticMarkup(React.createElement(ModelAnalysisSettings, {
    form: {
      enabled: false,
      communityInsightsEnabled: true,
      benefitDealsEnabled: true,
      baseUrl: 'https://model.example.com/v1',
      apiKey: '',
      clearApiKey: false,
      classificationModel: 'gemini-3.5-flash-extra-low',
      classificationReasoningEffort: 'low',
      model: '',
      reasoningEffort: 'default',
      semanticDedupeModel: 'gemini-3.5-flash-extra-low',
      semanticDedupeReasoningEffort: 'low',
      notificationModel: 'gemini-3.5-flash-extra-low',
      notificationReasoningEffort: 'low',
    },
    config: {
      enabled: false,
      community_insights_enabled: true,
      benefit_deals_enabled: true,
      base_url: 'https://model.example.com/v1',
      api_key_configured: configured,
      classification_model: 'gemini-3.5-flash-extra-low',
      classification_reasoning_effort: 'low',
      model: null,
      reasoning_effort: 'default',
      semantic_dedupe_model: 'gemini-3.5-flash-extra-low',
      semantic_dedupe_reasoning_effort: 'low',
      notification_model: 'gemini-3.5-flash-extra-low',
      notification_reasoning_effort: 'low',
    },
    models: ['model-a', 'model-z'],
    status,
    onChange() {},
    onRefresh() {},
    onSave() {},
    refreshing: status.kind === 'loading',
    saving: false,
  }))
}

test('renders an unconfigured state and a password-only key field', () => {
  const html = render()
  assert.match(html, />未配置</)
  assert.match(html, /type="password"/)
  assert.match(html, /尚未保存 API Key/)
  assert.doesNotMatch(html, /value="[^\"]+"[^>]*aria-label="模型 API Key"/)
})

test('configured state reports saved without rendering a stored key', () => {
  const html = render({ configured: true })
  assert.match(html, />已保存</)
  assert.match(html, /页面不会回读明文/)
  assert.match(html, /placeholder="已配置；留空保持不变"/)
})

test('renders loading, success, and error feedback states', () => {
  assert.match(render({ status: { kind: 'loading', message: '正在加载' } }), /加载中/)
  assert.match(render({ status: { kind: 'success', message: '刷新完成' } }), /刷新成功/)
  assert.match(render({ status: { kind: 'error', message: '安全错误提示' } }), />错误</)
})

test('reasoning effort participates in dirty state and explains both stages', () => {
  const cleanForm = {
    enabled: true,
    communityInsightsEnabled: true,
    benefitDealsEnabled: true,
    baseUrl: 'http://model.test/v1',
    apiKey: '',
    clearApiKey: false,
    classificationModel: 'gemini-3.5-flash-extra-low',
    classificationReasoningEffort: 'low',
    model: 'model-a',
    reasoningEffort: 'default',
    semanticDedupeModel: 'model-z',
    semanticDedupeReasoningEffort: 'medium',
    notificationModel: 'model-a',
    notificationReasoningEffort: 'high',
  }
  const config = {
    enabled: true,
    community_insights_enabled: true,
    benefit_deals_enabled: true,
    base_url: 'http://model.test/v1',
    api_key_configured: true,
    classification_model: 'gemini-3.5-flash-extra-low',
    classification_reasoning_effort: 'low',
    model: 'model-a',
    reasoning_effort: 'default',
    semantic_dedupe_model: 'model-z',
    semantic_dedupe_reasoning_effort: 'medium',
    notification_model: 'model-a',
    notification_reasoning_effort: 'high',
  }
  assert.equal(isModelConfigDirty(cleanForm, config), false)
  assert.equal(isModelConfigDirty({ ...cleanForm, classificationModel: 'model-z' }, config), true)
  assert.equal(isModelConfigDirty({ ...cleanForm, communityInsightsEnabled: false }, config), true)
  assert.equal(isModelConfigDirty({ ...cleanForm, benefitDealsEnabled: false }, config), true)
  assert.equal(isModelConfigDirty({ ...cleanForm, classificationReasoningEffort: 'high' }, config), true)
  assert.equal(isModelConfigDirty({ ...cleanForm, reasoningEffort: 'high' }, config), true)
  assert.equal(isModelConfigDirty({ ...cleanForm, semanticDedupeModel: 'model-a' }, config), true)
  assert.equal(isModelConfigDirty({ ...cleanForm, notificationReasoningEffort: 'low' }, config), true)
  const html = render()
  assert.match(html, /aria-label="分类分析模型选择"/)
  assert.match(html, /gemini-3.5-flash-extra-low/)
  assert.match(html, /aria-label="分类分析推理档位"/)
  assert.match(html, /aria-label="评分分析模型选择"/)
  assert.match(html, /aria-label="评分分析推理档位"/)
  assert.match(html, /aria-label="语义去重模型选择"/)
  assert.match(html, /aria-label="语义去重推理档位"/)
  assert.match(html, /aria-label="通知内容整理模型选择"/)
  assert.match(html, /aria-label="通知内容整理推理档位"/)
  assert.match(html, />模型默认</)
  assert.match(html, />低</)
  assert.match(html, />中</)
  assert.match(html, />高</)
  assert.match(html, /默认低推理；非资讯在此结束/)
  assert.match(html, /启用社区线索分析/)
  assert.match(html, /产品口碑/)
  assert.match(html, /达到 60 分后同样实时推送/)
  assert.match(html, /启用福利羊毛分析/)
  assert.match(html, /AI 分是唯一推送判断分数/)
})

test('message list and detail render classification and two-stage results', () => {
  const row = {
    id: 7,
    sent_at: '2026-01-01T00:00:00Z',
    score: 88,
    text: '测试消息',
    chat_name: '测试群',
    sender_name: '测试发送者',
    reasons: ['维护 +25'],
    local_reasons: ['维护 +25'],
    local_score: 25,
    reply_count: 0,
    reply_bonus: 0,
    ai_status: 'success',
    ai_classification_model: 'classifier-model',
    ai_model: 'test-model',
    ai_score: 88,
    ai_summary: '评分摘要',
    ai_reason: '评分理由',
    feedback_context_sample_count: 4,
    feedback_context_summary: '近 90 天相关反馈 4 条；新闻资讯 3 赞/1 踩',
    feedback_context_applied_at: '2026-01-01T00:00:30Z',
    ai_response_text: '评分原始响应',
    ai_category: 'external_information',
    ai_category_label: '外部资讯',
    ai_category_confidence: 97,
    ai_category_summary: '分类摘要',
    ai_category_reason: '分类理由',
    ai_category_response_text: '分类原始响应',
    ai_classification_effort: 'low',
    ai_scoring_effort: 'high',
    prefilter_status: 'passed',
    prefilter_reason_code: null,
    prefilter_reason: null,
    push_eligible: true,
    push_gate_reason: 'semantic_material_update',
    semantic_dedupe_status: 'material_update',
    semantic_dedupe_model: 'classifier-model',
    semantic_dedupe_effort: 'medium',
    semantic_dedupe_confidence: 96,
    semantic_dedupe_reason: '同一事件出现恢复状态，属于实质更新',
    semantic_dedupe_response_text: '{"same_event":true,"material_update":true,"update_type":"service_status_change"}',
    semantic_dedupe_matched_message_id: 5,
    semantic_dedupe_material_update: true,
    semantic_dedupe_update_type: 'service_status_change',
    semantic_dedupe_update_validated: true,
    semantic_dedupe_update_rejection_reason: null,
    semantic_dedupe_checked_at: '2026-01-01T00:01:00Z',
    semantic_dedupe_error_category: null,
    semantic_dedupe_candidate_count: 3,
    notification_prepare_status: 'success',
    notification_prepare_model: 'notification-model',
    notification_prepare_effort: 'high',
    notification_title: '整理后的客户标题',
    notification_body: '第一段客户正文\n\n第二段客户正文',
    notification_prepare_response_text: '{"title":"整理后的客户标题","body":"客户正文"}',
    notification_prepare_checked_at: '2026-01-01T00:01:02Z',
    notification_prepare_error_category: null,
    analysis_queue_state: 'succeeded',
    analysis_queue_attempts: 2,
    deliveries: [
      {
        channel: 'telegram',
        delivery_type: 'immediate',
        state: 'succeeded',
        attempts: 1,
        max_attempts: 5,
        error_category: null,
      },
      {
        channel: 'ntfy',
        delivery_type: 'immediate',
        state: 'retry',
        attempts: 2,
        max_attempts: 5,
        error_category: 'network_error',
      },
    ],
    ntfy_feedback: {
      vote: 'up',
      voted_at: '2026-01-01T00:02:00Z',
    },
  }
  const table = renderToStaticMarkup(React.createElement(MessageTable, {
    rows: [row],
    total: 1,
    selectedId: 7,
    onSelect() {},
    page: 1,
    pageSize: 20,
    onPage() {},
    loading: false,
  }))
  assert.match(table, /外部资讯/)
  assert.match(table, /实质更新已放行/)
  const overview = renderToStaticMarkup(React.createElement(Inspector, {
    row,
    onReanalyze() {},
    analyzing: false,
  }))
  assert.match(overview, /消息详情/)
  assert.match(overview, /外部资讯/)
  assert.match(overview, /具备外部资讯推送资格/)
  assert.match(overview, /跨来源资讯去重/)
  assert.match(overview, /同一事件出现恢复状态/)
  assert.match(overview, /ntfy 有用性反馈/)
  assert.match(overview, /👍 有用/)
  assert.doesNotMatch(overview, /分类原始响应/)

  const detail = renderToStaticMarkup(React.createElement(Inspector, {
    row,
    onReanalyze() {},
    analyzing: false,
    initialTab: 'analysis',
  }))
  assert.match(detail, /97%/)
  assert.match(detail, /分类摘要/)
  assert.match(detail, /分类理由/)
  assert.match(detail, /分类档位/)
  assert.match(detail, /classifier-model/)
  assert.match(detail, />低</)
  assert.match(detail, /评分模型/)
  assert.match(detail, /test-model/)
  assert.match(detail, /评分档位/)
  assert.match(detail, />高</)
  assert.match(detail, /评分摘要/)
  assert.match(detail, /反馈校准样本/)
  assert.match(detail, /4 条/)
  assert.match(detail, /新闻资讯 3 赞\/1 踩/)
  assert.match(detail, /跨来源去重/)
  assert.match(detail, /96%/)
  assert.match(detail, /#5/)
  assert.match(detail, /最近 24 小时/)
  assert.match(detail, /去重档位/)
  assert.match(detail, /服务状态变化/)
  assert.match(detail, /已通过程序校验/)
  assert.match(detail, /通知整理状态/)
  assert.match(detail, /notification-model/)
  assert.match(detail, /最终客户通知预览/)
  assert.match(detail, /整理后的客户标题/)
  assert.match(detail, /第二段客户正文/)
  assert.match(detail, /本地规则评分/)
  assert.match(detail, /回复人数/)
  assert.match(detail, /Telegram/)
  assert.match(detail, /ntfy/)
  assert.match(detail, /等待重试/)
  assert.match(detail, /network_error/)
  assert.match(detail, /ntfy 用户反馈/)
  assert.match(detail, /👍 有用/)

  const raw = renderToStaticMarkup(React.createElement(Inspector, {
    row,
    onReanalyze() {},
    analyzing: false,
    initialTab: 'raw',
  }))
  assert.match(raw, /分类原始响应/)
  assert.match(raw, /评分原始响应/)
  assert.match(raw, /跨来源去重原始响应/)
  assert.match(raw, /通知内容整理原始响应/)
  assert.match(raw, /material_update/)
})

test('community signal is distinct and exposes its audit fields', () => {
  const row = {
    id: 71,
    sent_at: '2026-01-01T00:00:00Z',
    text: '讨论中的去标识化技术观察',
    chat_name: '技术讨论群',
    ai_status: 'success',
    ai_category: 'discussion',
    ai_category_label: '讨论交流',
    content_kind: 'community_signal',
    ai_score: 76,
    ai_summary: '连接异常已有解决方法',
    ai_reason: '同一话题包含问题、步骤和结果',
    community_status: 'valuable',
    community_signal_type: 'technical_solution',
    community_confidence: 90,
    community_title: '连接异常已有解决方法',
    community_summary: '调整配置后连接恢复。',
    community_reason: '包含可执行步骤和明确结果',
    community_model: 'community-model',
    community_effort: 'medium',
    community_evidence_count: 2,
    community_checked_at: '2026-01-01T00:01:00Z',
    prefilter_status: 'passed',
    semantic_dedupe_status: 'unique_no_candidates',
    notification_prepare_status: 'success',
    notification_title: '连接异常已有解决方法',
    notification_body: '调整配置后连接恢复。',
    push_eligible: true,
    push_gate_reason: 'eligible_notification_prepared',
    deliveries: [],
  }
  const table = renderToStaticMarkup(React.createElement(MessageTable, {
    rows: [row], total: 1, selectedId: null, onSelect() {}, page: 1,
    pageSize: 20, onPage() {}, loading: false,
  }))
  assert.match(table, /社区线索/)
  const detail = renderToStaticMarkup(React.createElement(Inspector, {
    row, onReanalyze() {}, analyzing: false, initialTab: 'analysis',
  }))
  assert.match(detail, /有效解决方案/)
  assert.match(detail, /community-model/)
  assert.match(detail, /支持消息/)
  assert.match(detail, /2 条/)
})

test('product reputation is labeled distinctly in the list and audit detail', () => {
  const row = {
    id: 73,
    sent_at: '2026-01-01T00:00:00Z',
    text: '去标识化的多人产品评价',
    chat_name: '技术讨论群',
    ai_status: 'success',
    ai_category: 'discussion',
    ai_category_label: '讨论交流',
    content_kind: 'community_signal',
    ai_score: 72,
    ai_summary: '某产品稳定性较好但售后体验有分歧',
    ai_reason: '同一产品有多人具体体验',
    community_status: 'valuable',
    community_signal_type: 'product_review',
    community_confidence: 91,
    community_title: '某产品稳定性较好但售后体验有分歧',
    community_summary: '多位参与者给出了具体优缺点。',
    community_reason: '包含两条独立使用证据',
    community_model: 'community-model',
    community_effort: 'medium',
    community_evidence_count: 2,
    community_checked_at: '2026-01-01T00:01:00Z',
    prefilter_status: 'passed',
    semantic_dedupe_status: 'unique_no_candidates',
    notification_prepare_status: 'success',
    notification_title: '某产品稳定性较好但售后体验有分歧',
    notification_body: '多位参与者给出了具体优缺点。',
    push_eligible: true,
    push_gate_reason: 'eligible_notification_prepared',
    deliveries: [],
  }
  const table = renderToStaticMarkup(React.createElement(MessageTable, {
    rows: [row], total: 1, selectedId: null, onSelect() {}, page: 1,
    pageSize: 20, onPage() {}, loading: false,
  }))
  assert.match(table, /产品口碑/)
  const overview = renderToStaticMarkup(React.createElement(Inspector, {
    row, onReanalyze() {}, analyzing: false,
  }))
  assert.match(overview, /产品口碑聚合/)
  assert.match(overview, /具备产品口碑推送资格/)
  const detail = renderToStaticMarkup(React.createElement(Inspector, {
    row, onReanalyze() {}, analyzing: false, initialTab: 'analysis',
  }))
  assert.match(detail, /产品口碑/)
  assert.match(detail, /2 条/)
})

test('benefit deal is distinct and exposes customer and audit fields', () => {
  const row = {
    id: 72,
    sent_at: '2026-01-01T00:00:00Z',
    text: '去标识化优惠消息',
    chat_name: '优惠来源',
    ai_status: 'success',
    ai_category: 'promotion_spam',
    ai_category_label: '推广/垃圾',
    content_kind: 'benefit_deal',
    ai_score: 82,
    ai_summary: '开发工具开放限时免费额度',
    ai_reason: '对象、额度和期限明确',
    benefit_status: 'valuable',
    benefit_type: 'official_freebie',
    benefit_confidence: 92,
    benefit_title: '开发工具开放限时免费额度',
    benefit_summary: '符合条件的新用户可领取免费额度。',
    benefit_reason: '福利与领取条件明确',
    benefit_model: 'benefit-model',
    benefit_effort: 'medium',
    benefit_checked_at: '2026-01-01T00:01:00Z',
    prefilter_status: 'passed',
    semantic_dedupe_status: 'unique_no_candidates',
    notification_prepare_status: 'success',
    notification_title: '开发工具开放限时免费额度',
    notification_body: '符合条件的新用户可领取免费额度。',
    push_eligible: true,
    push_gate_reason: 'eligible_notification_prepared',
    deliveries: [],
  }
  const table = renderToStaticMarkup(React.createElement(MessageTable, {
    rows: [row], total: 1, selectedId: null, onSelect() {}, page: 1,
    pageSize: 20, onPage() {}, loading: false,
  }))
  assert.match(table, /福利羊毛/)
  const detail = renderToStaticMarkup(React.createElement(Inspector, {
    row, onReanalyze() {}, analyzing: false, initialTab: 'analysis',
  }))
  assert.match(detail, /官方限免\/免费额度/)
  assert.match(detail, /benefit-model/)
  assert.match(detail, /领取条件摘要/)
})

test('product restock benefit has a specific customer-facing audit label', () => {
  const row = {
    id: 73,
    sent_at: '2026-01-01T00:00:00Z',
    text: '去标识化产品补货消息',
    chat_name: '补货来源',
    ai_status: 'success',
    ai_category: 'promotion_spam',
    ai_category_label: '推广/垃圾',
    content_kind: 'benefit_deal',
    ai_score: 74,
    benefit_status: 'valuable',
    benefit_type: 'product_restock',
    benefit_confidence: 95,
    benefit_title: '云服务器套餐恢复下单',
    benefit_summary: '指定地区与系列已经补货。',
    benefit_reason: '产品和库存变化明确',
    prefilter_status: 'passed',
    semantic_dedupe_status: 'unique_no_candidates',
    notification_prepare_status: 'success',
    push_eligible: true,
    push_gate_reason: 'eligible_notification_prepared',
    deliveries: [],
  }
  const detail = renderToStaticMarkup(React.createElement(Inspector, {
    row, onReanalyze() {}, analyzing: false, initialTab: 'analysis',
  }))
  assert.match(detail, /商品补货\/恢复下单/)
})

test('message audit clearly shows a suppressed similar news item', () => {
  const row = {
    id: 8,
    sent_at: '2026-01-01T00:02:00Z',
    score: 82,
    text: '同一事件的另一来源转述',
    chat_name: '去标识来源',
    sender_name: '匿名',
    local_reasons: [],
    local_score: 0,
    ai_status: 'success',
    ai_category: 'external_information',
    ai_category_label: '外部资讯',
    ai_score: 82,
    ai_summary: '另一来源转述同一事件',
    ai_reason: '高相关且时效强',
    prefilter_status: 'passed',
    push_eligible: false,
    push_gate_reason: 'semantic_duplicate_suppressed',
    semantic_dedupe_status: 'suppressed',
    semantic_dedupe_confidence: 98,
    semantic_dedupe_reason: '与代表消息描述同一事件且没有新增状态',
    semantic_dedupe_matched_message_id: 7,
    semantic_dedupe_candidate_count: 2,
    deliveries: [],
  }
  const table = renderToStaticMarkup(React.createElement(MessageTable, {
    rows: [row], total: 1, selectedId: null, onSelect() {}, page: 1,
    pageSize: 20, onPage() {}, loading: false,
  }))
  assert.match(table, /相似资讯已抑制/)
  const detail = renderToStaticMarkup(React.createElement(Inspector, {
    row, onReanalyze() {}, analyzing: false,
  }))
  assert.match(detail, /不具备资格/)
  assert.match(detail, /同一事件已有代表消息/)
})

test('representative message shows grouped similar count and expandable details', () => {
  const duplicate = {
    id: 32,
    sent_at: '2026-01-01T00:05:00Z',
    text: '另一来源对同一事件的转述正文',
    chat_name: '来源乙',
    ai_score: 81,
    ai_summary: '同一事件的另一表述',
    semantic_dedupe_status: 'suppressed',
    semantic_dedupe_reason: '没有新增状态变化',
  }
  const row = {
    id: 31,
    sent_at: '2026-01-01T00:01:00Z',
    text: '代表资讯正文',
    chat_name: '来源甲',
    ai_status: 'success',
    ai_category: 'external_information',
    ai_score: 84,
    ai_summary: '代表资讯标题',
    prefilter_status: 'passed',
    push_eligible: true,
    semantic_dedupe_status: 'unique',
    similar_count: 1,
    similar_cluster: {
      representative: { id: 31, ai_summary: '代表资讯标题', chat_name: '来源甲' },
      similar_count: 1,
      items: [duplicate],
    },
    deliveries: [],
  }
  const table = renderToStaticMarkup(React.createElement(MessageTable, {
    rows: [row], total: 1, selectedId: null, onSelect() {}, page: 1,
    pageSize: 20, onPage() {}, loading: false,
  }))
  assert.match(table, /查看 1 条相似资讯/)
  assert.match(table, /相似 1/)

  const detail = renderToStaticMarkup(React.createElement(Inspector, {
    row, onReanalyze() {}, analyzing: false,
  }))
  assert.match(detail, /已归并到同一代表消息，不会重复推送/)
  assert.match(detail, /另一来源对同一事件的转述正文/)
  assert.match(detail, /没有新增状态变化/)
})

test('message audit exposes a claimed update rejected by deterministic evidence', () => {
  const row = {
    id: 18,
    sent_at: '2026-01-01T00:03:00Z',
    score: 85,
    text: '同一产品发布事件增加更多技术参数',
    chat_name: '去标识来源',
    sender_name: '匿名',
    local_reasons: [],
    local_score: 0,
    ai_status: 'success',
    ai_category: 'external_information',
    ai_category_label: '外部资讯',
    ai_score: 85,
    ai_summary: '同一发布事件的补充细节',
    ai_reason: '资讯评分成功',
    prefilter_status: 'passed',
    push_eligible: false,
    push_gate_reason: 'semantic_unverified_update_suppressed',
    semantic_dedupe_status: 'suppressed_unverified_update',
    semantic_dedupe_confidence: 96,
    semantic_dedupe_reason: '模型识别到同一事件，但所称更新缺少可验证状态变化',
    semantic_dedupe_matched_message_id: 7,
    semantic_dedupe_update_type: 'impact_status_change',
    semantic_dedupe_update_validated: false,
    semantic_dedupe_update_rejection_reason: '只有影响细节补充，没有实际影响状态变化',
    semantic_dedupe_candidate_count: 2,
    deliveries: [],
  }
  const table = renderToStaticMarkup(React.createElement(MessageTable, {
    rows: [row], total: 1, selectedId: null, onSelect() {}, page: 1,
    pageSize: 20, onPage() {}, loading: false,
  }))
  assert.match(table, /补充细节已抑制/)
  const detail = renderToStaticMarkup(React.createElement(Inspector, {
    row, onReanalyze() {}, analyzing: false, initialTab: 'analysis',
  }))
  assert.match(detail, /不具备资格/)
  assert.match(detail, /实际影响状态变化/)
  assert.match(detail, /未通过程序校验/)
  assert.match(detail, /只有影响细节补充/)
})

test('detail renders prefilter and non-information audit states', () => {
  const filtered = {
    id: 9,
    sent_at: '2026-01-01T00:00:00Z',
    score: 25,
    text: '去标识化自动流程样例',
    chat_name: '测试群',
    sender_name: '测试发送者',
    reasons: [],
    local_reasons: [],
    local_score: 25,
    reply_count: 0,
    reply_bonus: 0,
    ai_status: 'prefiltered',
    prefilter_status: 'filtered',
    prefilter_reason_code: 'welcome_verification',
    prefilter_reason: '入群欢迎或验证自动流程',
    push_eligible: false,
    push_gate_reason: 'prefiltered',
  }
  const detail = renderToStaticMarkup(React.createElement(Inspector, {
    row: filtered,
    onReanalyze() {},
    analyzing: false,
  }))
  assert.match(detail, /本地前置过滤/)
  assert.match(detail, /入群欢迎或验证自动流程/)
  assert.match(detail, /不具备资格/)

  const duplicate = {
    ...filtered,
    id: 11,
    prefilter_reason_code: 'recent_exact_duplicate',
    prefilter_reason: '同群 72 小时内已处理过相同内容',
  }
  const duplicateTable = renderToStaticMarkup(React.createElement(MessageTable, {
    rows: [duplicate],
    total: 1,
    selectedId: null,
    onSelect() {},
    page: 1,
    pageSize: 20,
    onPage() {},
    loading: false,
  }))
  assert.match(duplicateTable, /重复过滤/)
  const duplicateDetail = renderToStaticMarkup(React.createElement(Inspector, {
    row: duplicate,
    onReanalyze() {},
    analyzing: false,
  }))
  assert.match(duplicateDetail, /内容去重/)
  assert.match(duplicateDetail, /已去重/)
  assert.match(duplicateDetail, /同群 72 小时内已处理过相同内容/)

  const nonInformation = {
    ...filtered,
    id: 10,
    ai_status: 'filtered_non_information',
    prefilter_status: 'passed',
    prefilter_reason_code: null,
    prefilter_reason: null,
    ai_category: 'internal_governance',
    ai_category_label: '群内治理',
    ai_category_confidence: 96,
    push_gate_reason: 'non_information',
  }
  const classified = renderToStaticMarkup(React.createElement(Inspector, {
    row: nonInformation,
    onReanalyze() {},
    analyzing: false,
  }))
  assert.match(classified, /群内治理/)
  assert.match(classified, /分类为非资讯，不可推送/)
})

test('manual reanalysis queue state is visible while work is pending', () => {
  const queued = {
    id: 12,
    sent_at: '2026-01-01T00:00:00Z',
    score: 25,
    text: '去标识化待分析资讯',
    chat_name: '测试群',
    sender_name: '匿名',
    reasons: [],
    local_reasons: [],
    local_score: 25,
    reply_count: 0,
    reply_bonus: 0,
    ai_status: 'queued',
    analysis_queue_state: 'retry',
    analysis_queue_attempts: 2,
    analysis_queue_available_at: '2026-01-01T00:01:00Z',
    analysis_queue_error_category: 'network_error',
    analysis_queue_manual: true,
    prefilter_status: 'passed',
    push_eligible: false,
    push_gate_reason: 'analysis_retry',
    deliveries: [],
  }
  const html = renderToStaticMarkup(React.createElement(Inspector, {
    row: queued,
    onReanalyze() {},
    analyzing: false,
    initialTab: 'analysis',
  }))
  assert.match(html, /队列状态/)
  assert.match(html, /retry/)
  assert.match(html, /任务尝试/)
  assert.match(html, /队列错误类别/)
  assert.match(html, /网络不可用/)
})
