export type MarketChartType = 'line' | 'bar' | 'doughnut'

export interface MarketChartSeries {
  label: string
  values: (number | null)[]
  record_ids: string[]
}

export interface MarketChart {
  chart_id: string
  chart_type: MarketChartType
  title: string
  x: string[]
  x_label?: string
  series: MarketChartSeries[]
  unit: string | null
  source_label: string
}

export interface ParsedMarketCharts {
  charts: MarketChart[]
  rejectedCount: number
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value)
}

function parseAxis(value: unknown): { labels: string[]; label?: string } | undefined {
  if (Array.isArray(value)) {
    return value.length >= 2 && value.every(label => typeof label === 'string')
      ? { labels: value }
      : undefined
  }
  if (!isRecord(value) || !Array.isArray(value.values)) return undefined
  if (value.values.length < 2 || !value.values.every(label => typeof label === 'string')) return undefined
  return {
    labels: value.values,
    ...(typeof value.label === 'string' && value.label ? { label: value.label } : {}),
  }
}

function parseSeries(value: unknown, pointCount: number): MarketChartSeries[] | undefined {
  if (!Array.isArray(value) || value.length === 0) return undefined
  const parsed: MarketChartSeries[] = []
  for (const candidate of value) {
    if (!isRecord(candidate)) return undefined
    if (typeof candidate.label !== 'string') return undefined
    if (!Array.isArray(candidate.values) || candidate.values.length !== pointCount) return undefined
    if (!candidate.values.every(point => point === null || (typeof point === 'number' && Number.isFinite(point)))) {
      return undefined
    }
    if (!Array.isArray(candidate.record_ids) || candidate.record_ids.length !== pointCount) return undefined
    if (!candidate.record_ids.every(recordId => typeof recordId === 'string' && recordId.length > 0)) {
      return undefined
    }
    parsed.push({
      label: candidate.label,
      values: candidate.values,
      record_ids: candidate.record_ids,
    })
  }
  return parsed
}

function parseChart(value: unknown): MarketChart | undefined {
  if (!isRecord(value)) return undefined
  if (typeof value.chart_id !== 'string' || !value.chart_id) return undefined
  if (!['line', 'bar', 'doughnut'].includes(String(value.chart_type))) return undefined
  if (typeof value.title !== 'string' || typeof value.source_label !== 'string') return undefined
  if (value.unit !== null && typeof value.unit !== 'string') return undefined

  const axis = parseAxis(value.x)
  if (!axis) return undefined
  const series = parseSeries(value.series, axis.labels.length)
  if (!series) return undefined

  return {
    chart_id: value.chart_id,
    chart_type: value.chart_type as MarketChartType,
    title: value.title,
    x: axis.labels,
    ...(axis.label ? { x_label: axis.label } : {}),
    series,
    unit: value.unit,
    source_label: value.source_label,
  }
}

export function parseMarketCharts(value: unknown): ParsedMarketCharts | undefined {
  if (!Array.isArray(value)) return undefined
  const charts: MarketChart[] = []
  const ids = new Set<string>()
  let rejectedCount = 0

  for (const candidate of value) {
    const chart = parseChart(candidate)
    if (!chart || ids.has(chart.chart_id)) {
      rejectedCount += 1
      continue
    }
    ids.add(chart.chart_id)
    charts.push(chart)
  }
  return { charts, rejectedCount }
}
