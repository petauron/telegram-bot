import { readFileSync } from 'node:fs'
import { chromium } from '../web/node_modules/playwright/index.mjs'


function readEnvFile(path) {
  if (!path) return {}
  const values = {}
  for (const rawLine of readFileSync(path, 'utf8').split(/\r?\n/)) {
    const line = rawLine.trim()
    if (!line || line.startsWith('#')) continue
    const separator = line.indexOf('=')
    if (separator < 1) continue
    const key = line.slice(0, separator).trim().replace(/^export\s+/, '')
    let value = line.slice(separator + 1).trim()
    if (value.length >= 2 && value[0] === value.at(-1) && ['"', "'"].includes(value[0])) {
      value = value.slice(1, -1)
    }
    values[key] = value
  }
  return values
}

const fileEnv = readEnvFile(process.env.QA_ENV_FILE)
const baseURL = process.env.QA_BASE_URL || 'http://127.0.0.1:8080'
const username = process.env.QA_USERNAME || fileEnv.WEB_USERNAME
const password = process.env.QA_PASSWORD || fileEnv.WEB_PASSWORD
const output = process.env.QA_OUTPUT_DIR || '/output'

if (!username || !password) throw new Error('QA credentials are required')

const browser = await chromium.launch({
  headless: true,
  ...(process.env.QA_CHROMIUM_NO_SANDBOX === 'true' ? { args: ['--no-sandbox'] } : {}),
  ...(process.env.QA_CHROMIUM_EXECUTABLE_PATH
    ? { executablePath: process.env.QA_CHROMIUM_EXECUTABLE_PATH }
    : {}),
})
const consoleIssues = []

function shotPath(kind, page) {
  return `${output}/${kind}-${page}.png`
}

async function assertHealthyPage(page, viewport) {
  if (!page.url().startsWith(baseURL)) throw new Error(`Unexpected page URL: ${page.url()}`)
  if ((await page.title()) !== '群讯雷达 · 重点消息控制台') throw new Error('Page title is unexpected')
  const minimumContentLength = await page.locator('.auth-page').count() ? 15 : 70
  if ((await page.locator('body').innerText()).trim().length < minimumContentLength) throw new Error('Page content is blank')
  const overlays = page.locator('vite-error-overlay, nextjs-portal, [data-nextjs-dialog-overlay], .webpack-dev-server-client-overlay')
  if (await overlays.count()) throw new Error('Framework error overlay is visible')
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)
  if (overflow > 4) throw new Error(`${viewport.width}px page has ${overflow}px horizontal overflow`)
}

async function capture(page, path) {
  const sensitiveMessageContent = page.locator([
    '.user-chip span',
    '.admin-row span',
    '.attention-copy > strong',
    '.attention-copy small',
    '.message-title',
    '.message-meta',
    '.drawer-summary h3',
    '.drawer-summary > p',
    '.summary-meta',
    '.feedback-table td:nth-child(2)',
    '[aria-label="ntfy 反馈 Topic"]',
  ].join(', '))
  await page.screenshot({
    path,
    fullPage: false,
    animations: 'disabled',
    mask: [sensitiveMessageContent],
    maskColor: '#e8edf4',
  })
}

