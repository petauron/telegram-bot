export function SectionTabs({ items, active, onChange, label, className = '' }) {
  return (
    <div className={`section-tabs ${className}`.trim()} role="tablist" aria-label={label}>
      {items.map((item) => (
        <button
          type="button"
          role="tab"
          aria-selected={active === item.id}
          className={active === item.id ? 'active' : ''}
          key={item.id}
          onClick={() => onChange(item.id)}
        >
          {item.label}
          {item.meta ? <span>{item.meta}</span> : null}
        </button>
      ))}
    </div>
  )
}
