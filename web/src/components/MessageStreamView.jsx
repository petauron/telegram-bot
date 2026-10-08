import { Filters } from './Filters'
import { Inspector } from './Inspector'
import { MessageTable } from './MessageTable'
import { PageHeading } from './PageHeading'

export function chatsWithMessages(chats) {
  return chats.filter((chat) => Number(chat.message_count) > 0)
}

export function MessageStreamView({
  filters,
  chats,
  onFiltersChange,
  rows,
  total,
  selected,
  onSelect,
  page,
  pageSize,
  onPage,
  loading,
  onReanalyze,
  analyzing,
  onCloseDetail,
}) {
  const recordedChats = chatsWithMessages(chats)

  return (
    <main className="page message-stream-view">
      <PageHeading title="消息流" description="默认显示 AI 评分 60 分以上的消息，可在更多筛选中调整。" />
      <Filters filters={filters} chats={recordedChats} total={total} onChange={onFiltersChange} />
      <MessageTable
        rows={rows}
        total={total}
        selectedId={selected?.id}
        onSelect={onSelect}
        page={page}
        pageSize={pageSize}
        onPage={onPage}
        loading={loading}
      />
      {selected ? (
        <>
          <button className="drawer-backdrop" type="button" aria-label="关闭消息详情" onClick={onCloseDetail} />
          <Inspector
            key={selected.id}
            row={selected}
            onReanalyze={onReanalyze}
            analyzing={analyzing}
            onClose={onCloseDetail}
          />
        </>
      ) : null}
    </main>
  )
}