async function visit(viewport, kind, checkInvalidLogin) {
  const context = await browser.newContext({ viewport, locale: 'zh-CN' })
  const page = await context.newPage()
  const pageConsoleIssues = []
  let expectedUnauthorizedResponses = 0

  page.on('response', (response) => {
    if (response.status() !== 401) return
    const pathname = new URL(response.url()).pathname
    if (pathname === '/api/auth/login' || pathname === '/api/stats') expectedUnauthorizedResponses += 1
  })
  page.on('console', (message) => {
    if (message.type() === 'error' || message.type() === 'warning') {
      pageConsoleIssues.push(`${viewport.width}px ${message.type()}: ${message.text()}`)
    }
  })

  await page.goto(baseURL, { waitUntil: 'networkidle' })
  await page.getByRole('heading', { name: '登录管理界面' }).waitFor()
  for (const explanatoryText of [
    '使用服务器配置的管理员账号登录。',
    '会话使用 HttpOnly Cookie，退出后立即失效。',
    '无法登录？请检查服务器的 WEB_USERNAME / WEB_PASSWORD 配置。',
  ]) {
    if (await page.getByText(explanatoryText, { exact: true }).count()) {
      throw new Error('Login page still contains explanatory copy')
    }
  }
  await assertHealthyPage(page, viewport)
  await capture(page, shotPath(kind, 'login'))

  if (checkInvalidLogin) {
    await page.getByLabel('用户名').fill(username)
    await page.getByLabel('密码').fill(`invalid-${crypto.randomUUID()}`)
    await page.getByRole('button', { name: '登录', exact: true }).click()
    await page.getByText('用户名或密码不正确。', { exact: true }).waitFor()
  }

  await page.getByLabel('用户名').fill(username)
  await page.getByLabel('密码').fill(password)
  await page.getByRole('button', { name: '登录', exact: true }).click()
  await page.getByRole('heading', { name: '资讯总览', exact: true, level: 2 }).waitFor()
  await page.waitForLoadState('networkidle')
  await page.getByLabel(`当前管理员 ${username}`).waitFor()
  if ((await page.evaluate(() => localStorage.length + sessionStorage.length)) !== 0) {
    throw new Error('Authentication data appeared in Web Storage')
  }
  if (await page.evaluate(() => document.cookie.length > 0)) {
    throw new Error('A session cookie is visible to JavaScript')
  }
  if (await page.getByText('SSH 隧道', { exact: true }).count()) {
    throw new Error('Sidebar still exposes the deployment-specific SSH tunnel label')
  }
  const protectedWhileLoggedIn = await page.evaluate(() => fetch('/api/stats').then((response) => response.status))
  if (protectedWhileLoggedIn !== 200) throw new Error(`Authenticated API returned ${protectedWhileLoggedIn}`)

  await page.getByRole('heading', { name: '值得关注', exact: true, level: 3 }).waitFor()
  await page.getByRole('heading', { name: '处理概况', exact: true, level: 3 }).waitFor()
  const attentionAudit = await page.evaluate(async () => {
    const messages = await fetch('/api/messages?hours=24&prefilter_status=all&attention_only=true&limit=20').then((response) => response.json())
    return {
      valid: messages.items.every((row) => (
        row.prefilter_status === 'passed'
        && row.ai_status === 'success'
        && ['news', 'community_signal', 'benefit_deal'].includes(row.content_kind)
        && row.ai_score != null
        && row.push_eligible
      )),
    }
  })
  if (!attentionAudit.valid) throw new Error('Attention overview contains a non-pushable pipeline state')
  if (viewport.width > 760) await page.getByRole('heading', { name: '服务状态', exact: true, level: 3 }).waitFor()
  if (await page.locator('.message-stream-view').count()) throw new Error('Message stream rendered inside overview')
  await assertHealthyPage(page, viewport)
  await capture(page, shotPath(kind, 'overview'))

  const feedbackPayload = {
    summary: {
      total: 4,
      up: 3,
      down: 1,
      event_count: 5,
      positive_rate: 75,
      minimum_samples: 3,
      learning_mode: 'weak_signal',
    },
    by_content_kind: [
      { content_kind: 'news', label: '新闻资讯', total: 3, up: 2, down: 1 },
      { content_kind: 'community_signal', label: '社区线索', total: 1, up: 1, down: 0 },
    ],
    items: [{
      message_row_id: 880007,
      vote: 'up',
      voted_at: new Date().toISOString(),
      event_count: 2,
      title: '去标识化平台发布重要更新',
      source_name: '去标识化官方来源',
      content_kind: 'news',
      content_kind_label: '新闻资讯',
      ai_score: 82,
    }],
  }
  const feedbackRoute = async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(feedbackPayload),
    })
  }
  await page.route('**/api/feedback?*', feedbackRoute)
  await page.getByRole('button', { name: '反馈记录', exact: true }).click()
  await page.getByRole('heading', { name: '反馈记录', exact: true, level: 2 }).waitFor()
  await page.getByLabel('最近 90 天反馈汇总').getByText('75% 好评', { exact: true }).waitFor()
  await page.getByText('去标识化平台发布重要更新', { exact: true }).waitFor()
  await page.getByText('修改 1 次', { exact: true }).waitFor()
  const feedbackBody = await page.locator('.feedback-view').innerText()
  if (feedbackBody.includes('public_id') || feedbackBody.includes('source_key')) {
    throw new Error('Feedback page exposed an internal identifier')
  }
  await assertHealthyPage(page, viewport)
  await capture(page, shotPath(kind, 'feedback'))
  await page.unroute('**/api/feedback?*', feedbackRoute)

  const qaLastSuccess = new Date(Date.now() - 60_000).toISOString()
  const qaNextPoll = new Date(Date.now() + 15 * 60_000).toISOString()
  let qaSources = [{
    id: 880001,
    kind: 'rss',
    name: '示例厂商公告',
    url: 'https://feeds.example.invalid/official.xml',
    enabled: true,
    initialized: true,
    poll_interval_minutes: 15,
    poll_state: 'idle',
    item_count: 12,
    message_count: 2,
    last_success_at: qaLastSuccess,
    next_poll_at: qaNextPoll,
    consecutive_failures: 0,
  }, {
    id: 880003,
    kind: 'rss',
    name: '正在更新的状态页',
    url: 'https://status.example.invalid/feed.xml',
    enabled: true,
    initialized: true,
    poll_interval_minutes: 15,
    poll_state: 'processing',
    item_count: 8,
    message_count: 1,
    last_success_at: qaLastSuccess,
    next_poll_at: qaNextPoll,
    consecutive_failures: 0,
  }, {
    id: 880004,
    kind: 'github_releases',
    name: '额度受限的发布源',
    url: 'https://github.com/example/tool/releases',
    settings: { repository: 'example/tool', include_prereleases: false },
    enabled: true,
    initialized: true,
    poll_interval_minutes: 15,
    poll_state: 'error',
    item_count: 5,
    message_count: 1,
    last_success_at: qaLastSuccess,
    next_poll_at: qaNextPoll,
    consecutive_failures: 2,
    last_error_category: 'rate_limited',
  }]
  const sourcesRoute = async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    const sourceMatch = url.pathname.match(/^\/api\/sources\/(\d+)$/)
    const refreshMatch = url.pathname.match(/^\/api\/sources\/(\d+)\/refresh$/)
    if (url.pathname === '/api/sources' && request.method() === 'GET') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: qaSources }) })
      return
    }
    if (url.pathname === '/api/sources' && request.method() === 'POST') {
      if (request.headers()['x-requested-with'] !== 'admin-ui') {
        throw new Error('Information source save omitted the CSRF header')
      }
      const payload = request.postDataJSON()
      const item = {
        id: 880002,
        kind: payload.kind,
        name: payload.name,
        url: payload.kind === 'github_releases'
          ? `https://github.com/${payload.url}/releases`
          : payload.kind === 'github_advisories'
            ? 'https://github.com/advisories?query=type%3Areviewed'
            : payload.kind === 'cisa_kev'
              ? 'https://www.cisa.gov/known-exploited-vulnerabilities-catalog'
              : payload.kind === 'nvd_cve'
                ? 'https://nvd.nist.gov/vuln/search'
                : payload.kind === 'vendor_status'
                  ? payload.url
                  : payload.kind === 'hacker_news'
                    ? 'https://news.ycombinator.com/'
                    : payload.kind === 'bluesky'
                      ? `https://bsky.app/profile/${payload.bluesky_handle.toLowerCase()}`
                      : payload.kind === 'mastodon'
                        ? `${payload.mastodon_instance_url}/@${payload.mastodon_target}`
                        : payload.kind === 'newsletter_imap'
                          ? `imaps://${payload.imap_host}:993/${payload.imap_mailbox}?account=masked`
              : payload.url,
        settings: payload.kind === 'github_releases' ? {
          repository: payload.url,
          include_prereleases: payload.include_prereleases,
        } : payload.kind === 'github_advisories' ? {
          ecosystem: payload.ecosystem,
          minimum_severity: payload.minimum_severity,
          keywords: payload.keywords,
        } : payload.kind === 'nvd_cve' ? { keywords: payload.keywords }
          : payload.kind === 'hacker_news' ? { story_list: payload.story_list, minimum_score: payload.minimum_score, keywords: payload.keywords.map((value) => value.toLowerCase()) }
          : payload.kind === 'bluesky' ? { handle: payload.bluesky_handle.toLowerCase() }
          : payload.kind === 'mastodon' ? { instance_url: payload.mastodon_instance_url, timeline_type: payload.mastodon_timeline_type, target: payload.mastodon_target }
          : payload.kind === 'newsletter_imap' ? { host: payload.imap_host, port: payload.imap_port, username: payload.imap_username, mailbox: payload.imap_mailbox, sender_allowlist: payload.sender_allowlist }
          : {},
        secret_configured: ['mastodon', 'newsletter_imap'].includes(payload.kind) && Boolean(payload.source_secret),
        enabled: payload.enabled,
        initialized: false,
        poll_interval_minutes: payload.poll_interval_minutes,
        poll_state: payload.enabled ? 'idle' : 'disabled',
        item_count: 0,
        message_count: 0,
        next_poll_at: '2026-08-11T01:20:00Z',
        consecutive_failures: 0,
      }
      qaSources = [...qaSources, item]
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ item }) })
      return
    }
    if (sourceMatch && request.method() === 'PUT') {
      const id = Number(sourceMatch[1])
      const payload = request.postDataJSON()
      const item = { ...qaSources.find((source) => source.id === id), ...payload, poll_interval_minutes: payload.poll_interval_minutes }
      qaSources = qaSources.map((source) => (source.id === id ? item : source))
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ item }) })
      return
    }
    if (refreshMatch && request.method() === 'POST') {
      const item = qaSources.find((source) => source.id === Number(refreshMatch[1]))
      const refreshed = { ...item, poll_state: 'processing' }
      qaSources = qaSources.map((source) => (source.id === refreshed.id ? refreshed : source))
      await new Promise((resolve) => setTimeout(resolve, 700))
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ item: refreshed }) })
      return
    }
    await route.continue()
  }
  await page.route('**/api/sources', sourcesRoute)
  await page.route('**/api/sources/**', sourcesRoute)
  const providerRoute = async (route) => {
    if (route.request().method() === 'GET') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ github_token_configured: false, nvd_api_key_configured: false, updated_at: null }) })
      return
    }
    await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ github_token_configured: true, nvd_api_key_configured: true, updated_at: '2026-08-11T01:00:00Z' }) })
  }
  await page.route('**/api/source-provider-config', providerRoute)
  await page.getByRole('button', { name: '信息源', exact: true }).click()
  await page.getByRole('heading', { name: '信息源', exact: true, level: 2 }).waitFor()
  await page.getByText('示例厂商公告', { exact: true }).waitFor()
  const sourceSummary = page.getByLabel('信息源运行概况')
  await sourceSummary.getByText('1 定时运行', { exact: true }).waitFor()
  await sourceSummary.getByText('1 正在抓取', { exact: true }).waitFor()
  await sourceSummary.getByText('1 频率受限', { exact: true }).waitFor()
  await page.getByText('1 个 GitHub 来源触发匿名额度限制；可在下方配置免费 Personal access token 提高额度。', { exact: true }).waitFor()
  const firstSourceRow = page.locator('.source-table-row').filter({ hasText: '示例厂商公告' })
  const refreshRequest = page.waitForRequest((request) => (
    new URL(request.url()).pathname === '/api/sources/880001/refresh'
    && request.method() === 'POST'
  ))
  await firstSourceRow.getByRole('button', { name: '立即抓取', exact: true }).click()
  await firstSourceRow.getByRole('button', { name: '安排中…', exact: true }).waitFor()
  await refreshRequest
  await firstSourceRow.getByText('正在抓取', { exact: true }).waitFor({ timeout: 5000 })
  await sourceSummary.getByText('2 正在抓取', { exact: true }).waitFor()
  await page.getByLabel('来源类型').selectOption('newsletter_imap')
  await page.getByLabel('显示名称').fill('示例技术简报')
  await page.getByLabel('IMAP 主机').fill('imap.example.invalid')
  await page.getByLabel('邮箱账号').fill('reader@example.invalid')
  await page.getByLabel('邮箱目录').fill('INBOX')
  await page.getByLabel('发件人白名单').fill('vendor.example.invalid')
  await page.getByLabel('邮箱密码 / 应用专用密码').fill('test-only-mail-password')
  await page.getByLabel('采集间隔').selectOption('60')
  await page.getByRole('button', { name: '添加信息源', exact: true }).click()
  await page.getByText('示例技术简报', { exact: true }).waitFor()
  await page.getByText('只读 IMAPS · 凭据已配置 · 1 个发件人规则', { exact: true }).waitFor()
  if ((await page.locator('body').innerText()).includes('test-only-mail-password')) throw new Error('Newsletter password was rendered')
  await page.getByText('首次成功抓取后建立基线', { exact: true }).waitFor()
  await page.getByRole('heading', { name: '已配置的信息源', exact: true, level: 3 }).waitFor()
  const sourceLayoutBox = await page.locator('.sources-layout').boundingBox()
  const sourceListBox = await page.locator('.source-list').boundingBox()
  if (!sourceLayoutBox || !sourceListBox) throw new Error('Information source layout is not measurable')
  if (Math.abs(sourceListBox.x - sourceLayoutBox.x) > 2 || sourceListBox.width < sourceLayoutBox.width - 2) {
    throw new Error('Configured source list does not span the full layout width')
  }
  const sourceListPrecedesForms = await page.locator('.sources-view').evaluate((view) => {
    const list = view.querySelector('.source-list')
    const forms = view.querySelector('.sources-layout')
    return Boolean(list && forms && (list.compareDocumentPosition(forms) & Node.DOCUMENT_POSITION_FOLLOWING))
  })
  if (!sourceListPrecedesForms) throw new Error('Configured source table is not placed before configuration forms')
  const sourceFormPosition = await page.locator('.source-form').first().evaluate((form) => getComputedStyle(form).position)
  if (sourceFormPosition === 'sticky' || sourceFormPosition === 'fixed') throw new Error('Configuration form can overlap source rows while scrolling')
  await page.getByRole('table').waitFor()
  const sourceRows = page.locator('.source-table-row')
  if ((await sourceRows.count()) !== qaSources.length) throw new Error('Configured sources are not rendered as one compact table row per source')
  const sourceRowOverlap = await sourceRows.evaluateAll((rows) => rows.some((row, index) => index > 0 && row.getBoundingClientRect().top < rows[index - 1].getBoundingClientRect().bottom - 1))
  if (sourceRowOverlap) throw new Error('Configured source table rows overlap while scrolling')
  await assertHealthyPage(page, viewport)
  await capture(page, shotPath(kind, 'sources'))
  await page.unroute('**/api/sources', sourcesRoute)
  await page.unroute('**/api/sources/**', sourcesRoute)
  await page.unroute('**/api/source-provider-config', providerRoute)

  const chatOptionsResponse = page.waitForResponse((response) => {
    const url = new URL(response.url())
    return url.pathname === '/api/chats'
      && url.searchParams.get('recorded_only') === 'true'
      && response.status() === 200
  }, { timeout: 120_000 })
  const messageListResponse = page.waitForResponse((response) => {
    const url = new URL(response.url())
    return url.pathname === '/api/messages'
      && url.searchParams.get('limit') === '20'
      && url.searchParams.get('attention_only') !== 'true'
      && response.status() === 200
  }, { timeout: 120_000 })
  await page.getByRole('button', { name: '消息流', exact: true }).click()
  await page.getByRole('heading', { name: '消息流', exact: true, level: 2 }).waitFor()
  await page.getByLabel('消息筛选').waitFor()
  if (await page.getByLabel('前置过滤状态').count()) throw new Error('Advanced filters are expanded by default')
  const [chatResponse] = await Promise.all([chatOptionsResponse, messageListResponse])
  const chatData = await chatResponse.json()
  const expectedChatOptions = chatData.items
  await page.waitForFunction(
    (expectedCount) => document.querySelector('[aria-label="来源筛选"]')?.options.length === expectedCount + 1,
    expectedChatOptions.length,
    { timeout: 10_000 },
  )
  const renderedChatOptions = await page.getByLabel('来源筛选').locator('option').allTextContents()
  if (renderedChatOptions.length !== expectedChatOptions.length + 1) {
    throw new Error('Message stream chat filter includes chats without records')
  }
  await page.getByRole('button', { name: /更多筛选/ }).click()
  const scoreSelect = page.getByLabel('最低评分')
  await scoreSelect.waitFor()
  if ((await scoreSelect.inputValue()) !== '60') throw new Error('Message stream does not default to AI score 60+')
  const defaultScoreAudit = await page.evaluate(async () => {
    const response = await fetch('/api/messages?hours=24&min_score=60&prefilter_status=exclude&limit=100')
    const payload = await response.json()
    return payload.items.every((row) => row.ai_status === 'success' && row.ai_score != null && row.ai_score >= 60)
  })
  if (!defaultScoreAudit) throw new Error('Default message stream score filter returned an invalid AI score')
  const score20Response = page.waitForResponse((response) => {
    const url = new URL(response.url())
    return url.pathname === '/api/messages' && url.searchParams.get('min_score') === '20' && response.status() === 200
  })
  await scoreSelect.selectOption('20')
  const score20Payload = await (await score20Response).json()
  if (!score20Payload.items.every((row) => row.ai_status === 'success' && row.ai_score != null && row.ai_score >= 20)) {
    throw new Error('20+ score filter returned a zero, missing, failed, or local-only score')
  }
  const prefilterSelect = page.getByLabel('前置过滤状态')
  await prefilterSelect.waitFor()
  if ((await prefilterSelect.inputValue()) !== 'exclude') throw new Error('Prefiltered messages are not hidden by default')
  const filteredResponse = page.waitForResponse((response) => {
    const url = new URL(response.url())
    return url.pathname === '/api/messages' && url.searchParams.get('prefilter_status') === 'filtered' && response.status() === 200
  })
  await prefilterSelect.selectOption('filtered')
  await filteredResponse
  const excludedResponse = page.waitForResponse((response) => {
    const url = new URL(response.url())
    return url.pathname === '/api/messages' && url.searchParams.get('prefilter_status') === 'exclude' && response.status() === 200
  })
  await prefilterSelect.selectOption('exclude')
  await excludedResponse
  const similarSelect = page.getByLabel('相似资讯状态')
  if ((await similarSelect.inputValue()) !== 'exclude') throw new Error('Suppressed similar news is not hidden by default')
  await page.getByRole('button', { name: /更多筛选/ }).click()
  await assertHealthyPage(page, viewport)
  await capture(page, shotPath(kind, 'messages'))

  let messageRows = page.locator('.message-table tbody tr')
  if ((await messageRows.count()) === 0) {
    await page.getByLabel('时间范围').selectOption('72')
    await page.waitForLoadState('networkidle')
    messageRows = page.locator('.message-table tbody tr')
  }
  if ((await messageRows.count()) === 0) throw new Error('No stored message is available for detail QA')
  await messageRows.first().click()
  const drawer = page.getByRole('dialog', { name: '消息详情' })
  await drawer.waitFor()
  await drawer.getByRole('tab', { name: '分析', exact: true }).click()
  await drawer.getByLabel('模型分析详情').waitFor()
  await drawer.getByText('队列状态', { exact: true }).waitFor()
  await drawer.getByRole('heading', { name: '逐渠道投递', exact: true }).waitFor()
  await drawer.getByRole('tab', { name: '原始响应', exact: true }).click()
  await drawer.getByLabel('模型原始响应').waitFor()
  await drawer.getByRole('tab', { name: '概览', exact: true }).click()
  await drawer.getByLabel('消息审计概览').waitFor()
  await assertHealthyPage(page, viewport)
  await capture(page, shotPath(kind, 'detail'))
  await drawer.getByRole('button', { name: '关闭消息详情' }).click()
  await drawer.waitFor({ state: 'detached' })

  const semanticRows = [
    {
      id: 990001,
      sent_at: '2026-08-10T08:00:00Z',
      created_at: '2026-08-10T08:00:01Z',
      text: '去标识化的同一事件跨来源转述。',
      chat_name: '示例资讯源',
      sender_name: '匿名',
      score: 82,
      local_score: 25,
      local_reasons: ['仅供审计'],
      ai_status: 'success',
      ai_category: 'external_information',
      ai_category_label: '外部资讯',
      ai_category_confidence: 97,
      ai_category_summary: '分类为外部资讯',
      ai_category_reason: '消息描述外部事件',
      ai_score: 82,
      ai_summary: '同一事件的另一来源转述',
      ai_reason: '高相关且时效强',
      prefilter_status: 'passed',
      push_eligible: false,
      push_gate_reason: 'semantic_duplicate_suppressed',
      semantic_dedupe_status: 'suppressed',
      semantic_dedupe_model: '低成本分类模型',
      semantic_dedupe_effort: 'low',
      semantic_dedupe_confidence: 98,
      semantic_dedupe_reason: '与代表消息描述同一事件且没有新增状态',
      semantic_dedupe_matched_message_id: 989999,
      semantic_dedupe_candidate_count: 3,
      semantic_dedupe_checked_at: '2026-08-10T08:00:05Z',
      notification_prepare_status: 'not_required_semantic_duplicate',
      analysis_queue_state: 'succeeded',
      deliveries: [],
    },
    {
      id: 990002,
      sent_at: '2026-08-10T08:05:00Z',
      created_at: '2026-08-10T08:05:01Z',
      text: '去标识化事件新增恢复状态。',
      chat_name: '示例资讯源',
      sender_name: '匿名',
      score: 80,
      local_score: 25,
      local_reasons: ['仅供审计'],
      ai_status: 'success',
      ai_category: 'external_information',
      ai_category_label: '外部资讯',
      ai_category_confidence: 98,
      ai_category_summary: '分类为外部资讯',
      ai_category_reason: '消息描述外部状态变化',
      ai_score: 80,
      ai_summary: '服务已确认恢复',
      ai_reason: '状态变化具有时效性',
      prefilter_status: 'passed',
      push_eligible: true,
      push_gate_reason: 'semantic_material_update',
      semantic_dedupe_status: 'material_update',
      semantic_dedupe_model: '低成本分类模型',
      semantic_dedupe_effort: 'medium',
      semantic_dedupe_confidence: 99,
      semantic_dedupe_reason: '同一事件新增恢复状态，属于实质更新',
      semantic_dedupe_matched_message_id: 989999,
      semantic_dedupe_update_type: 'service_status_change',
      semantic_dedupe_update_validated: true,
      semantic_dedupe_candidate_count: 3,
      semantic_dedupe_checked_at: '2026-08-10T08:05:05Z',
      notification_prepare_status: 'success',
      notification_prepare_model: '通知整理模型',
      notification_prepare_effort: 'high',
      notification_title: '服务已确认恢复',
      notification_body: '平台确认服务已经恢复。\n\n受影响用户可重新尝试。',
      notification_prepare_response_text: '{"title":"服务已确认恢复","body":"已整理正文"}',
      notification_prepare_checked_at: '2026-08-10T08:05:08Z',
      analysis_queue_state: 'succeeded',
      deliveries: [],
    },
    {
      id: 990003,
      sent_at: '2026-08-10T08:10:00Z',
      created_at: '2026-08-10T08:10:01Z',
      text: '去标识化的独立产品安全更新。',
      chat_name: '示例资讯源',
      sender_name: '匿名',
      score: 75,
      local_score: 25,
      local_reasons: ['仅供审计'],
      ai_status: 'success',
      ai_category: 'external_information',
      ai_category_label: '外部资讯',
      ai_category_confidence: 96,
      ai_category_summary: '产品安全更新',
      ai_category_reason: '消息描述外部产品事件',
      ai_score: 75,
      ai_summary: '产品发布安全更新',
      ai_reason: '信息具体且具有客户价值',
      prefilter_status: 'passed',
      push_eligible: true,
      push_gate_reason: 'eligible_notification_fallback',
      semantic_dedupe_status: 'unique',
      semantic_dedupe_model: '去重模型',
      semantic_dedupe_effort: 'default',
      semantic_dedupe_confidence: 97,
      semantic_dedupe_reason: '与候选不是同一事件',
      semantic_dedupe_candidate_count: 2,
      semantic_dedupe_checked_at: '2026-08-10T08:10:04Z',
      notification_prepare_status: 'failed_fallback',
      notification_prepare_model: '通知整理模型',
      notification_prepare_effort: 'low',
      notification_title: '产品发布安全更新',
      notification_body: '产品发布安全更新，受影响用户可按公告升级。',
      notification_prepare_error_category: 'invalid_response',
      notification_prepare_checked_at: '2026-08-10T08:10:08Z',
      analysis_queue_state: 'succeeded',
      deliveries: [],
    },
    {
      id: 990004,
      sent_at: '2026-08-10T08:12:00Z',
      created_at: '2026-08-10T08:12:01Z',
      text: '同一发布事件增加更多技术参数和附带话题。',
      chat_name: '示例资讯源',
      sender_name: '匿名',
      score: 85,
      local_score: 25,
      local_reasons: ['仅供审计'],
      ai_status: 'success',
      ai_category: 'external_information',
      ai_category_label: '外部资讯',
      ai_category_confidence: 96,
      ai_category_summary: '同一发布事件的补充细节',
      ai_category_reason: '消息描述外部产品事件',
      ai_score: 85,
      ai_summary: '同一发布事件增加更多技术参数',
      ai_reason: '资讯评分成功',
      prefilter_status: 'passed',
      push_eligible: false,
      push_gate_reason: 'semantic_unverified_update_suppressed',
      semantic_dedupe_status: 'suppressed_unverified_update',
      semantic_dedupe_model: '去重模型',
      semantic_dedupe_effort: 'low',
      semantic_dedupe_confidence: 96,
      semantic_dedupe_reason: '模型识别到同一事件，但所称更新缺少可验证状态变化',
      semantic_dedupe_matched_message_id: 989999,
      semantic_dedupe_update_type: 'impact_status_change',
      semantic_dedupe_update_validated: false,
      semantic_dedupe_update_rejection_reason: '只有影响细节补充，没有实际影响状态变化',
      semantic_dedupe_candidate_count: 3,
      semantic_dedupe_checked_at: '2026-08-10T08:12:05Z',
      notification_prepare_status: 'not_required_semantic_duplicate',
      analysis_queue_state: 'succeeded',
      deliveries: [],
    },
    {
      id: 990005,
      sent_at: '2026-08-10T08:15:00Z',
      created_at: '2026-08-10T08:15:01Z',
      text: '去标识化讨论给出了故障复现条件和恢复步骤。',
      chat_name: '示例技术讨论群',
      sender_name: '匿名',
      score: 76,
      local_score: 0,
      local_reasons: [],
      ai_status: 'success',
      ai_category: 'discussion',
      ai_category_label: '讨论交流',
      ai_category_confidence: 94,
      ai_category_summary: '群内技术讨论',
      ai_category_reason: '消息是用户之间的技术交流',
      content_kind: 'community_signal',
      ai_score: 76,
      ai_summary: '连接异常已有可复现解决方法',
      ai_reason: '同一话题包含问题、步骤和结果',
      community_status: 'valuable',
      community_signal_type: 'technical_solution',
      community_confidence: 90,
      community_title: '连接异常已有可复现解决方法',
      community_summary: '调整配置后连接恢复，重复测试结果一致。',
      community_reason: '文本包含明确问题、操作步骤和恢复结果',
      community_model: '社区线索模型',
      community_effort: 'medium',
      community_evidence_count: 2,
      community_checked_at: '2026-08-10T08:15:04Z',
      prefilter_status: 'passed',
      push_eligible: true,
      push_gate_reason: 'eligible_notification_prepared',
      semantic_dedupe_status: 'unique_no_candidates',
      semantic_dedupe_candidate_count: 0,
      semantic_dedupe_checked_at: '2026-08-10T08:15:05Z',
      notification_prepare_status: 'success',
      notification_prepare_model: '社区线索模型',
      notification_prepare_effort: 'medium',
      notification_title: '连接异常已有可复现解决方法',
      notification_body: '调整配置后连接恢复，重复测试结果一致。',
      notification_prepare_checked_at: '2026-08-10T08:15:05Z',
      analysis_queue_state: 'succeeded',
      deliveries: [],
    },
    {
      id: 990006,
      sent_at: '2026-08-10T08:15:30Z',
      created_at: '2026-08-10T08:15:31Z',
      text: '去标识化的多人产品体验评价。',
      chat_name: '示例技术讨论群',
      sender_name: '匿名',
      score: 72,
      local_score: 0,
      local_reasons: [],
      ai_status: 'success',
      ai_category: 'discussion',
      ai_category_label: '讨论交流',
      ai_category_confidence: 94,
      ai_category_summary: '群内产品体验讨论',
      ai_category_reason: '消息是用户之间的产品体验交流',
      content_kind: 'community_signal',
      ai_score: 72,
      ai_summary: '某产品稳定性较好但售后体验有分歧',
      ai_reason: '同一产品有多人具体体验',
      community_status: 'valuable',
      community_signal_type: 'product_review',
      community_confidence: 91,
      community_title: '某产品稳定性较好但售后体验有分歧',
      community_summary: '多位参与者认可日常稳定性，对工单响应速度的体验有差异。',
      community_reason: '包含两条独立使用证据',
      community_model: '社区线索模型',
      community_effort: 'medium',
      community_evidence_count: 2,
      community_checked_at: '2026-08-10T08:15:34Z',
      prefilter_status: 'passed',
      push_eligible: true,
      push_gate_reason: 'eligible_notification_prepared',
      semantic_dedupe_status: 'unique_no_candidates',
      semantic_dedupe_candidate_count: 0,
      semantic_dedupe_checked_at: '2026-08-10T08:15:35Z',
      notification_prepare_status: 'success',
      notification_prepare_model: '社区线索模型',
      notification_prepare_effort: 'medium',
      notification_title: '某产品稳定性较好但售后体验有分歧',
      notification_body: '多位参与者认可日常稳定性，对工单响应速度的体验有差异。',
      notification_prepare_checked_at: '2026-08-10T08:15:35Z',
      analysis_queue_state: 'succeeded',
      deliveries: [],
    },
    {
      id: 990007,
      sent_at: '2026-08-10T08:16:00Z',
      created_at: '2026-08-10T08:16:01Z',
      text: '去标识化云服务器套餐已经补货并恢复下单。',
      chat_name: '示例优惠来源',
      sender_name: '匿名',
      score: 82,
      local_score: 0,
      local_reasons: [],
      ai_status: 'success',
      ai_category: 'promotion_spam',
      ai_category_label: '推广/垃圾',
      ai_category_confidence: 95,
      ai_category_summary: '产品补货信息',
      ai_category_reason: '包含明确命名产品与恢复下单状态',
      content_kind: 'benefit_deal',
      ai_score: 74,
      ai_summary: '云服务器套餐恢复下单',
      ai_reason: '厂商、套餐和库存状态变化明确',
      benefit_status: 'valuable',
      benefit_type: 'product_restock',
      benefit_confidence: 95,
      benefit_title: '云服务器套餐恢复下单',
      benefit_summary: '指定地区与系列已经补货，可以重新购买。',
      benefit_reason: '产品和库存恢复动作明确',
      benefit_model: '福利筛选模型',
      benefit_effort: 'medium',
      benefit_checked_at: '2026-08-10T08:16:04Z',
      prefilter_status: 'passed',
      push_eligible: true,
      push_gate_reason: 'eligible_notification_prepared',
      semantic_dedupe_status: 'unique_no_candidates',
      semantic_dedupe_candidate_count: 0,
      semantic_dedupe_checked_at: '2026-08-10T08:16:05Z',
      notification_prepare_status: 'success',
      notification_prepare_model: '福利筛选模型',
      notification_prepare_effort: 'medium',
      notification_title: '云服务器套餐恢复下单',
      notification_body: '指定地区与系列已经补货，可以重新购买。',
      notification_prepare_checked_at: '2026-08-10T08:16:05Z',
      analysis_queue_state: 'succeeded',
      deliveries: [],
    },
  ]
  const groupedRepresentative = semanticRows.find((row) => row.id === 990003)
  const groupedRepresentativeSummary = { ...groupedRepresentative }
  groupedRepresentative.similar_count = 2
  groupedRepresentative.similar_cluster = {
    representative: groupedRepresentativeSummary,
    similar_count: 2,
    items: semanticRows.filter((row) => [990001, 990004].includes(row.id)),
  }
  const semanticRoute = async (route) => {
    const url = new URL(route.request().url())
    const detailMatch = url.pathname.match(/^\/api\/messages\/(990001|990002|990003|990004|990005|990006|990007)$/)
    if (detailMatch) {
      const item = semanticRows.find((row) => row.id === Number(detailMatch[1]))
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(item) })
      return
    }
    if (url.pathname === '/api/messages') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ total: 7, items: semanticRows }) })
      return
    }
    await route.continue()
  }
  await page.route('**/api/messages/**', semanticRoute)
  await page.route('**/api/messages*', semanticRoute)
  await page.getByRole('button', { name: /更多筛选/ }).click()
  await page.getByLabel('相似资讯状态').selectOption('all')
  await page.getByRole('button', { name: /更多筛选/ }).click()
  const semanticListResponse = page.waitForResponse((response) => {
    const url = new URL(response.url())
    return url.pathname === '/api/messages' && url.searchParams.get('q') === '__qa_semantic__' && response.status() === 200
  })
  await page.getByLabel('搜索消息、来源或发送者').fill('__qa_semantic__')
  await semanticListResponse
  await page.getByText('同一事件的另一来源转述', { exact: true }).waitFor()
  await page.getByRole('button', { name: '查看 2 条相似资讯', exact: true }).click()
  const groupedDrawer = page.getByRole('dialog', { name: '消息详情' })
  await groupedDrawer.getByLabel('相似资讯关联').waitFor()
  await groupedDrawer.getByText('已归并到同一代表消息，不会重复推送。', { exact: true }).waitFor()
  await groupedDrawer.getByRole('button', { name: '关闭消息详情' }).click()
  const suppressedRow = page.locator('.message-table tbody tr').filter({ hasText: '相似资讯已抑制' }).first()
  await suppressedRow.waitFor()
  await suppressedRow.click()
  await page.getByRole('dialog', { name: '消息详情' }).getByText('同一事件已有代表消息', { exact: false }).waitFor()
  await page.getByRole('dialog', { name: '消息详情' }).getByText('与代表消息描述同一事件且没有新增状态', { exact: true }).waitFor()
  await capture(page, shotPath(kind, 'semantic-suppressed'))
  await page.getByRole('dialog', { name: '消息详情' }).getByRole('button', { name: '关闭消息详情' }).click()
  const semanticSearch = page.getByLabel('搜索消息、来源或发送者')
  await semanticSearch.fill('')
  await semanticSearch.fill('__qa_semantic__')
  await page.getByText('补充细节已抑制', { exact: true }).waitFor()
  await page.getByText('补充细节已抑制', { exact: true }).click()
  const rejectedUpdateDrawer = page.getByRole('dialog', { name: '消息详情' })
  await rejectedUpdateDrawer.getByText('只有影响细节补充，没有实际影响状态变化', { exact: true }).waitFor()
  await rejectedUpdateDrawer.getByRole('tab', { name: '分析', exact: true }).click()
  await rejectedUpdateDrawer.getByText('实际影响状态变化', { exact: true }).waitFor()
  await rejectedUpdateDrawer.getByText('未通过程序校验', { exact: true }).waitFor()
  await capture(page, shotPath(kind, 'semantic-unverified-update'))
  await rejectedUpdateDrawer.getByRole('button', { name: '关闭消息详情' }).click()
  await semanticSearch.fill('')
  await semanticSearch.fill('__qa_semantic__')
  await page.getByText('实质更新已放行', { exact: true }).waitFor()
  await page.getByText('实质更新已放行', { exact: true }).click()
  const updatedDrawer = page.getByRole('dialog', { name: '消息详情' })
  await updatedDrawer.getByText('同一事件新增恢复状态，属于实质更新', { exact: true }).waitFor()
  await updatedDrawer.getByRole('tab', { name: '分析', exact: true }).click()
  await updatedDrawer.getByRole('heading', { name: '最终客户通知预览', exact: true }).waitFor()
  const preparedPreview = updatedDrawer.getByLabel('最终客户通知预览')
  if (!(await preparedPreview.innerText()).includes('\n\n')) throw new Error('Prepared notification preview lost its paragraph break')
  await updatedDrawer.getByRole('button', { name: '关闭消息详情' }).click()
  await semanticSearch.fill('')
  await semanticSearch.fill('__qa_semantic__')
  await page.getByText('产品发布安全更新', { exact: true }).waitFor()
  await page.getByText('产品发布安全更新', { exact: true }).click()
  const fallbackDrawer = page.getByRole('dialog', { name: '消息详情' })
  await fallbackDrawer.getByText('整理失败并已安全回退，具备推送资格', { exact: true }).waitFor()
  await fallbackDrawer.getByText('整理失败，已安全回退', { exact: true }).waitFor()
  await fallbackDrawer.getByRole('tab', { name: '分析', exact: true }).click()
  await fallbackDrawer.getByRole('heading', { name: '最终客户通知预览', exact: true }).waitFor()
  await capture(page, shotPath(kind, 'notification-fallback'))
  await fallbackDrawer.getByRole('button', { name: '关闭消息详情' }).click()
  await semanticSearch.fill('')
  await semanticSearch.fill('__qa_semantic__')
  await page.getByText('连接异常已有可复现解决方法', { exact: true }).waitFor()
  await page.getByText('连接异常已有可复现解决方法', { exact: true }).click()
  const communityDrawer = page.getByRole('dialog', { name: '消息详情' })
  await communityDrawer.getByText('社区线索', { exact: true }).first().waitFor()
  await communityDrawer.getByRole('tab', { name: '分析', exact: true }).click()
  await communityDrawer.getByText('有效解决方案', { exact: true }).waitFor()
  await communityDrawer.getByText('支持消息', { exact: true }).waitFor()
  await capture(page, shotPath(kind, 'community-insight'))
  await communityDrawer.getByRole('button', { name: '关闭消息详情' }).click()
  await semanticSearch.fill('')
  await semanticSearch.fill('__qa_semantic__')
  await page.getByText('某产品稳定性较好但售后体验有分歧', { exact: true }).waitFor()
  await page.getByText('某产品稳定性较好但售后体验有分歧', { exact: true }).click()
  const productReviewDrawer = page.getByRole('dialog', { name: '消息详情' })
  await productReviewDrawer.getByText('产品口碑', { exact: true }).first().waitFor()
  await productReviewDrawer.getByText('产品口碑聚合', { exact: true }).waitFor()
  await productReviewDrawer.getByRole('tab', { name: '分析', exact: true }).click()
  await productReviewDrawer.getByText('支持消息', { exact: true }).waitFor()
  await capture(page, shotPath(kind, 'product-reputation'))
  await productReviewDrawer.getByRole('button', { name: '关闭消息详情' }).click()
  await semanticSearch.fill('')
  await semanticSearch.fill('__qa_semantic__')
  await page.getByText('云服务器套餐恢复下单', { exact: true }).waitFor()
  await page.getByText('云服务器套餐恢复下单', { exact: true }).click()
  const benefitDrawer = page.getByRole('dialog', { name: '消息详情' })
  await benefitDrawer.getByText('福利羊毛', { exact: true }).first().waitFor()
  await benefitDrawer.getByRole('tab', { name: '分析', exact: true }).click()
  await benefitDrawer.getByText('商品补货/恢复下单', { exact: true }).waitFor()
  await benefitDrawer.getByText('领取条件摘要', { exact: true }).waitFor()
  await capture(page, shotPath(kind, 'benefit-deal'))
  await benefitDrawer.getByRole('button', { name: '关闭消息详情' }).click()
  await page.unroute('**/api/messages/**', semanticRoute)
  await page.unroute('**/api/messages*', semanticRoute)

  let qaModelConfig = {
    enabled: true,
    community_insights_enabled: true,
    benefit_deals_enabled: true,
    base_url: 'http://fake-openai.invalid/v1',
    api_key_configured: true,
    classification_model: 'qa-existing-classifier',
    classification_reasoning_effort: 'low',
    model: 'qa-existing-scorer',
    reasoning_effort: 'default',
    semantic_dedupe_model: 'qa-existing-deduper',
    semantic_dedupe_reasoning_effort: 'low',
    notification_model: 'qa-existing-notifier',
    notification_reasoning_effort: 'low',
    updated_at: '2026-08-10T08:00:00Z',
  }
  const qaModels = ['qa-classifier', 'qa-deduper', 'qa-notifier', 'qa-scorer']
  const modelConfigRoute = async (route) => {
    const request = route.request()
    const url = new URL(request.url())
    if (url.pathname === '/api/model-config/models') {
      if (request.method() !== 'POST') throw new Error('Model refresh did not use POST')
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ items: qaModels }) })
      return
    }
    if (url.pathname === '/api/model-config' && request.method() === 'GET') {
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(qaModelConfig) })
      return
    }
    if (url.pathname === '/api/model-config' && request.method() === 'PUT') {
      const payload = request.postDataJSON()
      if (payload.api_key || request.headers()['x-requested-with'] !== 'admin-ui') {
        throw new Error('Model save exposed a key or omitted the CSRF header')
      }
      qaModelConfig = {
        enabled: Boolean(payload.enabled),
        community_insights_enabled: Boolean(payload.community_insights_enabled),
        benefit_deals_enabled: Boolean(payload.benefit_deals_enabled),
        base_url: payload.base_url,
        api_key_configured: true,
        classification_model: payload.classification_model,
        classification_reasoning_effort: payload.classification_reasoning_effort,
        model: payload.model,
        reasoning_effort: payload.reasoning_effort,
        semantic_dedupe_model: payload.semantic_dedupe_model,
        semantic_dedupe_reasoning_effort: payload.semantic_dedupe_reasoning_effort,
        notification_model: payload.notification_model,
        notification_reasoning_effort: payload.notification_reasoning_effort,
        updated_at: '2026-08-10T08:20:00Z',
      }
      await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(qaModelConfig) })
      return
    }
    await route.continue()
  }
  await page.route('**/api/model-config/models', modelConfigRoute)
  await page.route('**/api/model-config', modelConfigRoute)
  await page.getByRole('button', { name: '规则配置', exact: true }).click()
  await page.getByRole('heading', { name: '规则配置', exact: true, level: 2 }).waitFor()
  await page.getByRole('heading', { name: '监听群组与频道', exact: true }).waitFor()
  await page.getByRole('tab', { name: '资讯规则', exact: true }).click()
  await page.locator('.rule-editor').waitFor()
  await page.getByRole('tab', { name: '模型分析', exact: true }).click()
  await page.locator('.model-settings').waitFor()
  const communityToggle = page.getByLabel('启用社区线索分析')
  await communityToggle.waitFor()
  if (!(await communityToggle.isChecked())) throw new Error('Community insight analysis is not enabled')
  const benefitToggle = page.getByLabel('启用福利羊毛分析')
  await benefitToggle.waitFor()
  if (!(await benefitToggle.isChecked())) throw new Error('Benefit deal analysis is not enabled')
  const classificationModel = page.getByLabel('分类分析模型选择')
  const scoringModel = page.getByLabel('评分分析模型选择')
  const semanticModel = page.getByLabel('语义去重模型选择')
  const notificationModel = page.getByLabel('通知内容整理模型选择')
  const classificationEffort = page.getByLabel('分类分析推理档位')
  const scoringEffort = page.getByLabel('评分分析推理档位')
  const semanticEffort = page.getByLabel('语义去重推理档位')
  const notificationEffort = page.getByLabel('通知内容整理推理档位')
  await classificationModel.waitFor()
  if (!(await classificationModel.inputValue())) throw new Error('Classification model is not selected')
  for (const control of [classificationEffort, scoringEffort, semanticEffort, notificationEffort]) {
    if (!['default', 'low', 'medium', 'high'].includes(await control.inputValue())) {
      throw new Error('A reasoning effort control has an invalid value')
    }
  }
  const modelKey = page.getByLabel('模型 API Key')
  if ((await modelKey.getAttribute('type')) !== 'password' || (await modelKey.inputValue())) {
    throw new Error('Model API Key was exposed in the settings form')
  }
  await page.getByText('默认低推理；非资讯在此结束。', { exact: true }).waitFor()
  await page.getByText('AI 分是唯一推送判断分数。', { exact: true }).waitFor()
  await page.getByRole('button', { name: '刷新模型', exact: true }).click()
  await classificationModel.locator('option[value="qa-classifier"]').waitFor({ state: 'attached' })
  await classificationModel.selectOption('qa-classifier')
  await scoringModel.selectOption('qa-scorer')
  await semanticModel.selectOption('qa-deduper')
  await notificationModel.selectOption('qa-notifier')
  await classificationEffort.selectOption('low')
  await scoringEffort.selectOption('medium')
  await semanticEffort.selectOption('high')
  await notificationEffort.selectOption('default')
  await page.getByRole('button', { name: '保存模型配置', exact: true }).click()
  await page.getByText('模型配置已保存并立即生效；API Key 明文已从表单清除。', { exact: true }).waitFor()

  await page.reload({ waitUntil: 'networkidle' })
  await page.getByRole('heading', { name: '资讯总览', exact: true, level: 2 }).waitFor()
  await page.getByRole('button', { name: '规则配置', exact: true }).click()
  await page.getByRole('heading', { name: '规则配置', exact: true, level: 2 }).waitFor()
  await page.getByRole('tab', { name: '模型分析', exact: true }).click()
  await page.locator('.model-settings').waitFor()
  const persistedControls = [
    ['分类分析模型选择', 'qa-classifier'],
    ['评分分析模型选择', 'qa-scorer'],
    ['语义去重模型选择', 'qa-deduper'],
    ['通知内容整理模型选择', 'qa-notifier'],
    ['分类分析推理档位', 'low'],
    ['评分分析推理档位', 'medium'],
    ['语义去重推理档位', 'high'],
    ['通知内容整理推理档位', 'default'],
  ]
  for (const [label, expected] of persistedControls) {
    if ((await page.getByLabel(label).inputValue()) !== expected) {
      throw new Error(`${label} did not persist in browser QA`)
    }
  }
  await assertHealthyPage(page, viewport)
  await capture(page, shotPath(kind, 'settings'))

  await page.getByRole('button', { name: '推送渠道', exact: true }).click()
  await page.getByRole('heading', { name: '推送渠道', exact: true, level: 2 }).waitFor()
  await page.getByRole('tab', { name: /Telegram Bot/ }).click()
  const telegramToken = page.getByLabel('Telegram Bot Token')
  if ((await telegramToken.getAttribute('type')) !== 'password' || (await telegramToken.inputValue())) {
    throw new Error('Telegram Bot Token was exposed in the push form')
  }
  await page.getByRole('tab', { name: /ntfy/ }).click()
  await page.getByLabel('ntfy 手机通知示意').waitFor()
  const ntfyToken = page.getByLabel('ntfy Access Token')
  if ((await ntfyToken.getAttribute('type')) !== 'password' || (await ntfyToken.inputValue())) {
    throw new Error('ntfy Access Token was exposed in the push form')
  }
  if ((await page.getByLabel('ntfy 服务地址').inputValue()) !== 'https://ntfy.example.com') {
    throw new Error('ntfy service address is unexpected')
  }
  for (const label of ['ntfy 新闻资讯 Topic', 'ntfy 社区讨论 Topic', 'ntfy 福利羊毛 Topic']) {
    if (!(await page.getByLabel(label).inputValue())) throw new Error(`${label} is missing`)
  }
  const feedbackTopic = page.getByLabel('ntfy 反馈 Topic')
  if (!(await feedbackTopic.inputValue()) || await feedbackTopic.isEditable()) {
    throw new Error('ntfy feedback topic is missing or editable')
  }
  await page.getByText('精华标题', { exact: true }).waitFor()
  await page.getByText('自动优先级', { exact: true }).waitFor()
  await page.getByText('分段正文', { exact: true }).waitFor()
  await page.getByText('通用来源', { exact: true }).waitFor()
  await page.getByText('分数牌', { exact: true }).waitFor()
  await page.getByText('有用性反馈', { exact: true }).waitFor()
  await page.getByLabel('ntfy 反馈按钮示意').getByText('👍 有用', { exact: true }).waitFor()
  await page.getByLabel('ntfy 反馈按钮示意').getByText('👎 无用', { exact: true }).waitFor()
  const ntfyPreview = page.getByLabel('ntfy 手机通知示意')
  const ntfyPreviewText = await ntfyPreview.innerText()
  if (!ntfyPreviewText.includes('🟥92｜新闻｜')) throw new Error('ntfy preview is missing the color score and content type markers')
  if (!ntfyPreviewText.includes('查看原文')) throw new Error('ntfy preview is missing the safe external article link')
  if ((await ntfyPreview.locator('p').innerText()).split('\n').filter(Boolean).length < 2) {
    throw new Error('ntfy preview does not preserve readable paragraphs')
  }
  const previewHref = await ntfyPreview.getByRole('link', { name: '查看原文', exact: true }).getAttribute('href')
  if (!previewHref?.startsWith('https://example.com/') || previewHref.includes('t.me')) {
    throw new Error('ntfy preview external link is not platform neutral')
  }
  for (const forbidden of ['【最高优先】', '智能标签', 'AI：', '评分', '分类', '理由', '回复', 'Telegram 原消息', '点击跳转']) {
    if (ntfyPreviewText.includes(forbidden)) throw new Error(`ntfy preview exposes ${forbidden}`)
  }
  if (viewport.width <= 760) await ntfyPreview.scrollIntoViewIfNeeded()
  await assertHealthyPage(page, viewport)
  await capture(page, shotPath(kind, 'push'))
  await page.unroute('**/api/model-config/models', modelConfigRoute)
  await page.unroute('**/api/model-config', modelConfigRoute)

  await page.getByRole('button', { name: '运行状态', exact: true }).click()
  await page.getByRole('heading', { name: '运行状态', exact: true, level: 2 }).waitFor()
  await page.getByRole('heading', { name: /服务运行正常|服务需要关注/ }).waitFor()
  await page.getByRole('heading', { name: '分析队列', exact: true }).waitFor()
  await page.getByRole('heading', { name: '通知投递', exact: true }).waitFor()
  await page.locator('[data-chart-library="recharts"] .recharts-responsive-container').waitFor()
  await page.locator('[data-chart-library="recharts"] .recharts-area-area').waitFor()
  await page.getByText(/系统每 10 秒持久化 · 保留 3 天/).waitFor()
  await page.getByRole('button', { name: '1 小时', exact: true }).click()
  const primaryStatusContent = page.locator('.status-view > :not(.status-technical-details)')
  if ((await primaryStatusContent.getByText('滚动成功率', { exact: true }).count()) !== 0) {
    throw new Error('Technical queue rates leaked into the primary status view')
  }
  const technical = page.getByText('查看技术详情', { exact: true })
  await technical.waitFor()
  if (await page.locator('.technical-details').evaluate((element) => element.open)) {
    throw new Error('Technical details are expanded by default')
  }
  await assertHealthyPage(page, viewport)
  await capture(page, shotPath(kind, 'status'))

  await page.locator('.logout-button').click()
  await page.getByRole('heading', { name: '登录管理界面' }).waitFor()
  const protectedAfterLogout = await page.evaluate(() => fetch('/api/stats').then((response) => response.status))
  if (protectedAfterLogout !== 401) throw new Error(`Logged-out API returned ${protectedAfterLogout}`)
  await assertHealthyPage(page, viewport)

  const expectedNetworkDiagnostics = pageConsoleIssues.filter((issue) => (
    issue.includes('Failed to load resource: the server responded with a status of 401')
  ))
  const unexpectedIssues = pageConsoleIssues.filter((issue) => !expectedNetworkDiagnostics.includes(issue))
  if (expectedNetworkDiagnostics.length > expectedUnauthorizedResponses) {
    unexpectedIssues.push(`${viewport.width}px unexpected 401 console diagnostic`)
  }
  consoleIssues.push(...unexpectedIssues)
  await context.close()
}

await visit({ width: 1536, height: 1024 }, 'desktop', true)
await visit({ width: 390, height: 844 }, 'mobile', false)
await browser.close()

if (consoleIssues.length) throw new Error(`Browser console issues: ${consoleIssues.join(' | ')}`)
console.log('browser redesign QA passed')
