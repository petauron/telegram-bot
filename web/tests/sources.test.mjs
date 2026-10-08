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
const { InformationSourcesView, sourceRuntimeState, sourceStateLabel, summarizeSourceStates } = await vite.ssrLoadModule(
  '/src/components/InformationSourcesView.jsx',
)
const { Sidebar } = await vite.ssrLoadModule('/src/components/Sidebar.jsx')

after(async () => {
  await vite.close()
})

const source = {
  id: 12,
  kind: 'rss',
  name: '厂商安全公告',
  url: 'https://feeds.example.com/security.xml',
  enabled: true,
  initialized: true,
  poll_interval_minutes: 15,
  poll_state: 'idle',
  item_count: 14,
  message_count: 3,
  last_success_at: '2099-08-11T01:00:00Z',
  next_poll_at: '2099-08-11T01:15:00Z',
  consecutive_failures: 0,
}

test('information sources page renders baseline, status and guarded URL guidance', () => {
  const html = renderToStaticMarkup(React.createElement(InformationSourcesView, {
    sources: [source],
    loading: false,
    saving: false,
    providerConfig: { github_token_configured: false },
    onSave() {},
    onRefresh() {},
  }))
  assert.match(html, /<h2>信息源<\/h2>/)
  assert.match(html, /首次抓取只建立基线/)
  assert.match(html, /仅允许公开 HTTPS 地址/)
  assert.match(html, /厂商安全公告/)
  assert.match(html, /已建立采集基线/)
  assert.match(html, /已入管线/)
  assert.match(html, /立即抓取/)
  assert.match(html, /<section class="source-list surface-card" aria-label="已配置的信息源">/)
  assert.match(html, /<h3>已配置的信息源<\/h3>/)
  assert.match(html, /1 个来源/)
  assert.match(html, /<table class="source-table">/)
  assert.match(html, /<th>运行状态<\/th>/)
  assert.match(html, /<tr class="source-table-row "/)
  assert.ok(html.indexOf('source-table') < html.indexOf('source-form'), 'configured sources table should appear before configuration forms')
  assert.equal(sourceStateLabel(source), '定时采集中')
  assert.match(html, /1 定时运行/)
  assert.match(html, /采集器已生效，等待下次定时任务/)
})

test('source states distinguish scheduled, active, first-run, limited, stalled, failed and disabled', () => {
  const now = Date.parse('2099-08-11T01:05:00Z')
  const states = [
    source,
    { ...source, id: 2, poll_state: 'processing' },
    { ...source, id: 3, initialized: false, last_success_at: null, next_poll_at: null },
    { ...source, id: 4, initialized: false, last_success_at: null, next_poll_at: '2000-08-11T00:55:00Z' },
    { ...source, id: 5, poll_state: 'error', last_error_category: 'rate_limited', consecutive_failures: 2 },
    { ...source, id: 6, next_poll_at: null },
    { ...source, id: 7, next_poll_at: '2000-08-11T00:55:00Z' },
    { ...source, id: 8, poll_state: 'error', last_error_category: 'timeout', consecutive_failures: 2 },
    { ...source, id: 9, enabled: false, poll_state: 'disabled' },
  ]
  assert.deepEqual(states.map((item) => sourceStateLabel(item, now)), [
    '定时采集中', '正在抓取', '等待首次采集', '等待首次采集', '频率受限', '采集停滞', '采集停滞', '采集异常', '已停用',
  ])
  assert.deepEqual(states.map((item) => sourceRuntimeState(item, now)), [
    'scheduled', 'processing', 'waiting', 'waiting', 'rateLimited', 'stalled', 'stalled', 'error', 'disabled',
  ])
  assert.deepEqual(summarizeSourceStates(states, now), {
    scheduled: 1,
    processing: 1,
    waiting: 2,
    rateLimited: 1,
    stalled: 2,
    error: 1,
    disabled: 1,
  })
  const html = renderToStaticMarkup(React.createElement(InformationSourcesView, {
    sources: states,
    loading: false,
    saving: false,
    providerConfig: { github_token_configured: false },
    onSave() {},
    onRefresh() {},
  }))
  assert.match(html, /正在抓取/)
  assert.match(html, /频率受限/)
  assert.match(html, /采集停滞/)
  assert.match(html, /采集异常/)
  assert.match(html, /1 正在抓取/)
  assert.match(html, /2 待首次成功/)
  assert.match(html, /1 频率受限/)
  assert.match(html, /2 疑似停滞/)
  assert.match(html, /1 异常/)
  assert.doesNotMatch(html, /采集失败 · 连续 0 次/)
})

