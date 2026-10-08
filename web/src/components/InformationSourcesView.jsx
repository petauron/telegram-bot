import { useMemo, useRef, useState } from 'react'
import { CheckCircle2, Clock3, ExternalLink, RefreshCw, Rss, Save, X } from './Icons'
import { PageHeading } from './PageHeading'

const EMPTY_FORM = {
  kind: 'rss',
  name: '',
  url: '',
  enabled: true,
  pollIntervalMinutes: '15',
  includePrereleases: false,
  ecosystem: '',
  minimumSeverity: 'high',
  keywords: '',
  storyList: 'best',
  minimumScore: '150',
  blueskyHandle: '',
  mastodonInstanceUrl: '',
  mastodonTimelineType: 'account',
  mastodonTarget: '',
  sourceSecret: '',
  sourceSecretConfigured: false,
  clearSourceSecret: false,
  imapHost: '',
  imapPort: '993',
  imapUsername: '',
  imapMailbox: 'INBOX',
  senderAllowlist: '',
}

const EMPTY_PROVIDER_CONFIG = { github_token_configured: false, nvd_api_key_configured: false }

const STATE_LABELS = {
  scheduled: '定时采集中',
  waiting: '等待首次采集',
  processing: '正在抓取',
  rateLimited: '频率受限',
  stalled: '采集停滞',
  error: '采集异常',
  disabled: '已停用',
}

const SOURCE_STALE_GRACE_MS = 5 * 60 * 1000

const ERROR_LABELS = {
  dns_error: '域名解析失败',
  unsafe_address: '地址未通过安全校验',
  redirect_blocked: '来源发生重定向',
  rate_limited: '来源限制访问频率',
  http_4xx: '来源拒绝请求',
  http_5xx: '来源服务异常',
  timeout: '抓取超时',
  network_error: '网络连接失败',
  response_too_large: '响应超过大小限制',
  invalid_feed: '不是有效的 RSS / Atom',
  invalid_response: '来源响应格式无效',
  credentials_missing: '来源凭据缺失',
  authentication_error: '来源认证失败',
  tls_error: 'TLS 证书或握手失败',
  mailbox_error: '邮箱目录读取失败',
  interrupted: '上次采集被服务重启中断',
  internal_error: '采集处理异常',
}

const SOURCE_KIND_LABELS = {
  rss: 'RSS / Atom',
  github_releases: 'GitHub Releases',
  github_advisories: 'GitHub Security Advisories',
  cisa_kev: 'CISA KEV',
  nvd_cve: 'NVD CVE',
  vendor_status: '厂商状态页',
  hacker_news: 'Hacker News',
  bluesky: 'Bluesky 可信账号',
  mastodon: 'Mastodon 可信来源',
  newsletter_imap: '邮件 Newsletter',
}

function formatDateTime(value) {
  return value ? new Date(value).toLocaleString('zh-CN') : '—'
}

function sourceDetail(source) {
  if (source.kind === 'github_releases') return source.settings?.include_prereleases ? '正式版 + 预发布' : '仅正式版'
  if (source.kind === 'github_advisories') return `${source.settings?.minimum_severity === 'critical' ? '仅 Critical' : 'High + Critical'}${source.settings?.ecosystem ? ` · ${source.settings.ecosystem}` : ''}${source.settings?.keywords?.length ? ` · ${source.settings.keywords.join(' / ')}` : ''}`
  if (source.kind === 'cisa_kev') return 'CISA 已确认在野利用 · 新增与实质更新'
  if (source.kind === 'nvd_cve') return `High/Critical + KEV${source.settings?.keywords?.length ? ` · ${source.settings.keywords.join(' / ')}` : ''}`
  if (source.kind === 'vendor_status') return '故障 · 性能下降 · 恢复 · 事后报告'
  if (source.kind === 'hacker_news') return `${source.settings?.story_list === 'both' ? 'Top + Best' : source.settings?.story_list === 'top' ? 'Top' : 'Best'} · ≥ ${source.settings?.minimum_score || 150} 分${source.settings?.keywords?.length ? ` · ${source.settings.keywords.join(' / ')}` : ' · 内置技术主题'} · 不读取评论`
  if (source.kind === 'bluesky') return `@${source.settings?.handle} · Jetstream 单账号订阅`
  if (source.kind === 'mastodon') return `${source.settings?.timeline_type === 'tag' ? `#${source.settings?.target}` : `@${source.settings?.target}`} · ${source.secret_configured ? '已配置来源 Token' : '公开访问'}`
  if (source.kind === 'newsletter_imap') return `只读 IMAPS · ${source.secret_configured ? '凭据已配置' : '凭据缺失'}${source.settings?.sender_allowlist?.length ? ` · ${source.settings.sender_allowlist.length} 个发件人规则` : ''}`
  return ''
}

