import assert from 'node:assert/strict'
import test, { after } from 'node:test'
import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { createServer } from 'vite'

const vite = await createServer({
  appType: 'custom', logLevel: 'silent', server: { middlewareMode: true },
})
const { ChatPicker, chatActivityState } = await vite.ssrLoadModule('/src/components/ChatPicker.jsx')
after(async () => { await vite.close() })

const chat = {
  chat_id: -1001, chat_name: '示例公告', chat_type: 'channel',
  watched: true, message_count: 3,
  activity: {
    recorded_count: 10, successful_push_count: 0, media_unread_count: 7,
    retained_filtered_count: 2, retained_failed_push_count: 0,
    retained_analysis_error_count: 0, includes_partial_history: true,
    watch_start_known: false, watch_started_at: null,
    last_received_at: '2099-01-01T00:00:00Z', last_successful_push_at: null,
  },
}

test('source state distinguishes no traffic, unread images, filtering and delivery errors', () => {
  assert.equal(chatActivityState(chat), '消息已被过滤')
  assert.equal(chatActivityState({ ...chat, watched: false }), '未监听')
  assert.equal(chatActivityState({ ...chat, activity: { recorded_count: 0 } }), '尚未记录消息')
  assert.equal(chatActivityState({ ...chat, activity: { recorded_count: 2, media_unread_count: 2 } }), '图片未识别')
  assert.equal(chatActivityState({ ...chat, activity: { ...chat.activity, retained_failed_push_count: 1 } }), '存在推送失败')
  assert.equal(chatActivityState({ ...chat, activity: { ...chat.activity, retained_analysis_error_count: 1 } }), '存在分析失败')
  assert.equal(chatActivityState({ ...chat, activity: { ...chat.activity, successful_push_count: 1 } }), '已有成功推送')
  assert.equal(chatActivityState({ ...chat, activity: { ...chat.activity, retained_disabled_count: 1 } }), '模型未启用')
  assert.equal(chatActivityState({ ...chat, activity: { ...chat.activity, retained_pending_count: 1 } }), '消息待分析')
})

test('watch picker does not claim partial history is all time or missing messages are failures', () => {
  const html = renderToStaticMarkup(React.createElement(ChatPicker, {
    chats: [chat], selectedIds: [chat.chat_id], dirty: false, saving: false,
    onChange() {}, onSave() {},
  }))
  assert.match(html, /原监听开始时间未知/)
  assert.match(html, /不代表完整历史/)
  assert.match(html, /暂无消息不等于采集故障/)
  assert.match(html, /图片未识别/)
  assert.match(html, /最近成功 暂无/)
  assert.match(html, /成功推送按消息去重/)
  assert.match(html, /aria-pressed="true"/)
  assert.doesNotMatch(html, /近 7 天/)
})
