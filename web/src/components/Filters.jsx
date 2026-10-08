import { useState } from 'react'
import { ChevronDown, Filter, Search, X } from './Icons'

export const DEFAULT_MIN_SCORE = '60'

function SelectWrap({ children, className = '' }) {
  return <span className={`select-wrap ${className}`.trim()}>{children}<ChevronDown size={14} aria-hidden="true" /></span>
}

export function activeAdvancedFilterCount(filters) {
  return Number(filters.minScore !== DEFAULT_MIN_SCORE)
    + Number(filters.pushStatus !== 'all')
    + Number(filters.prefilterStatus !== 'exclude')
    + Number((filters.similarStatus || 'exclude') !== 'exclude')
}

export function Filters({ filters, chats, total, onChange }) {
  const [advancedOpen, setAdvancedOpen] = useState(false)
  const activeCount = activeAdvancedFilterCount(filters)
  const update = (key) => (event) => {
    const value = event.target.value
    onChange((current) => ({ ...current, [key]: value }))
  }
  const resetAdvanced = () => onChange((current) => ({
    ...current,
    minScore: DEFAULT_MIN_SCORE,
    pushStatus: 'all',
    prefilterStatus: 'exclude',
    similarStatus: 'exclude',
  }))

  return (
    <section className="filter-area" aria-label="消息筛选">
      <div className="filter-toolbar">
        <label className="search-field">
          <Search size={17} aria-hidden="true" />
          <input
            value={filters.q}
            onChange={update('q')}
            placeholder="搜索消息、来源或发送者"
            aria-label="搜索消息、来源或发送者"
          />
        </label>
        <SelectWrap className="chat-filter">
          <select value={filters.chatId} onChange={update('chatId')} aria-label="来源筛选">
            <option value="">全部来源</option>
            {chats.map((chat) => <option value={chat.chat_id} key={chat.chat_id}>{chat.chat_name}</option>)}
          </select>
        </SelectWrap>
        <SelectWrap className="time-filter">
          <select value={filters.hours} onChange={update('hours')} aria-label="时间范围">
            <option value="1">最近 1 小时</option>
            <option value="6">最近 6 小时</option>
            <option value="24">最近 24 小时</option>
            <option value="72">最近 3 天</option>
          </select>
        </SelectWrap>
        <button
          className={`advanced-filter-trigger ${advancedOpen ? 'active' : ''}`}
          type="button"
          aria-expanded={advancedOpen}
          aria-controls="advanced-message-filters"
          onClick={() => setAdvancedOpen((current) => !current)}
        >
          <Filter size={16} />
          更多筛选{activeCount ? <b>{activeCount}</b> : null}
        </button>
        <span className="result-count">共 {total} 条</span>
      </div>
      {advancedOpen ? (
        <div className="advanced-filters" id="advanced-message-filters">
          <label>
            <span>最低评分</span>
            <SelectWrap>
              <select value={filters.minScore} onChange={update('minScore')} aria-label="最低评分">
                <option value="0">不限</option>
                <option value="20">20 分以上</option>
                <option value="50">50 分以上</option>
                <option value="60">60 分以上（默认）</option>
                <option value="80">80 分以上</option>
                <option value="100">100 分</option>
              </select>
            </SelectWrap>
          </label>
          <label>
            <span>推送状态</span>
            <SelectWrap>
              <select value={filters.pushStatus} onChange={update('pushStatus')} aria-label="推送状态">
                <option value="all">全部状态</option>
                <option value="immediate">即时已推送</option>
                <option value="digest">历史摘要已推送</option>
                <option value="not_pushed">未推送</option>
              </select>
            </SelectWrap>
          </label>
          <label>
            <span>前置过滤</span>
            <SelectWrap>
              <select value={filters.prefilterStatus} onChange={update('prefilterStatus')} aria-label="前置过滤状态">
                <option value="exclude">隐藏已过滤</option>
                <option value="all">全部前置状态</option>
                <option value="filtered">仅看已过滤</option>
              </select>
            </SelectWrap>
          </label>
          <label>
            <span>相似资讯</span>
            <SelectWrap>
              <select value={filters.similarStatus || 'exclude'} onChange={update('similarStatus')} aria-label="相似资讯状态">
                <option value="exclude">隐藏重复（默认）</option>
                <option value="all">显示全部</option>
                <option value="suppressed">仅看已抑制重复</option>
              </select>
            </SelectWrap>
          </label>
          <button className="clear-filters" type="button" onClick={resetAdvanced} disabled={!activeCount}>
            <X size={14} />清除高级筛选
          </button>
        </div>
      ) : null}
    </section>
  )
}
