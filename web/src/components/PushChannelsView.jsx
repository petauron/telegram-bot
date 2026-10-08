import { useState } from 'react'
import { BellRing, CheckCircle2, LockKeyhole, Save, Send, ShieldCheck, Tag } from './Icons'
import { PageHeading } from './PageHeading'
import { SectionTabs } from './SectionTabs'

const FEEDBACK_ERRORS = {
  configuration: '反馈配置未就绪',
  timeout: '连接超时',
  network_error: '网络异常',
  authentication_error: '访问权限不足',
  rate_limited: '服务限制频率',
  upstream_error: '服务暂不可用',
  request_rejected: '请求被拒绝',
  response_too_large: '响应异常',
  invalid_response: '响应格式异常',
  internal_error: '内部异常',
}

export function isPushConfigDirty(form, config) {
  if (!form || !config) return false
  return Boolean(
    form.telegramBotToken
    || form.clearTelegramBotToken
    || form.ntfyAccessToken
    || form.clearNtfyAccessToken
    || form.telegramEnabled !== Boolean(config.telegram?.enabled)
    || form.telegramChatId !== (config.telegram?.chat_id || '')
    || form.ntfyEnabled !== Boolean(config.ntfy?.enabled)
    || form.ntfyBaseUrl !== (config.ntfy?.base_url || '')
    || form.ntfyTopic !== (config.ntfy?.topic || '')
    || form.ntfyCommunityTopic !== (config.ntfy?.community_topic || config.ntfy?.topic || '')
    || form.ntfyBenefitTopic !== (config.ntfy?.benefit_topic || config.ntfy?.topic || '')
  )
}

function ChannelTabs({ active, onChange, form }) {
  return (
    <SectionTabs
      items={[
        { id: 'telegram', label: 'Telegram Bot', meta: form.telegramEnabled ? '已启用' : '已停用' },
        { id: 'ntfy', label: 'ntfy', meta: form.ntfyEnabled ? '已启用' : '已停用' },
      ]}
      active={active}
      onChange={onChange}
      label="推送渠道选择"
      className="page-tabs channel-tabs"
    />
  )
}

function NtfyPreview() {
  return (
    <aside className="channel-preview" aria-label="ntfy 手机通知示意">
      <h3>推送预览</h3>
      <div className="notification-preview">
        <span className="preview-icon"><BellRing size={18} /></span>
        <div><strong>🟥92｜新闻｜高危漏洞修复已发布，建议尽快升级</strong><small>分段客户通知</small></div>
        <time>10:32</time>
        <p><b>来源</b>{'\n'}安全资讯频道{'\n\n'}<b>资讯摘要</b>{'\n'}• 产品安全升级已发布，修复一项高危漏洞。{'\n'}• 受影响用户建议尽快更新。</p>
        <a href="https://example.com/security/advisory" target="_blank" rel="noreferrer">查看原文</a>
        <div className="preview-feedback-actions" aria-label="ntfy 反馈按钮示意">
          <span>👍 有用</span><span>👎 无用</span>
        </div>
      </div>
      <h3>功能特性</h3>
      <ul className="capability-list">
        <li><BellRing size={17} /><strong>精华标题</strong><span>颜色分数、内容类型与核心变化</span></li>
        <li><ShieldCheck size={17} /><strong>自动优先级</strong><span>保留手机原生提醒级别</span></li>
        <li><Send size={17} /><strong>分段正文</strong><span>来源、摘要与要点清晰分隔</span></li>
        <li><Tag size={17} /><strong>通用来源</strong><span>不绑定具体消息平台</span></li>
        <li><Tag size={17} /><strong>分数牌</strong><span>60 起按绿、黄、橙、红渐变</span></li>
        <li><CheckCircle2 size={17} /><strong>有用性反馈</strong><span>点赞或踩，反馈结果进入本地审计</span></li>
      </ul>
    </aside>
  )
}

function TelegramPreview() {
  return (
    <aside className="channel-preview telegram-preview" aria-label="Telegram Bot 推送说明">
      <h3>送达方式</h3>
      <div className="delivery-diagram"><span><Send size={22} /></span><strong>Push Bot</strong><i /><strong>私人聊天</strong></div>
      <p>AI 分达到 60 后，使用安全转义的 Telegram HTML 实时送达。</p>
      <ul className="capability-list">
        <li><ShieldCheck size={17} /><strong>只推送到私聊</strong><span>不会向监听群发言</span></li>
        <li><Send size={17} /><strong>统一客户正文</strong><span>复用已整理标题、段落与外部原文</span></li>
      </ul>
    </aside>
  )
}

