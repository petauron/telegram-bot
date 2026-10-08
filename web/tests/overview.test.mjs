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
const { OverviewView } = await vite.ssrLoadModule('/src/components/OverviewView.jsx')
const {
  MessageStreamView,
  chatsWithMessages,
} = await vite.ssrLoadModule('/src/components/MessageStreamView.jsx')
const {
  activeAdvancedFilterCount,
  DEFAULT_MIN_SCORE,
} = await vite.ssrLoadModule('/src/components/Filters.jsx')
const { StatusView } = await vite.ssrLoadModule('/src/components/StatusView.jsx')
const { filterQueueSamples } = await vite.ssrLoadModule('/src/components/LiveQueueChart.jsx')
const {
  INITIAL_FILTERS,
  queueHistoryFromStatus,
  shouldLoadCachedResource,
  shouldPollPage,
  pollIntervalMs,
} = await vite.ssrLoadModule('/src/App.jsx')

after(async () => {
  await vite.close()
})

const row = {
  id: 17,
  sent_at: '2026-08-09T08:30:00Z',
  score: 86,
  ai_score: 86,
  text: '某产品发布重要安全更新',
  chat_name: '资讯频道',
  sender_name: '发布者',
  reasons: ['发布 +25'],
  reply_count: 1,
  ai_status: 'success',
  ai_category: 'external_information',
  ai_category_label: '外部资讯',
  prefilter_status: 'passed',
  push_eligible: true,
  push_gate_reason: 'eligible',
}

test('overview is a dedicated summary without message stream controls', () => {
  const html = renderToStaticMarkup(React.createElement(OverviewView, {
    stats: {
      window_messages: 21,
      important_messages: 4,
      immediate_pushes: 2,
      digest_pushes: 1,
      prefiltered_messages: 6,
      non_information_messages: 8,
      eligible_messages: 4,
      analysis_errors: 1,
      active_chats: 3,
      immediate_score: 60,
      latest_message_at: row.sent_at,
    },
    rows: [row],
    online: true,
    loading: false,
    onOpenMessage() {},
    onOpenMessages() {},
  }))
  assert.match(html, /资讯总览/)
  assert.match(html, /值得关注/)
  assert.match(html, /处理概况/)
  assert.match(html, /服务状态/)
  assert.match(html, /查看全部消息/)
  assert.match(html, /资讯合格/)
  assert.doesNotMatch(html, /消息筛选/)
  assert.doesNotMatch(html, /消息详情/)
})

test('message stream keeps compact primary filters and opens details in a drawer', () => {
  const html = renderToStaticMarkup(React.createElement(MessageStreamView, {
    filters: {
      q: '',
      chatId: '',
      minScore: DEFAULT_MIN_SCORE,
      hours: '24',
      pushStatus: 'all',
      prefilterStatus: 'exclude',
      similarStatus: 'exclude',
    },
    chats: [],
    onFiltersChange() {},
    rows: [row],
    total: 1,
    selected: row,
    onSelect() {},
    page: 1,
    pageSize: 20,
    onPage() {},
    loading: false,
    form: null,
    onFormChange() {},
    onSave() {},
    saving: false,
    onReanalyze() {},
    analyzing: false,
  }))
  assert.match(html, /<h2>消息流<\/h2>/)
  assert.match(html, /aria-label="消息筛选"/)
  assert.match(html, /更多筛选/)
  assert.match(html, /默认显示 AI 评分 60 分以上/)
  assert.match(html, /消息详情/)
  assert.match(html, /消息内容/)
  assert.doesNotMatch(html, /aria-label="前置过滤状态"/)
  assert.doesNotMatch(html, /处理概况/)
  assert.doesNotMatch(html, /服务状态/)
})

test('advanced filter badge only counts non-default conditions', () => {
  assert.equal(INITIAL_FILTERS.minScore, '60')
  assert.equal(INITIAL_FILTERS.similarStatus, 'exclude')
  assert.equal(activeAdvancedFilterCount({ minScore: '60', pushStatus: 'all', prefilterStatus: 'exclude' }), 0)
  assert.equal(activeAdvancedFilterCount({ minScore: '0', pushStatus: 'all', prefilterStatus: 'exclude' }), 1)
  assert.equal(activeAdvancedFilterCount({ minScore: '50', pushStatus: 'all', prefilterStatus: 'exclude' }), 1)
  assert.equal(activeAdvancedFilterCount({ minScore: '50', pushStatus: 'digest', prefilterStatus: 'filtered' }), 3)
  assert.equal(activeAdvancedFilterCount({ minScore: '60', pushStatus: 'all', prefilterStatus: 'exclude', similarStatus: 'all' }), 1)
})