test('first-run waiting respects active and failure precedence plus the five-minute grace', () => {
  const nextPollAt = '2099-08-11T01:00:00Z'
  const firstRun = { ...source, initialized: false, last_success_at: null, next_poll_at: nextPollAt }

  assert.equal(sourceRuntimeState(firstRun, Date.parse('2099-08-11T01:05:00Z')), 'waiting')
  assert.equal(sourceRuntimeState({ ...firstRun, poll_state: 'processing' }), 'processing')
  assert.equal(sourceRuntimeState({
    ...firstRun,
    poll_state: 'error',
    last_error_category: 'rate_limited',
    consecutive_failures: 1,
  }), 'rateLimited')
  assert.equal(sourceRuntimeState({
    ...firstRun,
    poll_state: 'error',
    last_error_category: 'timeout',
    consecutive_failures: 1,
  }), 'error')

  const established = { ...source, next_poll_at: nextPollAt }
  assert.equal(sourceRuntimeState(established, Date.parse('2099-08-11T01:05:00Z')), 'scheduled')
  assert.equal(sourceRuntimeState(established, Date.parse('2099-08-11T01:05:00.001Z')), 'stalled')
})

test('source failure is understandable and provider secret is write-only', () => {
  const html = renderToStaticMarkup(React.createElement(InformationSourcesView, {
    sources: [{
      ...source,
      poll_state: 'error',
      last_error_category: 'timeout',
      consecutive_failures: 2,
    }],
    loading: false,
    saving: false,
    providerConfig: { github_token_configured: true },
    onSave() {},
    onRefresh() {},
  }))
  assert.match(html, /抓取超时/)
  assert.match(html, /连续 2 次/)
  assert.match(html, /已配置，留空保持不变/)
  assert.match(html, /GET 接口永不回传凭据明文/)
  assert.doesNotMatch(html, /secret-test-token/)
})

test('anonymous GitHub rate limits are surfaced as an actionable source problem', () => {
  const html = renderToStaticMarkup(React.createElement(InformationSourcesView, {
    sources: [{
      ...source,
      kind: 'github_releases',
      poll_state: 'error',
      last_error_category: 'rate_limited',
      consecutive_failures: 3,
    }],
    loading: false,
    saving: false,
    providerConfig: { github_token_configured: false },
    onSave() {},
    onRefresh() {},
  }))
  assert.match(html, /1 个 GitHub 来源触发匿名额度限制/)
  assert.match(html, /免费 Personal access token/)
  assert.match(html, /来源限制访问频率 · 连续 3 次/)
})

test('GitHub Releases source exposes repository and prerelease controls', () => {
  const html = renderToStaticMarkup(React.createElement(InformationSourcesView, {
    sources: [{
      ...source,
      kind: 'github_releases',
      name: 'Example releases',
      url: 'https://github.com/example/tool/releases',
      settings: { repository: 'example/tool', include_prereleases: true },
    }],
    loading: false,
    saving: false,
    providerConfig: { github_token_configured: false },
    onSave() {},
    onRefresh() {},
  }))
  assert.match(html, /GitHub Releases/)
  assert.match(html, /github\.com\/example\/tool\/releases/)
  assert.match(html, /正式版 \+ 预发布/)
  assert.match(html, /Example releases/)
})

test('GitHub advisories card exposes high-signal filters without credentials', () => {
  const html = renderToStaticMarkup(React.createElement(InformationSourcesView, {
    sources: [{
      ...source,
      kind: 'github_advisories',
      name: 'npm 高危公告',
      url: 'https://github.com/advisories?query=type%3Areviewed',
      settings: { ecosystem: 'npm', minimum_severity: 'high', keywords: ['react'] },
    }],
    loading: false,
    saving: false,
    providerConfig: { github_token_configured: false },
    onSave() {},
    onRefresh() {},
  }))
  assert.match(html, /GitHub Security Advisories/)
  assert.match(html, /High \+ Critical · npm · react/)
  assert.doesNotMatch(html, /test-only-token/)
})

test('CISA KEV is presented as a fixed high-signal source', () => {
  const html = renderToStaticMarkup(React.createElement(InformationSourcesView, {
    sources: [{
      ...source,
      kind: 'cisa_kev',
      name: 'CISA KEV',
      url: 'https://www.cisa.gov/known-exploited-vulnerabilities-catalog',
      settings: {},
    }],
    loading: false,
    saving: false,
    providerConfig: { github_token_configured: false },
    onSave() {},
    onRefresh() {},
  }))
  assert.match(html, /CISA 已确认在野利用 · 新增与实质更新/)
  assert.doesNotMatch(html, /api_key|access_token/)
})

