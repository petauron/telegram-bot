import { useMemo } from 'react'
import {
  Area,
  AreaChart,
  CartesianGrid,
  Legend,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts'

function finiteCount(value) {
  const count = Number(value)
  return Number.isFinite(count) ? Math.max(0, count) : 0
}

export function filterQueueSamples(samples, rangeMs, now = Date.now()) {
  const cutoff = now - rangeMs
  return (samples || [])
    .filter((sample) => Number.isFinite(Number(sample?.timestamp)) && Number(sample.timestamp) >= cutoff)
    .map((sample) => ({
      timestamp: Number(sample.timestamp),
      pending: finiteCount(sample.analysisPending),
      processing: finiteCount(sample.analysisProcessing),
    }))
    .sort((left, right) => left.timestamp - right.timestamp)
}

function niceMaximum(value) {
  if (value <= 0) return 4
  const magnitude = 10 ** Math.floor(Math.log10(value))
  const normalized = value / magnitude
  const step = normalized <= 1 ? 1 : normalized <= 2 ? 2 : normalized <= 5 ? 5 : 10
  return Math.max(4, step * magnitude)
}

function formatChartTime(timestamp, includeSeconds = false) {
  return new Intl.DateTimeFormat('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    ...(includeSeconds ? { second: '2-digit' } : {}),
    hour12: false,
  }).format(timestamp)
}

const TOOLTIP_STYLE = {
  border: '1px solid rgba(0, 0, 0, .08)',
  borderRadius: 10,
  background: 'rgba(255, 255, 255, .97)',
  boxShadow: '0 7px 22px rgba(0, 0, 0, .1)',
  fontSize: 11,
}

export function LiveQueueChart({ samples, range = '15m', sampleIntervalSeconds = 10, retentionDays = 3 }) {
  const rangeMs = range === '1h' ? 60 * 60_000 : 15 * 60_000
  const now = Date.now()
  const data = useMemo(() => filterQueueSamples(samples, rangeMs, now), [samples, rangeMs, now])
  const maximum = useMemo(
    () => niceMaximum(Math.max(0, ...data.flatMap((item) => [item.pending, item.processing]))),
    [data],
  )
  const newest = data.at(-1)
  const includeSeconds = data.length > 1 && newest.timestamp - data[0].timestamp < 60_000
  const accessibleSummary = newest
    ? `分析队列实时图，最新待处理 ${newest.pending}，处理中 ${newest.processing}`
    : '分析队列实时图，正在等待首个样本'

  return (
    <div className="queue-chart-shell" data-chart-library="recharts">
      <div className="queue-chart-meta">系统每 {sampleIntervalSeconds} 秒持久化 · 保留 {retentionDays} 天</div>
      <div className="queue-chart-stage" role="img" aria-label={accessibleSummary}>
        <ResponsiveContainer
          className="queue-live-chart"
          width="100%"
          height="100%"
          minWidth={0}
          minHeight={220}
          debounce={80}
          initialDimension={{ width: 920, height: 250 }}
        >
          <AreaChart data={data} margin={{ top: 7, right: 8, bottom: 0, left: -18 }} accessibilityLayer>
            <defs>
              <linearGradient id="queuePendingFill" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="#007aff" stopOpacity={0.2} />
                <stop offset="100%" stopColor="#007aff" stopOpacity={0.015} />
              </linearGradient>
            </defs>
            <CartesianGrid vertical={false} stroke="#e5e5ea" strokeDasharray="3 4" />
            <XAxis
              dataKey="timestamp"
              tickFormatter={(value) => formatChartTime(value, includeSeconds)}
              axisLine={false}
              tickLine={false}
              minTickGap={28}
              tick={{ fill: '#86868b', fontSize: 10 }}
            />
            <YAxis
              allowDecimals={false}
              domain={[0, maximum]}
              axisLine={false}
              tickLine={false}
              tickCount={5}
              width={42}
              tick={{ fill: '#86868b', fontSize: 10 }}
            />
            <Tooltip
              cursor={{ stroke: '#8e8e93', strokeDasharray: '4 4' }}
              contentStyle={TOOLTIP_STYLE}
              labelStyle={{ color: '#6e6e73', marginBottom: 6 }}
              labelFormatter={(value) => formatChartTime(value, includeSeconds)}
              formatter={(value, name) => [finiteCount(value), name]}
              isAnimationActive={false}
            />
            <Legend
              align="left"
              verticalAlign="top"
              iconType="circle"
              iconSize={8}
              wrapperStyle={{ color: '#6e6e73', fontSize: 11, paddingBottom: 12 }}
            />
            <Area
              type="monotone"
              dataKey="pending"
              name="待处理"
              stroke="#007aff"
              strokeWidth={3}
              fill="url(#queuePendingFill)"
              activeDot={{ r: 4, strokeWidth: 2, stroke: '#fff' }}
              isAnimationActive={false}
            />
            <Line
              type="monotone"
              dataKey="processing"
              name="处理中"
              stroke="#34c759"
              strokeWidth={2.5}
              dot={false}
              activeDot={{ r: 4, strokeWidth: 2, stroke: '#fff' }}
              isAnimationActive={false}
            />
          </AreaChart>
        </ResponsiveContainer>
      </div>
      {data.length < 2 ? <p className="queue-chart-empty">正在积累实时走势，下一次采样后会连成曲线。</p> : null}
    </div>
  )
}
