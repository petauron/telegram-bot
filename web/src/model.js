export const CATEGORY_LABELS = Object.freeze({
  internal_governance: '群内治理',
  internal_coordination: '群内协作',
  external_information: '外部资讯',
  discussion: '讨论交流',
  promotion_spam: '推广/垃圾',
  unknown: '无法判断',
})

export const REASONING_EFFORT_LABELS = Object.freeze({
  default: '模型默认',
  low: '低',
  medium: '中',
  high: '高',
})

export const PREFILTER_STATUS_LABELS = Object.freeze({
  not_evaluated: '历史消息未检查',
  passed: '已通过本地前置过滤',
  filtered: '已被本地前置过滤',
})

export const PUSH_GATE_LABELS = Object.freeze({
  historical_unreviewed: '历史消息，默认无资讯推送资格',
  awaiting_analysis: '等待资讯分类',
  prefiltered: '前置过滤，不可推送',
  model_disabled: '模型关闭，不可推送',
  model_unavailable: '模型不可用，不可推送',
  analysis_pending: '资讯分析中',
  analysis_queued: '已进入分析队列',
  analysis_processing: '模型分析处理中',
  analysis_retry: '瞬态错误，等待重试',
  manual_reanalysis_queued: '手动重新分析已排队',
  non_information: '分类为非资讯，不可推送',
  classification_error: '分类失败，不可推送',
  scoring_error: '资讯评分失败，不可推送',
  community_not_valuable: '普通讨论，不进入推送',
  community_low_confidence: '社区线索证据不足，不进入推送',
  community_analysis_error: '社区线索分析失败，不进入推送',
  benefit_not_valuable: '普通推广，不进入推送',
  benefit_low_confidence: '福利条件或可信度不足，不进入推送',
  benefit_analysis_error: '福利分析失败，不进入推送',
  analysis_interrupted: '分析中断，不可推送',
  eligible: '外部资讯评分成功，具备推送资格',
  below_digest_threshold: '历史状态：低于 60 分推送线',
  below_push_threshold: '低于 60 分推送线，不进入推送',
  semantic_dedupe_pending: '等待相似资讯检查',
  semantic_dedupe_checking: '正在检查相似资讯',
  eligible_unique: '未发现已推送的同一事件，具备推送资格',
  semantic_duplicate_suppressed: '同一事件已有代表消息，已抑制重复推送',
  semantic_unverified_update_suppressed: '补充细节缺少状态变化证据，已抑制重复推送',
  semantic_material_update: '同一事件存在实质更新，已放行',
  semantic_low_confidence_pass: '相似度结论置信不足，已安全放行',
  semantic_dedupe_failed_open: '去重检查失败，为避免漏报已安全放行',
  semantic_superseded: '同一事件已有更高分代表消息',
  eligible_better_representative: '替换未投递的低分代表消息，具备推送资格',
  manual_reanalysis: '手动分析结果，不自动补推',
  notification_prepare_pending: '等待通知内容整理',
  manual_notification_prepare_pending: '手动分析等待通知内容整理',
  notification_preparing: '正在整理通知内容',
  eligible_notification_prepared: '通知内容已整理，具备推送资格',
  eligible_notification_fallback: '整理失败并已安全回退，具备推送资格',
})

export const SEMANTIC_DEDUPE_LABELS = Object.freeze({
  historical_unreviewed: '历史消息未检查',
  awaiting_analysis: '等待资讯评分',
  pending: '等待相似资讯检查',
  checking: '相似资讯检查中',
  not_required_prefilter: '前置过滤，无需检查',
  not_required_model: '模型不可用，无需检查',
  not_required_non_information: '非资讯，无需检查',
  not_required_analysis_error: '分析失败，无需检查',
  not_required_community_error: '社区线索分析失败，无需检查',
  not_required_community_filtered: '普通讨论，无需检查',
  not_required_benefit_error: '福利分析失败，无需检查',
  not_required_benefit_filtered: '普通推广，无需检查',
  not_required_below_threshold: '低于推送线，无需检查',
  unique_no_candidates: '24 小时内无候选，已放行',
  unique: '未发现同一事件，已放行',
  suppressed: '相似资讯已抑制',
  suppressed_unverified_update: '补充细节已抑制',
  material_update: '实质更新已放行',
  low_confidence_pass: '低置信度已放行',
  failed_open: '去重检查失败已放行',
  representative_replaced: '更高分代表项已放行',
  superseded: '已由更高分代表项替代',
})