export function sourceRuntimeState(source, now = Date.now()) {
  if (!source.enabled) return 'disabled'
  if (source.poll_state === 'processing') return 'processing'
  if (source.last_error_category === 'rate_limited') return 'rateLimited'
  if (source.poll_state === 'error' || Number(source.consecutive_failures || 0) > 0) return 'error'
  if (!source.initialized || !source.last_success_at) return 'waiting'
  const nextPollAt = Date.parse(source.next_poll_at || '')
  if (!Number.isFinite(nextPollAt) || nextPollAt + SOURCE_STALE_GRACE_MS < now) return 'stalled'
  return 'scheduled'
}

export function sourceStateLabel(source, now = Date.now()) {
  return STATE_LABELS[sourceRuntimeState(source, now)]
}

export function summarizeSourceStates(sources, now = Date.now()) {
  return sources.reduce((summary, source) => {
    summary[sourceRuntimeState(source, now)] += 1
    return summary
  }, {
    scheduled: 0,
    processing: 0,
    waiting: 0,
    rateLimited: 0,
    stalled: 0,
    error: 0,
    disabled: 0,
  })
}

export function InformationSourcesView({
  sources, loading, saving, providerConfig = EMPTY_PROVIDER_CONFIG, providerSaving = false,
  onSave, onRefresh, onSaveProviderConfig = async () => false,
}) {
  const [form, setForm] = useState(EMPTY_FORM)
  const [githubToken, setGithubToken] = useState('')
  const [clearGithubToken, setClearGithubToken] = useState(false)
  const [nvdApiKey, setNvdApiKey] = useState('')
  const [clearNvdApiKey, setClearNvdApiKey] = useState(false)
  const [editingId, setEditingId] = useState(null)
  const [refreshingId, setRefreshingId] = useState(null)
  const sourceFormRef = useRef(null)
  const enabledCount = useMemo(
    () => sources.filter((source) => source.enabled).length,
    [sources],
  )
  const stateSummary = useMemo(() => summarizeSourceStates(sources), [sources])
  const githubRateLimitedCount = useMemo(
    () => sources.filter((source) => (
      source.enabled
      && ['github_releases', 'github_advisories'].includes(source.kind)
      && source.last_error_category === 'rate_limited'
    )).length,
    [sources],
  )

  const change = (key) => (event) => {
    const value = event.target.type === 'checkbox' ? event.target.checked : event.target.value
    setForm((current) => ({ ...current, [key]: value }))
  }

  const reset = () => {
    setEditingId(null)
    setForm(EMPTY_FORM)
  }

  const edit = (source) => {
    setEditingId(source.id)
    setForm({
      kind: source.kind,
      name: source.name,
      url: source.kind === 'github_releases' ? (source.settings?.repository || source.url) : source.url,
      enabled: Boolean(source.enabled),
      pollIntervalMinutes: String(source.poll_interval_minutes),
      includePrereleases: Boolean(source.settings?.include_prereleases),
      ecosystem: source.settings?.ecosystem || '',
      minimumSeverity: source.settings?.minimum_severity || 'high',
      keywords: (source.settings?.keywords || []).join(', '),
      storyList: source.settings?.story_list || 'best',
      minimumScore: String(source.settings?.minimum_score || 150),
      blueskyHandle: source.settings?.handle || '',
      mastodonInstanceUrl: source.settings?.instance_url || '',
      mastodonTimelineType: source.settings?.timeline_type || 'account',
      mastodonTarget: source.settings?.target || '',
      sourceSecret: '',
      sourceSecretConfigured: Boolean(source.secret_configured),
      clearSourceSecret: false,
      imapHost: source.settings?.host || '',
      imapPort: String(source.settings?.port || 993),
      imapUsername: source.settings?.username || '',
      imapMailbox: source.settings?.mailbox || 'INBOX',
      senderAllowlist: (source.settings?.sender_allowlist || []).join(', '),
    })
    sourceFormRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }

  const submit = async (event) => {
    event.preventDefault()
    const saved = await onSave(form, editingId)
    if (saved) reset()
  }

  const refresh = async (sourceId) => {
    setRefreshingId(sourceId)
    await onRefresh(sourceId)
    setRefreshingId(null)
  }

  const saveProvider = async (event) => {
    event.preventDefault()
    const saved = await onSaveProviderConfig({ githubToken, clearGithubToken, nvdApiKey, clearNvdApiKey })
    if (saved) {
      setGithubToken('')
      setClearGithubToken(false)
      setNvdApiKey('')
      setClearNvdApiKey(false)
    }
  }

  return (
    <main className="page sources-view">
      <PageHeading
        title="信息源"
        description="管理官方 Feed、GitHub 与高信号安全目录。首次抓取只建立基线，不会推送历史内容。"
        action={<span className="source-count"><Rss size={15} />{enabledCount} 个启用</span>}
      />

      <section className="source-list surface-card" aria-label="已配置的信息源">
        <div className="source-list-heading">
          <div><h3>已配置的信息源</h3><p>集中查看采集状态、游标与最近运行时间，共 {sources.length} 个来源。</p></div>
          <div className="source-health-summary" aria-label="信息源运行概况">
            <span className="healthy">{stateSummary.scheduled} 定时运行</span>
            {stateSummary.processing ? <span className="processing">{stateSummary.processing} 正在抓取</span> : null}
            {stateSummary.waiting ? <span>{stateSummary.waiting} 待首次成功</span> : null}
            {stateSummary.rateLimited ? <span className="limited">{stateSummary.rateLimited} 频率受限</span> : null}
            {stateSummary.stalled ? <span className="error">{stateSummary.stalled} 疑似停滞</span> : null}
            {stateSummary.error ? <span className="error">{stateSummary.error} 异常</span> : null}
            {stateSummary.disabled ? <span>{stateSummary.disabled} 停用</span> : null}
          </div>
        </div>
        {githubRateLimitedCount > 0 && !providerConfig.github_token_configured ? (
          <p className="source-alert" role="status">
            {githubRateLimitedCount} 个 GitHub 来源触发匿名额度限制；可在下方配置免费 Personal access token 提高额度。
          </p>
        ) : null}
        {loading && sources.length === 0 ? <div className="empty-panel">正在读取信息源…</div> : null}
        {!loading && sources.length === 0 ? (
          <div className="empty-panel"><Rss size={28} /><strong>尚未添加信息源</strong><span>从官方 RSS / Atom 或 GitHub Releases 开始。</span></div>
        ) : null}
        {sources.length > 0 ? (
          <div className="source-table-wrap">
            <table className="source-table">
              <thead>
                <tr><th>来源</th><th>运行状态</th><th>已入管线</th><th>当前条目</th><th>最近成功</th><th>下次采集</th><th><span className="table-action-label">操作</span></th></tr>
              </thead>
              <tbody>
                {sources.map((source) => {
                  const runtimeState = sourceRuntimeState(source)
                  const failed = ['rateLimited', 'stalled', 'error'].includes(runtimeState)
                  const hasFetchError = ['rateLimited', 'error'].includes(runtimeState)
                  const status = STATE_LABELS[runtimeState]
                  const detail = sourceDetail(source)
                  const publicUrl = source.url.startsWith('http://') || source.url.startsWith('https://')
                  return (
                    <tr className={`source-table-row ${failed ? 'has-error' : ''}`} key={source.id}>
                      <td className="source-name-cell">
                        <div className="source-identity">
                          <span className="source-icon"><Rss size={17} /></span>
                          <div>
                            <strong>{source.name}</strong>
                            <span>{SOURCE_KIND_LABELS[source.kind] || source.kind}</span>
                          </div>
                        </div>
                        {publicUrl ? <a className="source-location" href={source.url} target="_blank" rel="noreferrer">{source.url}<ExternalLink size={12} /></a> : <span className="source-location">{source.settings?.host || source.url} · {source.settings?.mailbox || ''}</span>}
                        {detail ? <small className="source-detail">{detail}</small> : null}
                      </td>
                      <td className="source-status-cell" data-label="运行状态">
                        <span className={`source-state ${runtimeState}`}>{status}</span>
                        <small className="source-runtime-note">
                          {runtimeState === 'scheduled' ? '采集器已生效，等待下次定时任务' : null}
                          {runtimeState === 'processing' ? '正在请求并解析来源内容' : null}
                          {runtimeState === 'waiting' ? '尚未成功完成首次抓取' : null}
                          {runtimeState === 'rateLimited' ? '已自动退避，将按下次时间重试' : null}
                          {runtimeState === 'stalled' ? '已超过计划时间 5 分钟仍未运行' : null}
                          {runtimeState === 'error' ? '采集器仍在运行，将自动重试' : null}
                          {runtimeState === 'disabled' ? '采集器不会发起请求' : null}
                        </small>
                        <small className={source.initialized ? 'source-baseline success' : 'source-baseline'}>{source.initialized ? <CheckCircle2 size={12} /> : <Clock3 size={12} />}{source.initialized ? '已建立采集基线' : '首次成功抓取后建立基线'}</small>
                        {hasFetchError ? <small className="source-error">{ERROR_LABELS[source.last_error_category] || '采集失败'} · 连续 {source.consecutive_failures} 次</small> : null}
                      </td>
                      <td className="source-number-cell" data-label="已入管线">{source.message_count || 0}</td>
                      <td className="source-number-cell" data-label="当前条目">{source.item_count || 0}</td>
                      <td className="source-time-cell" data-label="最近成功">{formatDateTime(source.last_success_at)}</td>
                      <td className="source-time-cell" data-label="下次采集">{source.enabled ? formatDateTime(source.next_poll_at) : '已停用'}</td>
                      <td className="source-actions-cell">
                        <div className="source-row-actions">
                          <button className="secondary-button" type="button" onClick={() => edit(source)}>编辑</button>
                          <button className="secondary-button" type="button" disabled={!source.enabled || refreshingId === source.id} onClick={() => refresh(source.id)}>
                            <RefreshCw size={13} className={refreshingId === source.id ? 'spin' : ''} /><span>{refreshingId === source.id ? '安排中…' : '立即抓取'}</span>
                          </button>
                        </div>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          </div>
        ) : null}
      </section>

      <div className="sources-layout">
        <form className="source-form surface-card" onSubmit={submit} ref={sourceFormRef}>
          <div className="card-heading">
            <div><span className="eyebrow">INFORMATION SOURCE</span><h3>{editingId ? '编辑信息源' : '添加信息源'}</h3></div>
            {editingId ? <button className="icon-button" type="button" onClick={reset} aria-label="取消编辑"><X size={18} /></button> : null}
          </div>
          <label className="field-stack">
            <span>来源类型</span>
            <select value={form.kind} onChange={change('kind')} disabled={Boolean(editingId)}>
              <option value="rss">RSS / Atom</option>
              <option value="github_releases">GitHub Releases</option>
              <option value="github_advisories">GitHub Security Advisories</option>
              <option value="cisa_kev">CISA KEV</option>
              <option value="nvd_cve">NVD CVE</option>
              <option value="vendor_status">厂商状态页</option>
              <option value="hacker_news">Hacker News</option>
              <option value="bluesky">Bluesky 可信账号</option>
              <option value="mastodon">Mastodon 可信来源</option>
              <option value="newsletter_imap">邮件 Newsletter</option>
            </select>
            {editingId ? <small>类型不可直接切换；如需更换类型请新增来源。</small> : null}
          </label>
          <label className="field-stack">
            <span>显示名称</span>
            <input value={form.name} onChange={change('name')} maxLength={120} required placeholder="例如：厂商安全公告" />
          </label>
          {['rss', 'github_releases', 'vendor_status'].includes(form.kind) ? <label className="field-stack">
            <span>{form.kind === 'rss' ? 'Feed URL' : form.kind === 'vendor_status' ? '状态页首页' : 'GitHub 仓库'}</span>
            <input value={form.url} onChange={change('url')} maxLength={2048} required type="text" inputMode={['rss', 'vendor_status'].includes(form.kind) ? 'url' : 'text'} placeholder={form.kind === 'rss' ? 'https://example.com/feed.xml' : form.kind === 'vendor_status' ? 'https://status.example.com' : 'owner/repository'} />
            <small>{form.kind === 'rss' ? '仅允许公开 HTTPS 地址；不跟随重定向，也不访问内网地址。' : form.kind === 'vendor_status' ? '填写公开 Statuspage 首页；读取故障、恢复与事后更新，不采集计划维护历史。' : '使用官方 Releases API；可填 owner/repo 或公开 GitHub 仓库地址。'}</small>
          </label> : null}
          {form.kind === 'github_releases' ? (
            <label className="switch-line">
              <input type="checkbox" checked={form.includePrereleases} onChange={change('includePrereleases')} />
              <span><strong>包含重要预发布</strong><small>默认只采集正式 Release；开启后 prerelease 也进入现有模型筛选。</small></span>
            </label>
          ) : null}
          {form.kind === 'github_advisories' ? (
            <div className="source-settings-group">
              <label className="field-stack">
                <span>最低严重度</span>
                <select value={form.minimumSeverity} onChange={change('minimumSeverity')}>
                  <option value="high">High + Critical</option>
                  <option value="critical">仅 Critical</option>
                </select>
              </label>
              <label className="field-stack">
                <span>Ecosystem</span>
                <select value={form.ecosystem} onChange={change('ecosystem')}>
                  <option value="">全部</option>
                  <option value="npm">npm</option><option value="pip">pip</option>
                  <option value="go">Go</option><option value="maven">Maven</option>
                  <option value="rust">Rust</option><option value="rubygems">RubyGems</option>
                  <option value="composer">Composer</option><option value="nuget">NuGet</option>
                  <option value="actions">GitHub Actions</option><option value="swift">Swift</option>
                </select>
              </label>
              <label className="field-stack">
                <span>关注产品关键词</span>
                <input value={form.keywords} onChange={change('keywords')} maxLength={800} placeholder="例如：nginx, openssl, react" />
                <small>可留空；填写后至少命中一个产品或正文关键词才进入管线。</small>
              </label>
            </div>
          ) : null}
          {form.kind === 'cisa_kev' ? (
            <p className="source-note">固定使用 CISA 官方 Known Exploited Vulnerabilities JSON；只处理新增或内容发生实质变化的在野利用漏洞。</p>
          ) : null}
          {form.kind === 'nvd_cve' ? (
            <div className="source-settings-group">
              <p className="source-note">首次只读取最近 24 小时；以后按更新时间增量采集。High/Critical、CISA KEV 或命中下列产品关键词时才进入管线。</p>
              <label className="field-stack">
                <span>关注产品关键词</span>
                <input value={form.keywords} onChange={change('keywords')} maxLength={1000} placeholder="例如：nginx, openssl, kubernetes" />
              </label>
            </div>
          ) : null}
          {form.kind === 'vendor_status' ? (
            <p className="source-note">使用公开 Statuspage JSON API；同一事件的恢复、影响变化和事后报告作为实质更新进入管线。</p>
          ) : null}
          {form.kind === 'hacker_news' ? (
            <div className="source-settings-group">
              <p className="source-note">读取官方 Top/Best story，不读取评论；本地热度与主题筛选通过后才进入模型管线。</p>
              <label className="field-stack">
                <span>榜单</span>
                <select value={form.storyList} onChange={change('storyList')}>
                  <option value="best">Best</option><option value="top">Top</option><option value="both">Top + Best</option>
                </select>
              </label>
              <label className="field-stack">
                <span>最低热度</span>
                <input type="number" min="20" max="5000" value={form.minimumScore} onChange={change('minimumScore')} />
              </label>
              <label className="field-stack">
                <span>主题关键词</span>
                <input value={form.keywords} onChange={change('keywords')} maxLength={1000} placeholder="例如：AI, open source, security" />
                <small>留空使用内置 AI、开发、云服务、安全和开源主题词。</small>
              </label>
            </div>
          ) : null}
          {form.kind === 'bluesky' ? (
            <div className="source-settings-group">
              <p className="source-note">使用官方 Jetstream，仅订阅一个可信账号的新公开帖子；不接入全网 Firehose。</p>
              <label className="field-stack">
                <span>Bluesky handle</span>
                <input value={form.blueskyHandle} onChange={change('blueskyHandle')} maxLength={253} required placeholder="example.bsky.social" />
              </label>
            </div>
          ) : null}
          {form.kind === 'mastodon' ? (
            <div className="source-settings-group">
              <p className="source-note">监听指定实例上的可信账号或精确标签；不读取全联邦时间线。</p>
              <label className="field-stack">
                <span>实例地址</span>
                <input value={form.mastodonInstanceUrl} onChange={change('mastodonInstanceUrl')} maxLength={2048} required inputMode="url" placeholder="https://mastodon.social" />
              </label>
              <label className="field-stack">
                <span>来源范围</span>
                <select value={form.mastodonTimelineType} onChange={change('mastodonTimelineType')}>
                  <option value="account">可信账号</option><option value="tag">精确标签</option>
                </select>
              </label>
              <label className="field-stack">
                <span>{form.mastodonTimelineType === 'tag' ? '标签' : '账号'}</span>
                <input value={form.mastodonTarget} onChange={change('mastodonTarget')} maxLength={128} required placeholder={form.mastodonTimelineType === 'tag' ? 'opensource' : 'trusted_account'} />
              </label>
              <label className="field-stack">
                <span>实例 Access Token（可选）</span>
                <input type="password" autoComplete="new-password" value={form.sourceSecret} onChange={change('sourceSecret')} maxLength={2048} placeholder={form.sourceSecretConfigured ? '已配置，留空保持不变' : '公开时间线可留空'} />
                <small>仅保存于该来源；GET API 永不回传明文。切换实例会清除旧 Token。</small>
              </label>
              {form.sourceSecretConfigured ? <label className="switch-line"><input type="checkbox" checked={form.clearSourceSecret} onChange={change('clearSourceSecret')} /><span><strong>清除已保存 Token</strong><small>保存后改用实例公开额度。</small></span></label> : null}
            </div>
          ) : null}
          {form.kind === 'newsletter_imap' ? (
            <div className="source-settings-group">
              <p className="source-note">只读 IMAPS 993，适合用户主动订阅的厂商、安全与技术简报；不保存附件。</p>
              <label className="field-stack"><span>IMAP 主机</span><input value={form.imapHost} onChange={change('imapHost')} maxLength={253} required placeholder="imap.example.com" /></label>
              <label className="field-stack"><span>IMAP 端口</span><input type="number" value={form.imapPort} readOnly aria-readonly="true" /></label>
              <label className="field-stack"><span>邮箱账号</span><input value={form.imapUsername} onChange={change('imapUsername')} maxLength={254} required autoComplete="username" /></label>
              <label className="field-stack"><span>邮箱目录</span><input value={form.imapMailbox} onChange={change('imapMailbox')} maxLength={128} required /></label>
              <label className="field-stack"><span>发件人白名单</span><input value={form.senderAllowlist} onChange={change('senderAllowlist')} maxLength={1000} placeholder="news@example.com, vendor.com" /><small>可留空；填写邮箱或域名，逗号分隔。</small></label>
              <label className="field-stack">
                <span>邮箱密码 / 应用专用密码</span>
                <input type="password" autoComplete="new-password" value={form.sourceSecret} onChange={change('sourceSecret')} maxLength={2048} required={!form.sourceSecretConfigured} placeholder={form.sourceSecretConfigured ? '已配置，留空保持不变' : '必须填写'} />
                <small>仅保存于该来源，GET API 永不回传；建议使用只读专用邮箱与应用密码。</small>
              </label>
            </div>
          ) : null}
          <label className="field-stack">
            <span>采集间隔</span>
            <select value={form.pollIntervalMinutes} onChange={change('pollIntervalMinutes')}>
              <option value="5">每 5 分钟</option>
              <option value="15">每 15 分钟</option>
              <option value="30">每 30 分钟</option>
              <option value="60">每 1 小时</option>
              <option value="180">每 3 小时</option>
              <option value="720">每 12 小时</option>
            </select>
          </label>
          <label className="switch-line">
            <input type="checkbox" checked={form.enabled} onChange={change('enabled')} />
            <span><strong>启用采集</strong><small>停用后保留历史和游标</small></span>
          </label>
          <button className="primary-button" type="submit" disabled={saving}>
            <Save size={16} />{saving ? '保存中…' : editingId ? '保存修改' : '添加信息源'}
          </button>
        </form>

        <form className="source-form surface-card" onSubmit={saveProvider}>
          <div className="card-heading"><div><span className="eyebrow">OFFICIAL API</span><h3>官方 API 免费额度</h3></div></div>
          <p className="source-note">GitHub Token 与 NVD Key 均可选；未配置时使用匿名免费额度。GET 接口永不回传凭据明文。</p>
          <label className="field-stack">
            <span>Personal access token</span>
            <input type="password" autoComplete="new-password" value={githubToken} onChange={(event) => setGithubToken(event.target.value)} maxLength={2048} placeholder={providerConfig.github_token_configured ? '已配置，留空保持不变' : '可留空'} />
          </label>
          {providerConfig.github_token_configured ? (
            <label className="switch-line"><input type="checkbox" checked={clearGithubToken} onChange={(event) => setClearGithubToken(event.target.checked)} /><span><strong>清除已保存 Token</strong><small>保存后恢复匿名免费额度。</small></span></label>
          ) : null}
          <label className="field-stack">
            <span>NVD API Key</span>
            <input type="password" autoComplete="new-password" value={nvdApiKey} onChange={(event) => setNvdApiKey(event.target.value)} maxLength={2048} placeholder={providerConfig.nvd_api_key_configured ? '已配置，留空保持不变' : '可留空'} />
            <small>可选；未配置时使用 NVD 官方匿名免费额度。</small>
          </label>
          {providerConfig.nvd_api_key_configured ? (
            <label className="switch-line"><input type="checkbox" checked={clearNvdApiKey} onChange={(event) => setClearNvdApiKey(event.target.checked)} /><span><strong>清除已保存 NVD Key</strong><small>保存后恢复匿名免费额度。</small></span></label>
          ) : null}
          <button className="secondary-button" type="submit" disabled={providerSaving}><Save size={16} />{providerSaving ? '保存中…' : '保存 API 设置'}</button>
        </form>

      </div>
    </main>
  )
}
