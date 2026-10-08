import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from 'react'
import { api, isUnauthorized, params } from './api'
import { authReducer, INITIAL_AUTH_STATE, isAuthenticated } from './auth'
import { useDebouncedValue } from './hooks'
import { Header } from './components/Header'
import { AuthCheckingView, LoginView } from './components/LoginView'
import { DEFAULT_MIN_SCORE } from './components/Filters'
import { MessageStreamView } from './components/MessageStreamView'
import { InformationSourcesView } from './components/InformationSourcesView'
import { FeedbackView } from './components/FeedbackView'
import { OverviewView } from './components/OverviewView'
import { PushChannelsView } from './components/PushChannelsView'
import { SettingsView } from './components/SettingsView'
import { Sidebar } from './components/Sidebar'
import { StatusView } from './components/StatusView'

const PAGE_SIZE = 20
const DEFAULT_CLASSIFICATION_MODEL = 'gemini-3.5-flash-extra-low'
export const PUSH_SCORE_THRESHOLD = 60
const POLLED_PAGES = new Set(['overview', 'messages', 'sources', 'feedback', 'status'])
const DEFAULT_POLL_INTERVAL_MS = 30_000
const SOURCE_STATUS_POLL_INTERVAL_MS = 5_000
const QUEUE_STATUS_POLL_INTERVAL_MS = 10_000

export function shouldPollPage(page) {
  return POLLED_PAGES.has(page)
}

export function pollIntervalMs(page) {
  if (page === 'sources') return SOURCE_STATUS_POLL_INTERVAL_MS
  if (page === 'status') return QUEUE_STATUS_POLL_INTERVAL_MS
  return DEFAULT_POLL_INTERVAL_MS
}

export function queueHistoryFromStatus(status) {
  return (status?.queue_history || []).flatMap((sample) => {
    const timestamp = Date.parse(sample?.sampled_at)
    if (!Number.isFinite(timestamp)) return []
    return [{
      timestamp,
      analysisPending: Number(sample.analysis_pending) || 0,
      analysisProcessing: Number(sample.analysis_processing) || 0,
    }]
  })
}

export function shouldLoadCachedResource(isLoaded, force = false) {
  return !isLoaded || force
}
export const INITIAL_FILTERS = {
  q: '',
  chatId: '',
  minScore: DEFAULT_MIN_SCORE,
  hours: '24',
  pushStatus: 'all',
  prefilterStatus: 'exclude',
  similarStatus: 'exclude',
}

function parseConfig(config) {
  return {
    keywords: (config?.important_keywords || []).join(', '),
    trustedIds: (config?.trusted_sender_ids || []).join(', '),
    watchChatIds: config?.watch_chat_ids || [],
    immediateScore: String(PUSH_SCORE_THRESHOLD),
  }
}

function parseModelConfig(config) {
  return {
    enabled: Boolean(config?.enabled),
    communityInsightsEnabled: config?.community_insights_enabled !== false,
    benefitDealsEnabled: config?.benefit_deals_enabled !== false,
    baseUrl: config?.base_url || 'https://model.example.com/v1',
    apiKey: '',
    clearApiKey: false,
    classificationModel: config?.classification_model || DEFAULT_CLASSIFICATION_MODEL,
    classificationReasoningEffort: config?.classification_reasoning_effort || 'low',
    model: config?.model || '',
    reasoningEffort: config?.reasoning_effort || 'default',
    semanticDedupeModel: config?.semantic_dedupe_model || DEFAULT_CLASSIFICATION_MODEL,
    semanticDedupeReasoningEffort: config?.semantic_dedupe_reasoning_effort || 'low',
    notificationModel: config?.notification_model || config?.model || DEFAULT_CLASSIFICATION_MODEL,
    notificationReasoningEffort: config?.notification_reasoning_effort || 'low',
  }
}