export function semanticDedupeLabel(status) {
  return SEMANTIC_DEDUPE_LABELS[status] || '尚未检查'
}

export const SEMANTIC_UPDATE_TYPE_LABELS = Object.freeze({
  none: '无实质更新',
  confirmation_or_correction: '正式确认、辟谣或更正',
  service_status_change: '服务状态变化',
  new_version_or_cve: '新版本或新 CVE',
  price_or_policy_change: '价格或政策变化',
  region_or_availability_change: '地区或可用范围变化',
  date_or_deadline_change: '日期或期限变化',
  impact_status_change: '实际影响状态变化',
})

export function semanticUpdateTypeLabel(updateType) {
  return SEMANTIC_UPDATE_TYPE_LABELS[updateType] || '—'
}

export const COMMUNITY_SIGNAL_LABELS = Object.freeze({
  incident_report: '故障与异常反馈',
  technical_solution: '有效解决方案',
  verified_observation: '实测发现',
  consensus_correction: '讨论纠正',
  status_update: '状态更新',
  product_review: '产品口碑',
})

export function communitySignalLabel(value) {
  return COMMUNITY_SIGNAL_LABELS[value] || '普通讨论'
}

export const BENEFIT_TYPE_LABELS = Object.freeze({
  official_freebie: '官方限免/免费额度',
  limited_discount: '限时折扣',
  coupon_credit: '优惠码/兑换额度',
  free_trial: '免费试用',
  giveaway: '公开赠送',
  price_drop: '明确降价',
  product_restock: '商品补货/恢复下单',
})

export function benefitTypeLabel(value) {
  return BENEFIT_TYPE_LABELS[value] || '普通推广'
}

export const NOTIFICATION_PREPARE_LABELS = Object.freeze({
  historical_unprepared: '历史消息未整理',
  awaiting_analysis: '等待资讯分析',
  awaiting_semantic_dedupe: '等待相似资讯检查',
  pending: '等待通知整理',
  preparing: '正在整理通知',
  success: '通知已整理',
  failed_fallback: '整理失败，已安全回退',
  not_required_prefilter: '前置过滤，无需整理',
  not_required_model: '模型不可用，无需整理',
  not_required_non_information: '非资讯，无需整理',
  not_required_analysis_error: '分析失败，无需整理',
  not_required_below_threshold: '低于推送线，无需整理',
  not_required_semantic_duplicate: '相似资讯已抑制，无需整理',
})

export function notificationPrepareLabel(status) {
  return NOTIFICATION_PREPARE_LABELS[status] || '尚未整理'
}

export function categoryLabel(category, fallback = '未分类') {
  return CATEGORY_LABELS[category] || fallback
}

export function reasoningEffortLabel(effort) {
  return REASONING_EFFORT_LABELS[effort] || '—'
}

export function prefilterStatusLabel(status) {
  return PREFILTER_STATUS_LABELS[status] || '未检查'
}

export function pushGateLabel(reason) {
  return PUSH_GATE_LABELS[reason] || '不可推送'
}

export const DELIVERY_STATE_LABELS = Object.freeze({
  queued: '待投递',
  processing: '投递中',
  retry: '等待重试',
  succeeded: '已送达',
  failed: '投递失败',
})

export function deliveryStateLabel(state) {
  return DELIVERY_STATE_LABELS[state] || '未投递'
}