export function PushChannelsView({
  form,
  config,
  status,
  onChange,
  onSave,
  onTest,
  saving,
  testingChannel,
  initialChannel = '',
}) {
  const [activeChannel, setActiveChannel] = useState(initialChannel || (config?.ntfy?.enabled ? 'ntfy' : 'telegram'))
  if (!form) return null
  const dirty = isPushConfigDirty(form, config)
  const update = (key) => (event) => {
    const value = event.target.type === 'checkbox' ? event.target.checked : event.target.value
    onChange((current) => ({ ...current, [key]: value }))
  }
  const telegramReady = Boolean(form.telegramEnabled && form.telegramChatId && (config?.telegram?.bot_token_configured || form.telegramBotToken))
  const ntfyReady = Boolean(
    form.ntfyEnabled
    && form.ntfyBaseUrl
    && form.ntfyTopic
    && form.ntfyCommunityTopic
    && form.ntfyBenefitTopic
  )
  const activeReady = activeChannel === 'telegram' ? telegramReady : ntfyReady

  return (
    <main className="page push-channels-view">
      <PageHeading title="推送渠道" description="选择资讯送达方式并验证连接。" />
      <ChannelTabs active={activeChannel} onChange={setActiveChannel} form={form} />
      <p className="channel-note">启用多个渠道时，同一条资讯会同时送达。</p>

      <section className="form-surface channel-surface">
        <div className="channel-form">
          <header className="form-surface-header">
            <div><h3>{activeChannel === 'telegram' ? 'Telegram Bot' : 'ntfy'}</h3><p>{activeChannel === 'telegram' ? '通过独立 Bot 推送到私人聊天。' : '向 ntfy Topic 发送简洁的客户手机通知。'}</p></div>
            <label className="inline-toggle">
              <input type="checkbox" checked={activeChannel === 'telegram' ? form.telegramEnabled : form.ntfyEnabled} onChange={update(activeChannel === 'telegram' ? 'telegramEnabled' : 'ntfyEnabled')} aria-label={activeChannel === 'telegram' ? '启用 Telegram Push Bot' : '启用 ntfy'} />
              <span>{(activeChannel === 'telegram' ? form.telegramEnabled : form.ntfyEnabled) ? '已启用' : '已停用'}</span>
            </label>
            <span className={`save-state ${dirty ? 'dirty' : 'saved'}`} role="status"><CheckCircle2 size={15} />{dirty ? '尚未保存' : '所有更改已保存'}</span>
          </header>

          {activeChannel === 'telegram' ? (
            <section className="form-section channel-fields" aria-labelledby="telegram-channel-fields">
              <h4 id="telegram-channel-fields">连接配置</h4>
              <label className="field"><span>Bot Token</span><input type="password" value={form.telegramBotToken} onChange={update('telegramBotToken')} placeholder={config?.telegram?.bot_token_configured ? '已配置；留空保持不变' : '输入 Bot Token'} autoComplete="new-password" disabled={form.clearTelegramBotToken} aria-label="Telegram Bot Token" /><small>{config?.telegram?.bot_token_configured ? '已安全保存，页面不会回读明文。' : '尚未保存 Bot Token。'}</small></label>
              <label className="field"><span>接收聊天 ID</span><input type="text" value={form.telegramChatId} onChange={update('telegramChatId')} placeholder="例如 123456789" aria-label="Telegram 接收聊天 ID" /><small>请先在私人聊天中向 Bot 发送 /start。</small></label>
              <details className="security-disclosure"><summary><LockKeyhole size={16} />凭据与安全</summary><div><p>Token 只保存在受限 SQLite，GET API 永不回传。</p><label className="danger-option"><input type="checkbox" checked={form.clearTelegramBotToken} onChange={update('clearTelegramBotToken')} disabled={!config?.telegram?.bot_token_configured} />保存时显式清除 Bot Token</label></div></details>
            </section>
          ) : (
            <section className="form-section channel-fields" aria-labelledby="ntfy-channel-fields">
              <h4 id="ntfy-channel-fields">连接配置</h4>
              <label className="field"><span>服务地址</span><input type="url" value={form.ntfyBaseUrl} onChange={update('ntfyBaseUrl')} placeholder="https://ntfy.example.com" aria-label="ntfy 服务地址" /><small>默认使用 https://ntfy.example.com；仅允许 HTTP/HTTPS。</small></label>
              <div className="topic-routing-note"><strong>按内容分流</strong><small>每条通知只进入对应 Topic；产品口碑归入社区讨论。</small></div>
              <label className="field"><span>新闻资讯 Topic</span><input type="text" value={form.ntfyTopic} onChange={update('ntfyTopic')} placeholder="例如 radar-news" aria-label="ntfy 新闻资讯 Topic" /><small>外部新闻、产品变化和重要行业资讯。</small></label>
              <label className="field"><span>社区讨论 Topic</span><input type="text" value={form.ntfyCommunityTopic} onChange={update('ntfyCommunityTopic')} placeholder="例如 radar-community" aria-label="ntfy 社区讨论 Topic" /><small>有价值的讨论线索、故障佐证和产品口碑。</small></label>
              <label className="field"><span>福利羊毛 Topic</span><input type="text" value={form.ntfyBenefitTopic} onChange={update('ntfyBenefitTopic')} placeholder="例如 radar-benefits" aria-label="ntfy 福利羊毛 Topic" /><small>免费额度、限免和条件明确的优惠。</small></label>
              <label className="field"><span>Access Token（可选）</span><input type="password" value={form.ntfyAccessToken} onChange={update('ntfyAccessToken')} placeholder={config?.ntfy?.access_token_configured ? '已配置；留空保持不变' : '未启用访问控制时可留空'} autoComplete="new-password" disabled={form.clearNtfyAccessToken} aria-label="ntfy Access Token" /><small>{config?.ntfy?.access_token_configured ? '已安全保存，页面不会回读明文。' : '未保存 Access Token。'}</small></label>
              <label className="field"><span>反馈 Topic（只读）</span><input type="text" value={config?.ntfy?.feedback?.topic || ''} readOnly aria-label="ntfy 反馈 Topic" /><small>在 ntfy 服务端仅为此 Topic 授予 anonymous/everyone 的 write-only 权限；不要开放读取，也不要修改主通知 Topic 的权限。</small></label>
              <div className={`ntfy-feedback-state ${config?.ntfy?.feedback?.last_error_category ? 'error' : 'ready'}`} role="status">
                <strong>👍 有用 / 👎 无用反馈</strong>
                <small>{config?.ntfy?.feedback?.last_error_category
                  ? `反馈收取暂时异常：${FEEDBACK_ERRORS[config.ntfy.feedback.last_error_category] || '暂时不可用'} · 连续 ${config.ntfy.feedback.consecutive_failures || 1} 次`
                  : config?.ntfy?.feedback?.last_received_at
                    ? '反馈收取正常，最近已收到用户选择。'
                    : '启用 ntfy 后自动附加；通过独立签名 Topic 出站收取。'}</small>
              </div>
              <details className="security-disclosure"><summary><LockKeyhole size={16} />凭据与安全</summary><div><p>Access Token 只用于服务端推送，不会回传页面。</p><label className="danger-option"><input type="checkbox" checked={form.clearNtfyAccessToken} onChange={update('clearNtfyAccessToken')} disabled={!config?.ntfy?.access_token_configured} />保存时显式清除 Access Token</label></div></details>
            </section>
          )}

          <footer className="form-actions channel-actions">
            <button className="button secondary" type="button" onClick={() => onTest(activeChannel)} disabled={saving || Boolean(testingChannel) || dirty || !activeReady}><BellRing size={16} className={testingChannel === activeChannel ? 'spin' : ''} />{testingChannel === activeChannel ? '测试中…' : '发送测试推送'}</button>
            <p className={`form-feedback ${status.kind}`}>{status.message || (dirty ? '有尚未保存的渠道配置。' : '所有更改已保存。')}</p>
            <button className="button primary" type="button" onClick={onSave} disabled={saving || Boolean(testingChannel) || !dirty}><Save size={16} />{saving ? '保存中…' : '保存推送配置'}</button>
          </footer>
        </div>
        {activeChannel === 'telegram' ? <TelegramPreview /> : <NtfyPreview />}
      </section>
    </main>
  )
}