function parsePushConfig(config) {
  return {
    telegramEnabled: Boolean(config?.telegram?.enabled),
    telegramBotToken: '',
    clearTelegramBotToken: false,
    telegramChatId: config?.telegram?.chat_id || '',
    ntfyEnabled: Boolean(config?.ntfy?.enabled),
    ntfyBaseUrl: config?.ntfy?.base_url || 'https://ntfy.example.com',
    ntfyTopic: config?.ntfy?.topic || '',
    ntfyCommunityTopic: config?.ntfy?.community_topic || config?.ntfy?.topic || '',
    ntfyBenefitTopic: config?.ntfy?.benefit_topic || config?.ntfy?.topic || '',
    ntfyAccessToken: '',
    clearNtfyAccessToken: false,
  }
}

function splitValues(value) {
  return value.split(/[,，\n]/).map((item) => item.trim()).filter(Boolean)
}

function isHeartbeatOnline(stats) {
  const heartbeat = stats?.heartbeat
  if (!heartbeat?.connected || !heartbeat.updated_at) return false
  return Date.now() - new Date(heartbeat.updated_at).getTime() < 45_000
}

function Dashboard({ username, onUnauthorized, onLogout }) {
  const [active, setActive] = useState('overview')
  const [filters, setFilters] = useState(INITIAL_FILTERS)
  const debouncedQuery = useDebouncedValue(filters.q)
  const [page, setPage] = useState(1)
  const [rows, setRows] = useState([])
  const [overviewRows, setOverviewRows] = useState([])
  const [total, setTotal] = useState(0)
  const [selected, setSelected] = useState(null)
  const [stats, setStats] = useState(null)
  const [statusHistory, setStatusHistory] = useState([])
  const [messageChats, setMessageChats] = useState([])
  const [availableChats, setAvailableChats] = useState([])
  const [sources, setSources] = useState([])
  const [sourceProviderConfig, setSourceProviderConfig] = useState({ github_token_configured: false, nvd_api_key_configured: false })
  const [config, setConfig] = useState(null)
  const [form, setForm] = useState(null)
  const [modelConfig, setModelConfig] = useState(null)
  const [modelForm, setModelForm] = useState(null)
  const [models, setModels] = useState([])
  const [pushConfig, setPushConfig] = useState(null)
  const [pushForm, setPushForm] = useState(null)
  const [feedback, setFeedback] = useState(null)
  const [pushStatus, setPushStatus] = useState({ kind: 'idle', message: '' })
  const [modelStatus, setModelStatus] = useState({ kind: 'idle', message: '' })
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [saving, setSaving] = useState(false)
  const [modelRefreshing, setModelRefreshing] = useState(false)
  const [modelSaving, setModelSaving] = useState(false)
  const [pushSaving, setPushSaving] = useState(false)
  const [sourceSaving, setSourceSaving] = useState(false)
  const [sourceProviderSaving, setSourceProviderSaving] = useState(false)
  const [testingChannel, setTestingChannel] = useState('')
  const [analyzing, setAnalyzing] = useState(false)
  const [loggingOut, setLoggingOut] = useState(false)
  const [lastSync, setLastSync] = useState(null)
  const [clock, setClock] = useState(Date.now())
  const [toast, setToast] = useState(null)
  const selectionRequest = useRef(0)
  const requests = useRef({ overview: 0, messages: 0, sources: 0, feedback: 0, status: 0, config: 0, push: 0 })
  const loaded = useRef({ config: false, push: false, sources: false })

  const online = isHeartbeatOnline(stats)

  const handleRequestError = useCallback((error) => {
    if (isUnauthorized(error)) {
      onUnauthorized()
      return true
    }
    setToast({ type: 'error', message: error.message })
    return false
  }, [onUnauthorized])

  useEffect(() => {
    const interval = window.setInterval(() => setClock(Date.now()), 5_000)
    return () => window.clearInterval(interval)
  }, [])

  useEffect(() => setPage(1), [
    debouncedQuery,
    filters.chatId,
    filters.minScore,
    filters.hours,
    filters.pushStatus,
    filters.prefilterStatus,
  ])

  const beginRequest = useCallback((name) => {
    const requestId = requests.current[name] + 1
    requests.current[name] = requestId
    return requestId
  }, [])

  const isCurrentRequest = useCallback(
    (name, requestId) => requests.current[name] === requestId,
    [],
  )

  const loadOverview = useCallback(async ({ quiet = false } = {}) => {
    const requestId = beginRequest('overview')
    if (!quiet) setRefreshing(true)
    if (!quiet) setLoading(true)
    try {
      const overviewQuery = params({
        hours: 24,
        prefilter_status: 'exclude',
        attention_only: true,
        limit: 6,
        offset: 0,
      })
      const [overviewData, statData] = await Promise.all([
        api(`/api/messages?${overviewQuery}`),
        api('/api/stats?hours=24'),
      ])
      if (!isCurrentRequest('overview', requestId)) return
      setOverviewRows(overviewData.items)
      setStats(statData)
      setLastSync(Date.now())
    } catch (error) {
      if (isCurrentRequest('overview', requestId)) handleRequestError(error)
    } finally {
      if (isCurrentRequest('overview', requestId)) {
        setLoading(false)
        setRefreshing(false)
      }
    }
  }, [beginRequest, handleRequestError, isCurrentRequest])

  const loadMessages = useCallback(async ({ quiet = false } = {}) => {
    const requestId = beginRequest('messages')
    if (!quiet) setRefreshing(true)
    if (!quiet) setLoading(true)
    try {
      const query = params({
        hours: filters.hours,
        q: debouncedQuery,
        chat_id: filters.chatId,
        min_score: filters.minScore,
        push_status: filters.pushStatus,
        prefilter_status: filters.prefilterStatus,
        similar_status: filters.similarStatus,
        limit: PAGE_SIZE,
        offset: (page - 1) * PAGE_SIZE,
      })
      const [messageData, chatData] = await Promise.all([
        api(`/api/messages?${query}`),
        api('/api/chats?hours=720&recorded_only=true&include_sources=true'),
      ])
      if (!isCurrentRequest('messages', requestId)) return
      setRows(messageData.items)
      setTotal(messageData.total)
      setMessageChats(chatData.items)
      setSelected((current) => {
        if (!current) return null
        const next = messageData.items.find((row) => row.id === current.id)
        return next ? { ...current, ...next } : current
      })
      setLastSync(Date.now())
    } catch (error) {
      if (isCurrentRequest('messages', requestId)) handleRequestError(error)
    } finally {
      if (isCurrentRequest('messages', requestId)) {
        setLoading(false)
        setRefreshing(false)
      }
    }
  }, [
    beginRequest,
    debouncedQuery,
    filters.chatId,
    filters.hours,
    filters.minScore,
    filters.pushStatus,
    filters.prefilterStatus,
    filters.similarStatus,
    handleRequestError,
    isCurrentRequest,
    page,
  ])

  const loadStatus = useCallback(async ({ quiet = false } = {}) => {
    const requestId = beginRequest('status')
    if (!quiet) setRefreshing(true)
    if (!quiet) setLoading(true)
    try {
      const value = await api('/api/status?hours=24')
      if (!isCurrentRequest('status', requestId)) return
      setStats((current) => ({ ...current, ...value }))
      setStatusHistory(queueHistoryFromStatus(value))
      setLastSync(Date.now())
    } catch (error) {
      if (isCurrentRequest('status', requestId)) handleRequestError(error)
    } finally {
      if (isCurrentRequest('status', requestId)) {
        setLoading(false)
        setRefreshing(false)
      }
    }
  }, [beginRequest, handleRequestError, isCurrentRequest])

  const loadSources = useCallback(async ({ quiet = false, force = false } = {}) => {
    if (!shouldLoadCachedResource(loaded.current.sources, force) && !quiet) return
    const requestId = beginRequest('sources')
    if (!quiet) setRefreshing(true)
    if (!quiet && !loaded.current.sources) setLoading(true)
    try {
      const providerRequest = loaded.current.sources
        ? Promise.resolve(null)
        : api('/api/source-provider-config')
      const [value, providerConfig] = await Promise.all([api('/api/sources'), providerRequest])
      if (!isCurrentRequest('sources', requestId)) return
      setSources(value.items)
      if (providerConfig) setSourceProviderConfig(providerConfig)
      loaded.current.sources = true
      setLastSync(Date.now())
    } catch (error) {
      if (isCurrentRequest('sources', requestId)) handleRequestError(error)
    } finally {
      if (isCurrentRequest('sources', requestId)) {
        setLoading(false)
        setRefreshing(false)
      }
    }
  }, [beginRequest, handleRequestError, isCurrentRequest])

  const loadConfig = useCallback(async ({ quiet = false, force = false } = {}) => {
    if (!shouldLoadCachedResource(loaded.current.config, force)) return
    const requestId = beginRequest('config')
    if (!quiet) setRefreshing(true)
    if (!quiet && !loaded.current.config) setLoading(true)
    try {
      const [configData, modelConfigData, chatData] = await Promise.all([
        api('/api/config'),
        api('/api/model-config'),
        api('/api/chats?hours=720'),
      ])
      if (!isCurrentRequest('config', requestId)) return
      setConfig(configData)
      setForm(parseConfig(configData))
      setModelConfig(modelConfigData)
      setModelForm(parseModelConfig(modelConfigData))
      setAvailableChats(chatData.items)
      loaded.current.config = true
      setLastSync(Date.now())
    } catch (error) {
      if (isCurrentRequest('config', requestId)) handleRequestError(error)
    } finally {
      if (isCurrentRequest('config', requestId)) {
        setLoading(false)
        setRefreshing(false)
      }
    }
  }, [beginRequest, handleRequestError, isCurrentRequest])

  const loadPush = useCallback(async ({ quiet = false, force = false } = {}) => {
    if (!shouldLoadCachedResource(loaded.current.push, force)) return
    const requestId = beginRequest('push')
    if (!quiet) setRefreshing(true)
    if (!quiet && !loaded.current.push) setLoading(true)
    try {
      const value = await api('/api/push-config')
      if (!isCurrentRequest('push', requestId)) return
      setPushConfig(value)
      setPushForm(parsePushConfig(value))
      loaded.current.push = true
      setLastSync(Date.now())
    } catch (error) {
      if (isCurrentRequest('push', requestId)) handleRequestError(error)
    } finally {
      if (isCurrentRequest('push', requestId)) {
        setLoading(false)
        setRefreshing(false)
      }
    }
  }, [beginRequest, handleRequestError, isCurrentRequest])

  const loadFeedback = useCallback(async ({ quiet = false } = {}) => {
    const requestId = beginRequest('feedback')
    if (!quiet) setRefreshing(true)
    if (!quiet) setLoading(true)
    try {
      const value = await api('/api/feedback?hours=2160&limit=100')
      if (!isCurrentRequest('feedback', requestId)) return
      setFeedback(value)
      setLastSync(Date.now())
    } catch (error) {
      if (isCurrentRequest('feedback', requestId)) handleRequestError(error)
    } finally {
      if (isCurrentRequest('feedback', requestId)) {
        setLoading(false)
        setRefreshing(false)
      }
    }
  }, [beginRequest, handleRequestError, isCurrentRequest])

  const loadActive = useCallback((options = {}) => {
    if (active === 'overview') return loadOverview(options)
    if (active === 'messages') return loadMessages(options)
    if (active === 'status') return loadStatus(options)
    if (active === 'sources') return loadSources(options)
    if (active === 'config') return loadConfig(options)
    if (active === 'push') return loadPush(options)
    if (active === 'feedback') return loadFeedback(options)
    return Promise.resolve()
  }, [active, loadConfig, loadFeedback, loadMessages, loadOverview, loadPush, loadSources, loadStatus])

  useEffect(() => { void loadActive({ quiet: false }) }, [loadActive])
  useEffect(() => {
    if (!shouldPollPage(active)) return undefined
    const interval = window.setInterval(
      () => { void loadActive({ quiet: true }) },
      pollIntervalMs(active),
    )
    return () => window.clearInterval(interval)
  }, [active, loadActive])

  useEffect(() => {
    if (!toast) return undefined
    const timeout = window.setTimeout(() => setToast(null), 4_000)
    return () => window.clearTimeout(timeout)
  }, [toast])

  const saveRules = useCallback(async () => {
    if (!form) return
    const trusted = splitValues(form.trustedIds)
    if (trusted.some((value) => !/^-?\d+$/.test(value))) {
      setToast({ type: 'error', message: '可信发送者 ID 必须是整数' })
      return
    }
    setSaving(true)
    try {
      const saved = await api('/api/config', {
        method: 'PUT',
        headers: { 'X-Requested-With': 'admin-ui' },
        body: JSON.stringify({
          important_keywords: splitValues(form.keywords),
          trusted_sender_ids: trusted.map(Number),
          watch_chat_ids: form.watchChatIds,
          immediate_score: PUSH_SCORE_THRESHOLD,
        }),
      })
      setConfig((current) => ({ ...current, ...saved }))
      setForm(parseConfig(saved))
      setToast({ type: 'success', message: '监听与评分规则已保存并立即生效' })
    } catch (error) {
      handleRequestError(error)
    } finally {
      setSaving(false)
    }
  }, [form, handleRequestError])

  const changeModelForm = useCallback((updater) => {
    setModelForm((current) => (typeof updater === 'function' ? updater(current) : updater))
    setModelStatus({ kind: 'idle', message: '' })
  }, [])

  const refreshModels = useCallback(async () => {
    if (!modelForm) return
    setModelRefreshing(true)
    setModelStatus({ kind: 'loading', message: '正在从模型服务加载列表…' })
    try {
      const response = await api('/api/model-config/models', {
        method: 'POST',
        headers: { 'X-Requested-With': 'admin-ui' },
        body: JSON.stringify({ base_url: modelForm.baseUrl, api_key: modelForm.apiKey }),
      })
      setModels(response.items)
      setModelForm((current) => {
        if (!current) return current
        const nextModel = response.items.includes(current.model) ? current.model : (response.items[0] || '')
        return {
          ...current,
          classificationModel: current.classificationModel || DEFAULT_CLASSIFICATION_MODEL,
          model: nextModel,
          semanticDedupeModel: current.semanticDedupeModel || (response.items[0] || ''),
          notificationModel: current.notificationModel || (response.items[0] || ''),
        }
      })
      setModelStatus({ kind: 'success', message: `已加载 ${response.items.length} 个可用模型，请选择后保存。` })
    } catch (error) {
      if (!handleRequestError(error)) setModelStatus({ kind: 'error', message: error.message })
    } finally {
      setModelRefreshing(false)
    }
  }, [handleRequestError, modelForm])

  const saveModelConfig = useCallback(async () => {
    if (!modelForm) return
    setModelSaving(true)
    setModelStatus({ kind: 'loading', message: '正在保存模型配置…' })
    try {
      const saved = await api('/api/model-config', {
        method: 'PUT',
        headers: { 'X-Requested-With': 'admin-ui' },
        body: JSON.stringify({
          enabled: modelForm.enabled,
          community_insights_enabled: modelForm.communityInsightsEnabled,
          benefit_deals_enabled: modelForm.benefitDealsEnabled,
          base_url: modelForm.baseUrl,
          api_key: modelForm.apiKey,
          clear_api_key: modelForm.clearApiKey,
          classification_model: modelForm.classificationModel,
          classification_reasoning_effort: modelForm.classificationReasoningEffort,
          model: modelForm.model,
          reasoning_effort: modelForm.reasoningEffort,
          semantic_dedupe_model: modelForm.semanticDedupeModel,
          semantic_dedupe_reasoning_effort: modelForm.semanticDedupeReasoningEffort,
          notification_model: modelForm.notificationModel,
          notification_reasoning_effort: modelForm.notificationReasoningEffort,
        }),
      })
      setModelConfig(saved)
      setModelForm(parseModelConfig(saved))
      setModelStatus({ kind: 'saved', message: '模型配置已保存并立即生效；API Key 明文已从表单清除。' })
      setToast({ type: 'success', message: '模型分析配置已保存并立即生效' })
    } catch (error) {
      if (!handleRequestError(error)) setModelStatus({ kind: 'error', message: error.message })
    } finally {
      setModelSaving(false)
    }
  }, [handleRequestError, modelForm])

  const changePushForm = useCallback((updater) => {
    setPushForm((current) => (typeof updater === 'function' ? updater(current) : updater))
    setPushStatus({ kind: 'idle', message: '' })
  }, [])

  const savePushConfig = useCallback(async () => {
    if (!pushForm) return
    setPushSaving(true)
    setPushStatus({ kind: 'loading', message: '正在保存推送渠道配置…' })
    try {
      const saved = await api('/api/push-config', {
        method: 'PUT',
        body: JSON.stringify({
          telegram_enabled: pushForm.telegramEnabled,
          telegram_bot_token: pushForm.telegramBotToken,
          clear_telegram_bot_token: pushForm.clearTelegramBotToken,
          telegram_chat_id: pushForm.telegramChatId,
          ntfy_enabled: pushForm.ntfyEnabled,
          ntfy_base_url: pushForm.ntfyBaseUrl,
          ntfy_topic: pushForm.ntfyTopic,
          ntfy_community_topic: pushForm.ntfyCommunityTopic,
          ntfy_benefit_topic: pushForm.ntfyBenefitTopic,
          ntfy_access_token: pushForm.ntfyAccessToken,
          clear_ntfy_access_token: pushForm.clearNtfyAccessToken,
        }),
      })
      setPushConfig(saved)
      setPushForm(parsePushConfig(saved))
      loaded.current.push = true
      setPushStatus({ kind: 'saved', message: '推送渠道已保存并立即生效；Token 明文已从表单清除。' })
      setToast({ type: 'success', message: '推送渠道配置已保存并立即生效' })
    } catch (error) {
      if (!handleRequestError(error)) setPushStatus({ kind: 'error', message: error.message })
    } finally {
      setPushSaving(false)
    }
  }, [handleRequestError, pushForm])

  const testPushChannel = useCallback(async (channel) => {
    if (testingChannel) return
    setTestingChannel(channel)
    setPushStatus({ kind: 'loading', message: `正在测试 ${channel === 'telegram' ? 'Telegram Push Bot' : 'ntfy'}…` })
    try {
      await api('/api/push-config/test', {
        method: 'POST',
        body: JSON.stringify({ channel }),
      })
      setPushStatus({ kind: 'success', message: `${channel === 'telegram' ? 'Telegram Push Bot' : 'ntfy'} 测试推送已发送。` })
    } catch (error) {
      if (!handleRequestError(error)) setPushStatus({ kind: 'error', message: error.message })
    } finally {
      setTestingChannel('')
    }
  }, [handleRequestError, testingChannel])

  const saveSource = useCallback(async (formValue, sourceId = null) => {
    if (sourceSaving) return null
    beginRequest('sources')
    setSourceSaving(true)
    try {
      const response = await api(sourceId ? `/api/sources/${sourceId}` : '/api/sources', {
        method: sourceId ? 'PUT' : 'POST',
        body: JSON.stringify({
          kind: formValue.kind,
          name: formValue.name,
          url: formValue.url,
          enabled: formValue.enabled,
          poll_interval_minutes: Number(formValue.pollIntervalMinutes),
          include_prereleases: Boolean(formValue.includePrereleases),
          ecosystem: formValue.ecosystem,
          minimum_severity: formValue.minimumSeverity,
          keywords: splitValues(formValue.keywords),
          story_list: formValue.storyList,
          minimum_score: Number(formValue.minimumScore),
          bluesky_handle: formValue.blueskyHandle,
          mastodon_instance_url: formValue.mastodonInstanceUrl,
          mastodon_timeline_type: formValue.mastodonTimelineType,
          mastodon_target: formValue.mastodonTarget,
          source_secret: formValue.sourceSecret,
          clear_source_secret: formValue.clearSourceSecret,
          imap_host: formValue.imapHost,
          imap_port: Number(formValue.imapPort),
          imap_username: formValue.imapUsername,
          imap_mailbox: formValue.imapMailbox,
          sender_allowlist: splitValues(formValue.senderAllowlist),
        }),
      })
      setSources((current) => {
        const found = current.some((item) => item.id === response.item.id)
        return found
          ? current.map((item) => (item.id === response.item.id ? response.item : item))
          : [...current, response.item].sort((a, b) => a.name.localeCompare(b.name, 'zh-CN'))
      })
      setToast({
        type: 'success',
        message: sourceId ? '信息源已更新' : '信息源已保存；首次抓取只建立基线，不推送历史文章',
      })
      return response.item
    } catch (error) {
      handleRequestError(error)
      return null
    } finally {
      setSourceSaving(false)
    }
  }, [beginRequest, handleRequestError, sourceSaving])

  const saveSourceProviderConfig = useCallback(async (value) => {
    if (sourceProviderSaving) return false
    setSourceProviderSaving(true)
    try {
      const response = await api('/api/source-provider-config', {
        method: 'PUT',
        body: JSON.stringify({
          github_token: value.githubToken,
          clear_github_token: value.clearGithubToken,
          nvd_api_key: value.nvdApiKey,
          clear_nvd_api_key: value.clearNvdApiKey,
        }),
      })
      setSourceProviderConfig(response)
      setToast({ type: 'success', message: 'GitHub 凭据设置已保存' })
      return true
    } catch (error) {
      handleRequestError(error)
      return false
    } finally {
      setSourceProviderSaving(false)
    }
  }, [handleRequestError, sourceProviderSaving])

  const refreshSource = useCallback(async (sourceId) => {
    beginRequest('sources')
    try {
      const response = await api(`/api/sources/${sourceId}/refresh`, {
        method: 'POST',
        body: JSON.stringify({}),
      })
      setSources((current) => current.map((item) => (
        item.id === response.item.id ? { ...item, ...response.item } : item
      )))
      setToast({ type: 'success', message: '已安排立即抓取' })
      return true
    } catch (error) {
      handleRequestError(error)
      return false
    }
  }, [beginRequest, handleRequestError])

  const selectMessage = useCallback(async (row) => {
    const requestId = selectionRequest.current + 1
    selectionRequest.current = requestId
    setSelected(row)
    try {
      const detail = await api(`/api/messages/${row.id}`)
      if (selectionRequest.current === requestId) setSelected(detail)
    } catch (error) {
      handleRequestError(error)
    }
  }, [handleRequestError])

  const reanalyzeMessage = useCallback(async () => {
    if (!selected || analyzing) return
    setAnalyzing(true)
    try {
      const detail = await api(`/api/messages/${selected.id}/reanalyze`, {
        method: 'POST',
        body: JSON.stringify({}),
      })
      setSelected(detail)
      setRows((current) => current.map((row) => (row.id === detail.id ? { ...row, ...detail } : row)))
      setOverviewRows((current) => current.map((row) => (row.id === detail.id ? { ...row, ...detail } : row)))
      setToast({
        type: 'success',
        message: detail.queue_created
          ? '已加入重新分析队列；完成后仍不会自动补推历史消息'
          : '该消息已有分析任务，未重复入队',
      })
    } catch (error) {
      handleRequestError(error)
    } finally {
      setAnalyzing(false)
    }
  }, [analyzing, handleRequestError, selected])

  const logout = useCallback(async () => {
    if (loggingOut) return
    setLoggingOut(true)
    try {
      await onLogout()
    } catch (error) {
      handleRequestError(error)
    } finally {
      setLoggingOut(false)
    }
  }, [handleRequestError, loggingOut, onLogout])

  const syncedLabel = useMemo(() => {
    if (!lastSync) return '—'
    const seconds = Math.max(0, Math.floor((clock - lastSync) / 1000))
    return seconds < 5 ? '刚刚' : `${seconds} 秒前`
  }, [clock, lastSync])

  const openOverviewMessage = useCallback((row) => {
    setActive('messages')
    void selectMessage(row)
  }, [selectMessage])

  const navigate = useCallback((destination) => {
    setActive(destination)
    if (destination === 'messages') setSelected(null)
  }, [])

  return (
    <div className={`app-shell ${active === 'messages' && selected ? 'detail-open' : ''}`}>
      <Sidebar active={active} onNavigate={navigate} online={online} username={username} />
      <div className="app-main">
        <Header
          syncedLabel={syncedLabel}
          refreshing={refreshing}
          onRefresh={() => { void loadActive({ quiet: false, force: true }) }}
          username={username}
          onLogout={logout}
          loggingOut={loggingOut}
        />
        {active === 'config' ? (
          <SettingsView
            form={form}
            config={config}
            chats={availableChats}
            onChange={setForm}
            onSave={saveRules}
            saving={saving}
            modelForm={modelForm}
            modelConfig={modelConfig}
            models={models}
            modelStatus={modelStatus}
            onModelChange={changeModelForm}
            onRefreshModels={refreshModels}
            onSaveModel={saveModelConfig}
            modelRefreshing={modelRefreshing}
            modelSaving={modelSaving}
          />
        ) : null}
        {active === 'status' ? <StatusView stats={stats} history={statusHistory} online={online} loading={loading} /> : null}
        {active === 'feedback' ? <FeedbackView data={feedback} loading={loading} /> : null}
        {active === 'push' ? (
          <PushChannelsView
            form={pushForm}
            config={pushConfig}
            status={pushStatus}
            onChange={changePushForm}
            onSave={savePushConfig}
            onTest={testPushChannel}
            saving={pushSaving}
            testingChannel={testingChannel}
          />
        ) : null}
        {active === 'sources' ? (
          <InformationSourcesView
            sources={sources}
            loading={loading}
            saving={sourceSaving}
            providerConfig={sourceProviderConfig}
            providerSaving={sourceProviderSaving}
            onSave={saveSource}
            onRefresh={refreshSource}
            onSaveProviderConfig={saveSourceProviderConfig}
          />
        ) : null}
        {active === 'overview' ? (
          <OverviewView
            stats={stats}
            rows={overviewRows}
            online={online}
            loading={loading}
            onOpenMessage={openOverviewMessage}
            onOpenMessages={() => setActive('messages')}
          />
        ) : null}
        {active === 'messages' ? (
          <MessageStreamView
            filters={filters}
            chats={messageChats}
            onFiltersChange={setFilters}
            rows={rows}
            total={total}
            selected={selected}
            onSelect={selectMessage}
            page={page}
            pageSize={PAGE_SIZE}
            onPage={setPage}
            loading={loading}
            onReanalyze={reanalyzeMessage}
            analyzing={analyzing}
            onCloseDetail={() => setSelected(null)}
          />
        ) : null}
      </div>
      {toast ? <div className={`toast ${toast.type}`} role="status">{toast.message}</div> : null}
    </div>
  )
}


