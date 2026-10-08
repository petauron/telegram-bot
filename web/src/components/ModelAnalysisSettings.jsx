import { CheckCircle2, LockKeyhole, RefreshCw, Save } from './Icons'

const STATUS_LABELS = {
  unconfigured: '未配置',
  loading: '加载中',
  success: '刷新成功',
  error: '错误',
  saved: '已保存',
  dirty: '尚未保存',
}

export function isModelConfigDirty(form, config) {
  if (!form || !config) return false
  return Boolean(
    form.apiKey
    || form.clearApiKey
    || form.enabled !== Boolean(config.enabled)
    || form.communityInsightsEnabled !== (config.community_insights_enabled !== false)
    || form.benefitDealsEnabled !== (config.benefit_deals_enabled !== false)
    || form.baseUrl !== (config.base_url || '')
    || form.classificationModel !== (config.classification_model || 'gemini-3.5-flash-extra-low')
    || form.classificationReasoningEffort !== (config.classification_reasoning_effort || 'low')
    || form.model !== (config.model || '')
    || form.reasoningEffort !== (config.reasoning_effort || 'default')
    || form.semanticDedupeModel !== (config.semantic_dedupe_model || 'gemini-3.5-flash-extra-low')
    || form.semanticDedupeReasoningEffort !== (config.semantic_dedupe_reasoning_effort || 'low')
    || form.notificationModel !== (config.notification_model || 'gemini-3.5-flash-extra-low')
    || form.notificationReasoningEffort !== (config.notification_reasoning_effort || 'low')
  )
}

function EffortSelect({ value, onChange, label }) {
  return (
    <select value={value} onChange={onChange} aria-label={label}>
      <option value="default">模型默认</option>
      <option value="low">低</option>
      <option value="medium">中</option>
      <option value="high">高</option>
    </select>
  )
}

