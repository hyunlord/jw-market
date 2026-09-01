import { buildBrandActivityScopeRequest, type BrandActivityScope } from './brandActivityScope.ts'

export type ActivitySeriesRequestOptions = {
  readonly entityLevel?: 'brand' | 'company'
  readonly csdChannel?: string
  readonly csdMarket?: string
  readonly visit?: string
  readonly specialty?: string
}

export type ActivitySeriesRequest = {
  readonly view: string
  readonly market_id?: string
  readonly selected_brand: string
  readonly filters: { readonly atc4: readonly string[] } | Readonly<Record<string, never>>
  readonly period: { readonly start: string; readonly end: string }
  readonly entity_level?: 'company'
  readonly csd_channel?: string
  readonly csd_market?: string
  readonly visit_location?: string
  readonly specialty?: string
}

export type CsdMarketScope = {
  readonly csd_market?: string
  readonly csd_markets: readonly string[]
}

type CsdMarketScopeInput = {
  readonly csd_market?: string
  readonly csd_markets?: readonly string[]
}

export type CsdMarketOption = {
  readonly value: string
  readonly label: string
}

export type TopicPeriodBounds = {
  readonly available_start?: string
  readonly available_end?: string
}

export function resolveTopicMonths(
  period: TopicPeriodBounds | null,
): readonly string[] {
  const startMatch = /^(\d{4})-(0[1-9]|1[0-2])$/.exec(period?.available_start ?? '')
  const endMatch = /^(\d{4})-(0[1-9]|1[0-2])$/.exec(period?.available_end ?? '')
  if (!startMatch || !endMatch) return []

  const start = Number(startMatch[1]) * 12 + Number(startMatch[2]) - 1
  const end = Number(endMatch[1]) * 12 + Number(endMatch[2]) - 1
  if (start > end) return []

  return Array.from({ length: end - start + 1 }, (_, offset) => {
    const monthIndex = start + offset
    const year = Math.floor(monthIndex / 12)
    const month = String(monthIndex % 12 + 1).padStart(2, '0')
    return `${year}-${month}`
  })
}

export function normalizeCsdMarketScope(scope: CsdMarketScopeInput | undefined): CsdMarketScope {
  return {
    ...(scope?.csd_market ? { csd_market: scope.csd_market } : {}),
    csd_markets: scope?.csd_markets ?? [],
  }
}

export function buildCsdMarketOptions(markets: readonly string[]): readonly CsdMarketOption[] {
  return [
    { value: 'all', label: '시장 전체' },
    ...markets.map(market => ({ value: market, label: market })),
  ]
}

export function buildActivitySeriesRequest(
  selectedBrand: string,
  atc4: readonly string[],
  scope: BrandActivityScope,
  options: ActivitySeriesRequestOptions = {},
): ActivitySeriesRequest {
  const entityLevel = options.entityLevel ?? 'brand'
  const csdChannel = options.csdChannel ?? 'TOTAL'
  const visit = options.visit ?? '전체'
  const specialty = options.specialty ?? '전체'

  return {
    ...buildBrandActivityScopeRequest(scope, { atc4 }),
    selected_brand: selectedBrand,
    period: { start: '2021-Q1', end: '2026-Q4' },
    ...(entityLevel === 'company' ? { entity_level: 'company' as const } : {}),
    ...(csdChannel !== 'TOTAL' ? { csd_channel: csdChannel } : {}),
    ...(options.csdMarket && options.csdMarket !== 'all' ? { csd_market: options.csdMarket } : {}),
    ...(visit !== '전체' ? { visit_location: visit } : {}),
    ...(specialty !== '전체' ? { specialty } : {}),
  }
}