test('message stream group filter only includes chats with stored messages', () => {
  const chats = [
    { chat_id: -1001, chat_name: '已有记录群', message_count: 7 },
    { chat_id: -1002, chat_name: '尚无记录群', message_count: 0 },
    { chat_id: -1003, chat_name: '未提供计数群' },
  ]
  assert.deepEqual(chatsWithMessages(chats).map((chat) => chat.chat_id), [-1001])

  const html = renderToStaticMarkup(React.createElement(MessageStreamView, {
    filters: {
      q: '',
      chatId: '',
      minScore: DEFAULT_MIN_SCORE,
      hours: '24',
      pushStatus: 'all',
      prefilterStatus: 'exclude',
      similarStatus: 'exclude',
    },
    chats,
    onFiltersChange() {},
    rows: [],
    total: 0,
    selected: null,
    onSelect() {},
    page: 1,
    pageSize: 20,
    onPage() {},
    loading: false,
    form: null,
    onFormChange() {},
    onSave() {},
    saving: false,
    onReanalyze() {},
    analyzing: false,
  }))
  assert.match(html, /已有记录群/)
  assert.doesNotMatch(html, /尚无记录群/)
  assert.doesNotMatch(html, /未提供计数群/)
})

test('only live data pages poll and cached configuration is not repeatedly fetched', () => {
  assert.equal(shouldPollPage('overview'), true)
  assert.equal(shouldPollPage('messages'), true)
  assert.equal(shouldPollPage('sources'), true)
  assert.equal(shouldPollPage('status'), true)
  assert.equal(shouldPollPage('config'), false)
  assert.equal(shouldPollPage('push'), false)
  assert.equal(pollIntervalMs('sources'), 5_000)
  assert.equal(pollIntervalMs('status'), 10_000)
  assert.equal(pollIntervalMs('overview'), 30_000)
  assert.equal(shouldLoadCachedResource(false), true)
  assert.equal(shouldLoadCachedResource(true), false)
  assert.equal(shouldLoadCachedResource(true, true), true)
})

test('status page prioritizes health and live queue pressure while hiding technical noise', () => {
  const sampleTime = new Date(row.sent_at).getTime()
  const html = renderToStaticMarkup(React.createElement(StatusView, {
    online: true,
    loading: false,
    stats: {
      heartbeat: { connected: true, watch_count: 2, updated_at: row.sent_at },
      latest_message_at: row.sent_at,
      analysis_queue: {
        pending: 7,
        processing: 2,
        retry: 3,
        failed: 1,
        success_rate: 91.2,
        error_rate: 8.8,
        error_categories: [{ category: 'network_error', count: 3 }],
        health: 'degraded',
      },
      delivery_queue: {
        pending: 4,
        processing: 1,
        retry: 2,
        failed: 1,
        success_rate: 80,
        error_rate: 20,
        error_categories: [{ category: 'delivery_failed', count: 1 }],
        health: 'degraded',
      },
    },
    history: [
      { timestamp: sampleTime - 5_000, analysisPending: 8, analysisProcessing: 1 },
      { timestamp: sampleTime, analysisPending: 7, analysisProcessing: 2 },
    ],
  }))
  assert.match(html, /服务需要关注/)
  assert.match(html, /监听来源/)
  assert.match(html, /待分析/)
  assert.match(html, /分析队列/)
  assert.match(html, /data-chart-library="recharts"/)
  assert.match(html, /recharts-responsive-container/)
  assert.doesNotMatch(html, /<svg class="queue-live-chart"/)
  assert.match(html, /通知投递/)
  assert.match(html, /查看技术详情/)
  assert.doesNotMatch(html, /安全边界/)
  assert.doesNotMatch(html, /个人账号会话已载入/)
  assert.match(html, /91.2%/)
  assert.match(html, /8.8%/)
  assert.match(html, /network_error/)
  assert.match(html, /delivery_failed/)
})

test('status chart normalizes persisted server history and ranges only show recent samples', () => {
  const now = Date.parse('2026-08-13T03:00:00Z')
  const samples = queueHistoryFromStatus({
    queue_history: [
      { sampled_at: new Date(now - 61 * 60_000).toISOString(), analysis_pending: 11, analysis_processing: 2 },
      { sampled_at: new Date(now - 10 * 60_000).toISOString(), analysis_pending: 9, analysis_processing: 3 },
      { sampled_at: new Date(now).toISOString(), analysis_pending: 7, analysis_processing: 4 },
      { sampled_at: 'invalid', analysis_pending: 99, analysis_processing: 99 },
    ],
  })
  assert.equal(samples.length, 3)
  assert.deepEqual(filterQueueSamples(samples, 15 * 60_000, now), [
    { timestamp: now - 10 * 60_000, pending: 9, processing: 3 },
    { timestamp: now, pending: 7, processing: 4 },
  ])
  assert.deepEqual(queueHistoryFromStatus({}), [])
})