export function ModelAnalysisSettings({
  form,
  config,
  models,
  status,
  onChange,
  onRefresh,
  onSave,
  refreshing,
  saving,
}) {
  if (!form) return null
  const dirty = isModelConfigDirty(form, config)
  const effectiveStatus = status.kind === 'idle'
    ? (config?.api_key_configured ? (dirty ? 'dirty' : 'saved') : 'unconfigured')
    : status.kind
  const availableModels = [...new Set([
    form.classificationModel,
    form.model,
    form.semanticDedupeModel,
    form.notificationModel,
    ...models,
  ].filter(Boolean))]
  const update = (key) => (event) => {
    const value = event.target.type === 'checkbox' ? event.target.checked : event.target.value
    onChange((current) => ({ ...current, [key]: value }))
  }

  return (
    <section className="form-surface model-settings" aria-labelledby="model-analysis-title">
      <header className="form-surface-header">
        <div><h3 id="model-analysis-title">模型分析</h3><p>四个阶段共享连接，但模型与推理档位相互独立。</p></div>
        <label className="inline-toggle">
          <input type="checkbox" checked={form.enabled} onChange={update('enabled')} aria-label="启用模型分析" />
          <span>{form.enabled ? '已启用' : '已停用'}</span>
        </label>
        <span className={`save-state ${effectiveStatus}`} role="status">
          <CheckCircle2 size={15} />{refreshing ? '加载中' : STATUS_LABELS[effectiveStatus]}
        </span>
      </header>

      <section className="form-section model-community-option" aria-labelledby="community-insights-title">
        <div>
          <h4 id="community-insights-title">社区线索</h4>
          <p>讨论消息先经过本地门控，再复用资讯评分模型提炼故障、实测和解决方案。同一产品两小时内至少两位参与者的具体体验可聚合为“产品口碑”；达到 60 分后同样实时推送，单人评价、求推荐和普通闲聊不推送。</p>
        </div>
        <label className="inline-toggle">
          <input type="checkbox" checked={form.communityInsightsEnabled} onChange={update('communityInsightsEnabled')} aria-label="启用社区线索分析" />
          <span>{form.communityInsightsEnabled ? '已启用' : '已停用'}</span>
        </label>
      </section>

      <section className="form-section model-community-option" aria-labelledby="benefit-deals-title">
        <div>
          <h4 id="benefit-deals-title">福利羊毛</h4>
          <p>推广类消息先经过高精度本地门控，再复用资讯评分模型判断限免、免费额度、优惠码和明确降价；返佣拉人、诈骗和普通广告不会推送。</p>
        </div>
        <label className="inline-toggle">
          <input type="checkbox" checked={form.benefitDealsEnabled} onChange={update('benefitDealsEnabled')} aria-label="启用福利羊毛分析" />
          <span>{form.benefitDealsEnabled ? '已启用' : '已停用'}</span>
        </label>
      </section>

      <section className="form-section connection-section">
        <h4>连接配置</h4>
        <div className="connection-row">
          <label className="field grow">
            <span>OpenAI-compatible API Base URL</span>
            <input type="url" value={form.baseUrl} onChange={update('baseUrl')} placeholder="https://model.example.com/v1" aria-label="模型 API Base URL" />
            <small>仅允许 http/https；默认建议使用本机 CPA 地址。</small>
          </label>
          <button className="button secondary model-refresh" type="button" onClick={onRefresh} disabled={refreshing || saving || form.clearApiKey || !form.baseUrl}>
            <RefreshCw size={16} className={refreshing ? 'spin' : ''} />{refreshing ? '刷新中…' : '刷新模型'}
          </button>
        </div>
        <label className="field">
          <span>API Key</span>
          <input type="password" value={form.apiKey} onChange={update('apiKey')} placeholder={config?.api_key_configured ? '已配置；留空保持不变' : '输入 API Key'} autoComplete="new-password" disabled={form.clearApiKey} aria-label="模型 API Key" />
          <small>{config?.api_key_configured ? '已安全保存，页面不会回读明文。' : '尚未保存 API Key。'}</small>
        </label>
        <details className="security-disclosure">
          <summary><LockKeyhole size={16} />密钥与隐私</summary>
          <div>
            <p>密钥仅保存在受限 SQLite；消息只在模型分析启用且配置完整后发送。</p>
            <label className="danger-option"><input type="checkbox" checked={form.clearApiKey} onChange={update('clearApiKey')} disabled={!config?.api_key_configured} />保存时显式清除 API Key</label>
          </div>
        </details>
      </section>

      <section className="form-section stages-section">
        <h4>四阶段模型配置</h4>
        <div className="stage-row">
          <div className="stage-intro"><strong>分类阶段</strong><small>先判断是否为外部资讯</small></div>
          <label className="field"><span>模型</span><select value={form.classificationModel} onChange={update('classificationModel')} aria-label="分类分析模型选择"><option value="">请先刷新模型</option>{availableModels.map((model) => <option value={model} key={model}>{model}</option>)}</select></label>
          <label className="field effort-field"><span>推理档位</span><EffortSelect value={form.classificationReasoningEffort} onChange={update('classificationReasoningEffort')} label="分类分析推理档位" /></label>
          <p>默认低推理；非资讯在此结束。</p>
        </div>
        <div className="stage-row">
          <div className="stage-intro"><strong>评分阶段</strong><small>仅外部资讯进入评分</small></div>
          <label className="field"><span>模型</span><select value={form.model} onChange={update('model')} aria-label="评分分析模型选择"><option value="">请先刷新模型</option>{availableModels.map((model) => <option value={model} key={model}>{model}</option>)}</select></label>
          <label className="field effort-field"><span>推理档位</span><EffortSelect value={form.reasoningEffort} onChange={update('reasoningEffort')} label="评分分析推理档位" /></label>
          <p>AI 分是唯一推送判断分数。</p>
        </div>
        <div className="stage-row">
          <div className="stage-intro"><strong>语义去重</strong><small>仅比较达到 60 分推送线的资讯</small></div>
          <label className="field"><span>模型</span><select value={form.semanticDedupeModel} onChange={update('semanticDedupeModel')} aria-label="语义去重模型选择"><option value="">请先刷新模型</option>{availableModels.map((model) => <option value={model} key={model}>{model}</option>)}</select></label>
          <label className="field effort-field"><span>推理档位</span><EffortSelect value={form.semanticDedupeReasoningEffort} onChange={update('semanticDedupeReasoningEffort')} label="语义去重推理档位" /></label>
          <p>跨来源比较会增加一次调用；更高档位可能增加时延和费用。</p>
        </div>
        <div className="stage-row">
          <div className="stage-intro"><strong>通知内容整理</strong><small>只整理最终放行的资讯</small></div>
          <label className="field"><span>模型</span><select value={form.notificationModel} onChange={update('notificationModel')} aria-label="通知内容整理模型选择"><option value="">请先刷新模型</option>{availableModels.map((model) => <option value={model} key={model}>{model}</option>)}</select></label>
          <label className="field effort-field"><span>推理档位</span><EffortSelect value={form.notificationReasoningEffort} onChange={update('notificationReasoningEffort')} label="通知内容整理推理档位" /></label>
          <p>输出冻结后由所有渠道复用；失败会安全回退，不阻塞通知。</p>
        </div>
      </section>

      <footer className="form-actions">
        <p className={`form-feedback ${status.kind}`}>{status.message || (dirty ? '有尚未保存的模型配置。' : '所有更改已保存。')}</p>
        <button className="button primary" type="button" onClick={onSave} disabled={saving || refreshing || !dirty}>
          <Save size={16} />{saving ? '保存中…' : '保存模型配置'}
        </button>
      </footer>
    </section>
  )
}

export { STATUS_LABELS }