export default function App() {
  const [auth, dispatch] = useReducer(authReducer, INITIAL_AUTH_STATE)

  useEffect(() => {
    let active = true
    api('/api/auth/session')
      .then((session) => {
        if (!active) return
        dispatch({
          type: session.authenticated ? 'SESSION_AUTHENTICATED' : 'SESSION_ANONYMOUS',
          username: session.username,
        })
      })
      .catch(() => {
        if (active) dispatch({ type: 'SESSION_FAILED' })
      })
    return () => { active = false }
  }, [])

  const authenticate = useCallback((credentials) => api('/api/auth/login', {
    method: 'POST',
    body: JSON.stringify(credentials),
  }), [])

  const authenticated = useCallback((session) => {
    dispatch({ type: 'LOGIN_SUCCEEDED', username: session.username })
  }, [])

  const unauthorized = useCallback(() => {
    dispatch({ type: 'BUSINESS_UNAUTHORIZED' })
  }, [])

  const logout = useCallback(async () => {
    try {
      await api('/api/auth/logout', {
        method: 'POST',
        body: JSON.stringify({}),
      })
    } catch (error) {
      if (!isUnauthorized(error)) throw error
    }
    dispatch({ type: 'LOGOUT_SUCCEEDED' })
  }, [])

  if (auth.status === 'checking') return <AuthCheckingView />
  if (!isAuthenticated(auth)) {
    return (
      <LoginView
        key={auth.notice || 'login'}
        notice={auth.notice}
        onAuthenticate={authenticate}
        onAuthenticated={authenticated}
      />
    )
  }
  return (
    <Dashboard
      username={auth.username}
      onUnauthorized={unauthorized}
      onLogout={logout}
    />
  )
}
