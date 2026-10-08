import { useMemo, useState } from 'react'
import { ChatPicker } from './ChatPicker'
import { ModelAnalysisSettings } from './ModelAnalysisSettings'
import { PageHeading } from './PageHeading'
import { RuleEditor } from './RuleEditor'
import { SectionTabs } from './SectionTabs'

const CONFIG_TABS = [
  { id: 'watch', label: '监听范围' },
  { id: 'rules', label: '资讯规则' },
  { id: 'model', label: '模型分析' },
]

function sameChatIds(current = [], saved = []) {
  if (current.length !== saved.length) return false
  const savedIds = new Set(saved)
  return current.every((id) => savedIds.has(id))
}

function countValues(value = '') {
  return value.split(/[,，\n]/).map((item) => item.trim()).filter(Boolean).length
}

function ConfigSummary({ active, form, config, chats, modelConfig }) {
  const chatCounts = useMemo(() => chats.reduce((result, chat) => {
    if ((form?.watchChatIds || []).includes(chat.chat_id)) result[chat.chat_type] += 1
    return result
  }, { group: 0, channel: 0 }), [chats, form?.watchChatIds])

  if (active === 'watch') {
    return (
      <aside className="config-summary" aria-label="监听范围摘要">
        <h3>监听范围</h3>
        <p className="summary-lead">已监听 <strong>{form?.watchChatIds?.length ?? 0}</strong> / 可见 {chats.length}</p>
        <dl>
          <div><dt>群组</dt><dd>{chatCounts.group}</dd></div>
          <div><dt>频道</dt><dd>{chatCounts.channel}</dd></div>
          <div><dt>最近同步</dt><dd>自动</dd></div>
        </dl>
        <p className="summary-note">只显示个人账号原本有权查看的群组与频道。</p>
      </aside>
    )
  }
  if (active === 'rules') {
    return (
      <aside className="config-summary" aria-label="资讯规则摘要">
        <h3>资讯规则</h3>
        <p className="summary-lead">实时推送线 <strong>60</strong> 分</p>
        <dl>
          <div><dt>重要关键词</dt><dd>{countValues(form?.keywords)} 个</dd></div>
          <div><dt>可信发送者</dt><dd>{countValues(form?.trustedIds)} 个</dd></div>
          <div><dt>投递方式</dt><dd>分析完成即发送</dd></div>
        </dl>
        <p className="summary-note">模型开启后，推送资格和排序只使用资讯 AI 分。</p>
      </aside>
    )
  }
  return (
    <aside className="config-summary" aria-label="模型分析摘要">
      <h3>模型分析</h3>
      <p className={`summary-lead ${modelConfig?.enabled ? 'healthy' : ''}`}>{modelConfig?.enabled ? '● 已启用' : '○ 已停用'}</p>
      <dl>
        <div><dt>API Key</dt><dd>{modelConfig?.api_key_configured ? '已配置' : '未配置'}</dd></div>
        <div><dt>分类模型</dt><dd>{modelConfig?.classification_model ? '已选择' : '未选择'}</dd></div>
        <div><dt>评分模型</dt><dd>{modelConfig?.model ? '已选择' : '未选择'}</dd></div>
        <div><dt>去重模型</dt><dd>{modelConfig?.semantic_dedupe_model ? '已选择' : '未选择'}</dd></div>
        <div><dt>整理模型</dt><dd>{modelConfig?.notification_model ? '已选择' : '未选择'}</dd></div>
      </dl>
      <ol className="request-flow">
        <li><b>1</b><span>本地过滤<small>0 次请求</small></span></li>
        <li><b>2</b><span>非资讯<small>1 次请求</small></span></li>
        <li><b>3</b><span>低分资讯<small>2 次请求</small></span></li>
        <li><b>4</b><span>达到推送线<small>去重与整理，总计 3–4 次</small></span></li>
      </ol>
    </aside>
  )
}

export function SettingsView({
  form,
  config,
  chats,
  onChange,
  onSave,
  saving,
  modelForm,
  modelConfig,
  models,
  modelStatus,
  onModelChange,
  onRefreshModels,
  onSaveModel,
  modelRefreshing,
  modelSaving,
}) {
  const [activeTab, setActiveTab] = useState('watch')
  const watchDirty = !sameChatIds(form?.watchChatIds, config?.watch_chat_ids)
  return (
    <main className="page settings-view">
      <PageHeading title="规则配置" description="管理监听范围、资讯规则与模型分析。" />
      <SectionTabs items={CONFIG_TABS} active={activeTab} onChange={setActiveTab} label="规则配置分类" className="page-tabs" />
      <div className="settings-layout">
        <ConfigSummary active={activeTab} form={form} config={config} chats={chats} modelConfig={modelConfig} />
        <div className="settings-content">
          {activeTab === 'watch' ? (
            <ChatPicker
              chats={chats}
              selectedIds={form?.watchChatIds || []}
              onChange={(watchChatIds) => onChange((current) => ({ ...current, watchChatIds }))}
              onSave={onSave}
              saving={saving}
              dirty={watchDirty}
            />
          ) : null}
          {activeTab === 'rules' ? <RuleEditor form={form} onChange={onChange} onSave={onSave} saving={saving} expanded /> : null}
          {activeTab === 'model' ? (
            <ModelAnalysisSettings
              form={modelForm}
              config={modelConfig}
              models={models}
              status={modelStatus}
              onChange={onModelChange}
              onRefresh={onRefreshModels}
              onSave={onSaveModel}
              refreshing={modelRefreshing}
              saving={modelSaving}
            />
          ) : null}
        </div>
      </div>
    </main>
  )
}

export { CONFIG_TABS, sameChatIds }
