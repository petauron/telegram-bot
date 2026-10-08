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
  PushChannelsView,
  isPushConfigDirty,
} = await vite.ssrLoadModule('/src/components/PushChannelsView.jsx')
const { Sidebar } = await vite.ssrLoadModule('/src/components/Sidebar.jsx')
const { Header } = await vite.ssrLoadModule('/src/components/Header.jsx')

after(async () => {
  await vite.close()
})

const config = {
  telegram: {
    enabled: true,
    chat_id: '123456',
    bot_token_configured: true,
  },
  ntfy: {
    enabled: false,
    base_url: 'https://ntfy.example.com',
    topic: '',
    community_topic: '',
    benefit_topic: '',
    access_token_configured: false,
    feedback: {
      enabled: false,
      topic: 'feedback-opaque-qa',
      last_received_at: null,
      last_error_category: null,
      consecutive_failures: 0,
    },
  },
  updated_at: '2026-08-09T00:00:00Z',
}

const form = {
  telegramEnabled: true,
  telegramBotToken: '',
  clearTelegramBotToken: false,
  telegramChatId: '123456',
  ntfyEnabled: false,
  ntfyBaseUrl: 'https://ntfy.example.com',
  ntfyTopic: '',
  ntfyCommunityTopic: '',
  ntfyBenefitTopic: '',
  ntfyAccessToken: '',
  clearNtfyAccessToken: false,
}

function render(overrides = {}) {
  return renderToStaticMarkup(React.createElement(PushChannelsView, {
    form,
    config,
    status: { kind: 'idle', message: '' },
    onChange() {},
    onSave() {},
    onTest() {},
    saving: false,
    testingChannel: '',
    ...overrides,
  }))
}

test('push channels page renders current Telegram channel and ntfy default', () => {
  const html = render({ initialChannel: 'ntfy' })
  assert.match(html, /<h2>推送渠道<\/h2>/)
  assert.match(html, /Telegram Bot/)
  assert.match(html, /<h3>ntfy<\/h3>/)
  assert.match(html, /value="https:\/\/ntfy.example.com"/)
  assert.match(html, /aria-label="ntfy 新闻资讯 Topic"/)
  assert.match(html, /aria-label="ntfy 社区讨论 Topic"/)
  assert.match(html, /aria-label="ntfy 福利羊毛 Topic"/)
  assert.match(html, /产品口碑归入社区讨论/)
  assert.match(html, /aria-label="ntfy 反馈 Topic"/)
  assert.match(html, /value="feedback-opaque-qa"/)
  assert.match(html, /anonymous\/everyone 的 write-only 权限/)
  assert.match(html, /启用多个渠道时，同一条资讯会同时送达/)
  assert.match(html, /精华标题/)
  assert.match(html, /自动优先级/)
  assert.match(html, /分数牌/)
  assert.match(html, /60 起按绿、黄、橙、红渐变/)
  assert.match(html, /分段正文/)
  assert.match(html, /通用来源/)
  assert.match(html, /有用性反馈/)
  assert.match(html, /👍 有用/)
  assert.match(html, /👎 无用/)
  assert.match(html, /独立签名 Topic 出站收取/)
  assert.match(html, /🟥92｜新闻｜高危漏洞修复已发布，建议尽快升级/)
  assert.match(html, /分段客户通知/)
  assert.match(html, /<b>来源<\/b>\n安全资讯频道\n\n<b>资讯摘要<\/b>\n• 产品安全升级已发布，修复一项高危漏洞。\n• 受影响用户建议尽快更新。/)
  assert.match(html, /href="https:\/\/example.com\/security\/advisory"/)
  assert.match(html, />查看原文<\/a>/)
  assert.doesNotMatch(html, /点击跳转|Telegram 原消息|【最高优先】|智能标签/)
  assert.doesNotMatch(html, /AI[：： ]|评分|分类|理由|回复/)
})

test('saved secrets only render configured state and password inputs', () => {
  const html = render()
  assert.match(html, /aria-label="Telegram Bot Token"/)
  assert.match(html, /type="password"/)
  assert.match(html, /placeholder="已配置；留空保持不变"/)
  assert.doesNotMatch(html, /bot-token-value/)
  assert.match(html, /页面不会回读明文/)
})

test('all channel controls participate in dirty state', () => {
  assert.equal(isPushConfigDirty(form, config), false)
  assert.equal(isPushConfigDirty({ ...form, ntfyEnabled: true }, config), true)
  assert.equal(isPushConfigDirty({ ...form, ntfyTopic: 'priority-news' }, config), true)
  assert.equal(isPushConfigDirty({ ...form, ntfyCommunityTopic: 'priority-community' }, config), true)
  assert.equal(isPushConfigDirty({ ...form, ntfyBenefitTopic: 'priority-benefits' }, config), true)
  assert.equal(isPushConfigDirty({ ...form, telegramBotToken: 'replacement' }, config), true)
  assert.equal(isPushConfigDirty({ ...form, clearTelegramBotToken: true }, config), true)
  assert.equal(isPushConfigDirty({ ...form, ntfyAccessToken: 'new-token' }, config), true)
})

test('test actions are only available for saved configured channels', () => {
  const saved = render()
  assert.match(saved, /aria-label="Telegram Bot Token"/)
  assert.match(saved, /发送测试推送/)
  const dirty = render({
    form: {
      ...form,
      ntfyEnabled: true,
      ntfyTopic: 'priority-news',
      ntfyCommunityTopic: 'priority-community',
      ntfyBenefitTopic: 'priority-benefits',
    },
    initialChannel: 'ntfy',
  })
  const disabledButtons = dirty.match(/<button[^>]*disabled=""[^>]*>/g) || []
  assert.ok(disabledButtons.length >= 1)
  assert.match(dirty, /尚未保存/)
})

test('sidebar exposes a dedicated push channels destination', () => {
  const html = renderToStaticMarkup(React.createElement(Sidebar, {
    active: 'push',
    onNavigate() {},
    online: true,
    username: 'admin',
  }))
  assert.match(html, /aria-label="推送渠道"/)
  assert.match(html, /nav-item active/)
  assert.doesNotMatch(html, /SSH 隧道/)
})

test('push page header does not show the unrelated save-rules action', () => {
  const html = renderToStaticMarkup(React.createElement(Header, {
    syncedLabel: '刚刚',
    refreshing: false,
    onRefresh() {},
    onSave() {},
    canSave: true,
    showRuleSave: false,
    username: 'admin',
    onLogout() {},
    loggingOut: false,
  }))
  assert.doesNotMatch(html, /保存规则/)
  assert.match(html, />退出</)
})
