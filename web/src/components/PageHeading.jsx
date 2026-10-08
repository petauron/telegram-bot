export function PageHeading({ title, description, action = null, className = '' }) {
  return (
    <header className={`page-heading ${className}`.trim()}>
      <div>
        <h2>{title}</h2>
        {description ? <p>{description}</p> : null}
      </div>
      {action ? <div className="page-heading-action">{action}</div> : null}
    </header>
  )
}
