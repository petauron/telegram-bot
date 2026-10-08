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
const { FeedbackView } = await vite.ssrLoadModule('/src/components/FeedbackView.jsx')
const { Sidebar } = await vite.ssrLoadModule('/src/components/Sidebar.jsx')

after(async () => {
  await vite.close()
})

test('feedback page renders compact aggregates and records without internal identifiers', () => {
  const html = renderToStaticMarkup(React.createElement(FeedbackView, {
    loading: false,
    data: {
      summary: { total: 4, up: 3, down: 1, event_count: 5, positive_rate: 75, minimum_samples: 3 },
      by_content_kind: [
        { content_kind: 'news', label: '新闻资讯', up: 2, down: 1 },
        { content_kind: 'community_signal', label: '社区线索', up: 1, down: 0 },
      ],
      items: [{
        message_row_id: 7,
        vote: 'up',
        voted_at: '2026-08-12T00:00:00Z',
        event_count: 2,
        title: '平台发布重要产品更新',
        source_name: '官方产品公告',
        content_kind: 'news',
        content_kind_label: '新闻资讯',
        ai_score: 82,
      }],
    },
  }))
  assert.match(html, /<h2>反馈记录<\/h2>/)
  assert.match(html, /有效反馈/)
  assert.match(html, /75% 好评/)
  assert.match(html, /学习门槛/)
  assert.match(html, /平台发布重要产品更新/)
  assert.match(html, /修改 1 次/)
  assert.doesNotMatch(html, /public_id|source_key|interest_tags/)
})

test('feedback empty and loading states remain explicit', () => {
  const empty = renderToStaticMarkup(React.createElement(FeedbackView, { data: null, loading: false }))
  assert.match(empty, /还没有反馈记录/)
  const loading = renderToStaticMarkup(React.createElement(FeedbackView, { data: null, loading: true }))
  assert.match(loading, /正在加载反馈/)
})

test('sidebar exposes the dedicated feedback destination', () => {
  const html = renderToStaticMarkup(React.createElement(Sidebar, {
    active: 'feedback',
    onNavigate() {},
    online: true,
    username: 'admin',
  }))
  assert.match(html, /aria-label="反馈记录"/)
  assert.match(html, /aria-current="page"/)
})