test('NVD source shows incremental high-signal product filtering', () => {
  const html = renderToStaticMarkup(React.createElement(InformationSourcesView, {
    sources: [{
      ...source,
      kind: 'nvd_cve',
      name: 'NVD 重点产品',
      url: 'https://nvd.nist.gov/vuln/search',
      settings: { keywords: ['nginx', 'openssl'] },
    }],
    loading: false,
    saving: false,
    providerConfig: { github_token_configured: false, nvd_api_key_configured: true },
    onSave() {},
    onRefresh() {},
  }))
  assert.match(html, /High\/Critical \+ KEV · nginx \/ openssl/)
  assert.match(html, /NVD API Key/)
  assert.doesNotMatch(html, /test-only-nvd-key/)
})

test('vendor status source shows incident and recovery scope', () => {
  const html = renderToStaticMarkup(React.createElement(InformationSourcesView, {
    sources: [{
      ...source,
      kind: 'vendor_status',
      name: 'Example Status',
      url: 'https://status.example.com/',
      settings: {},
    }],
    loading: false,
    saving: false,
    providerConfig: { github_token_configured: false, nvd_api_key_configured: false },
    onSave() {},
    onRefresh() {},
  }))
  assert.match(html, /厂商状态页/)
  assert.match(html, /故障 · 性能下降 · 恢复 · 事后报告/)
  assert.match(html, /https:\/\/status\.example\.com/)
})

test('Hacker News source shows bounded story-only filters', () => {
  const html = renderToStaticMarkup(React.createElement(InformationSourcesView, {
    sources: [{
      ...source,
      kind: 'hacker_news',
      name: 'HN 技术热点',
      url: 'https://news.ycombinator.com/',
      settings: { story_list: 'best', minimum_score: 120, keywords: ['ai', 'security'] },
    }],
    loading: false,
    saving: false,
    providerConfig: {},
    onSave() {},
    onRefresh() {},
  }))
  assert.match(html, /Hacker News/)
  assert.match(html, /Best · ≥ 120 分 · ai \/ security/)
  assert.match(html, /不读取评论/)
})

test('Bluesky source is a single trusted account Jetstream subscription', () => {
  const html = renderToStaticMarkup(React.createElement(InformationSourcesView, {
    sources: [{
      ...source,
      kind: 'bluesky',
      name: '可信开发者',
      url: 'https://bsky.app/profile/example.bsky.social',
      settings: { handle: 'example.bsky.social' },
    }],
    loading: false,
    saving: false,
    providerConfig: {},
    onSave() {},
    onRefresh() {},
  }))
  assert.match(html, /Bluesky 可信账号/)
  assert.match(html, /@example\.bsky\.social · Jetstream 单账号订阅/)
  assert.doesNotMatch(html, /Firehose.*开启/)
})

test('Mastodon source shows source-scoped credential state without a value', () => {
  const html = renderToStaticMarkup(React.createElement(InformationSourcesView, {
    sources: [{
      ...source,
      kind: 'mastodon',
      name: '可信实例账号',
      url: 'https://social.example/@trusted',
      settings: { instance_url: 'https://social.example', timeline_type: 'account', target: 'trusted' },
      secret_configured: true,
    }],
    loading: false,
    saving: false,
    providerConfig: {},
    onSave() {},
    onRefresh() {},
  }))
  assert.match(html, /Mastodon 可信来源/)
  assert.match(html, /@trusted · 已配置来源 Token/)
  assert.doesNotMatch(html, /test-only-mastodon-token/)
})

test('Newsletter source shows read-only IMAPS and write-only credential state', () => {
  const html = renderToStaticMarkup(React.createElement(InformationSourcesView, {
    sources: [{
      ...source,
      kind: 'newsletter_imap',
      name: '技术简报',
      url: 'imaps://imap.vendor.example:993/INBOX?account=reader%40example.com',
      settings: { host: 'imap.vendor.example', port: 993, username: 'reader@example.com', mailbox: 'INBOX', sender_allowlist: ['vendor.example'] },
      secret_configured: true,
    }],
    loading: false,
    saving: false,
    providerConfig: {},
    onSave() {},
    onRefresh() {},
  }))
  assert.match(html, /邮件 Newsletter/)
  assert.match(html, /只读 IMAPS · 凭据已配置 · 1 个发件人规则/)
  assert.match(html, /imap\.vendor\.example · INBOX/)
  assert.doesNotMatch(html, /test-only-mail-password/)
})

test('sidebar exposes sources as a dedicated page', () => {
  const html = renderToStaticMarkup(React.createElement(Sidebar, {
    active: 'sources',
    onNavigate() {},
    online: true,
    username: 'admin',
  }))
  assert.match(html, /aria-label="信息源"/)
  assert.match(html, /aria-current="page"/)
})
