import { useMemo, useState } from 'react'
import { Save, Search } from './Icons'

const KINDS = [
  { id: 'all', label: '全部' },
  { id: 'group', label: '群组' },
  { id: 'channel', label: '频道' },
  { id: 'selected', label: '已选择' },
]

export function ChatPicker({ chats, selectedIds, onChange, onSave, saving, dirty }) {
  const [query, setQuery] = useState('')
  const [kind, setKind] = useState('all')
  const selected = useMemo(() => new Set(selectedIds), [selectedIds])
  const filtered = useMemo(() => {
    const term = query.trim().toLocaleLowerCase('zh-CN')
    return chats.filter((chat) => {
      if (kind === 'selected' && !selected.has(chat.chat_id)) return false
      if (kind !== 'all' && kind !== 'selected' && chat.chat_type !== kind) return false
      if (!term) return true
      return `${chat.chat_name} ${chat.username || ''} ${chat.chat_id}`
        .toLocaleLowerCase('zh-CN')
        .includes(term)
    })
  }, [chats, kind, query, selected])

  const toggle = (chatId) => {
    if (selected.has(chatId)) {
      onChange(selectedIds.filter((id) => id !== chatId))
      return
    }
    onChange([...selectedIds, chatId])
  }

  const selectFiltered = () => {
    const next = new Set(selectedIds)
    filtered.forEach((chat) => next.add(chat.chat_id))
    onChange([...next])
  }

  return (
    <section className="chat-picker" aria-labelledby="chat-picker-title">
      <div className="chat-picker-heading">
        <div>
          <h3 id="chat-picker-title">监听群组与频道</h3>
          <p>仅处理选中会话之后产生的新消息</p>
        </div>
        <div className="chat-picker-commit">
          <strong>{selected.size} / {chats.length}</strong>
          <span className={dirty ? 'dirty' : 'saved'}>{dirty ? '尚未保存' : '已保存'}</span>
          <button className="button primary" type="button" onClick={onSave} disabled={!dirty || saving}>
            <Save size={16} />{saving ? '保存中…' : '保存监听范围'}
          </button>
        </div>
      </div>

      <div className="chat-picker-tools">
        <label className="chat-search">
          <Search size={16} aria-hidden="true" />
          <input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="搜索名称、用户名或 chat_id"
            aria-label="搜索可监听会话"
          />
        </label>
        <div className="chat-kind-tabs" aria-label="会话类型筛选">
          {KINDS.map((item) => (
            <button
              className={kind === item.id ? 'active' : ''}
              key={item.id}
              onClick={() => setKind(item.id)}
              type="button"
            >
              {item.label}
            </button>
          ))}
        </div>
      </div>

      <div className="chat-picker-actions">
        <span>当前显示 {filtered.length} 个会话</span>
        <div>
          <button type="button" onClick={selectFiltered} disabled={!filtered.length}>选择当前结果</button>
          <button type="button" onClick={() => onChange([])} disabled={!selected.size}>清空选择</button>
        </div>
      </div>

      <div className="chat-list" role="group" aria-label="可监听群组和频道">
        {filtered.length ? filtered.map((chat) => (
          <label className={`chat-option ${selected.has(chat.chat_id) ? 'selected' : ''}`} key={chat.chat_id}>
            <input
              type="checkbox"
              checked={selected.has(chat.chat_id)}
              onChange={() => toggle(chat.chat_id)}
            />
            <span className={`chat-type ${chat.chat_type}`}>{chat.chat_type === 'group' ? '群组' : '频道'}</span>
            <span className="chat-option-main">
              <strong>{chat.chat_name}</strong>
              <small>{chat.username ? `@${chat.username} · ` : ''}{chat.chat_id}</small>
            </span>
            <span className="chat-message-count">近 7 天 {chat.message_count} 条</span>
          </label>
        )) : (
          <div className="chat-list-empty">
            {chats.length ? '没有符合条件的会话' : '会话列表尚未同步，请确认监听服务正在运行'}
          </div>
        )}
      </div>
      <p className="chat-picker-note">列表来自个人账号本来有权查看的会话；取消选择只会停止后续处理，不会退出群或修改历史记录。</p>
    </section>
  )
}
