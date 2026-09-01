export type MarketSource = 'UBIST' | 'IQVIA'
export type SourceAvailabilityStatus = 'available' | 'stale' | 'unavailable'

export type SourceAvailability = Record<MarketSource, SourceAvailabilityStatus>

export type MarketBrandSourceEntry = {
  readonly brand?: string
  readonly general_sources?: readonly string[]
  readonly strategic_sources?: readonly string[]
  readonly sources?: readonly string[]
  readonly observed_source_periods?: Readonly<Record<string, string>>
  readonly [key: string]: unknown
}

export function brandObservedSourcePeriods(brand: string): Readonly<Record<string, string>> {
  try {
    const raw = sessionStorage.getItem('marketBrandsResult')
    const entries = raw ? JSON.parse(raw) as MarketBrandSourceEntry[] : []
    return entries.find(entry => entry.brand === brand)?.observed_source_periods ?? {}
  } catch {
    return {}
  }
}

export function mergeMarketBrandResults(
  cached: readonly MarketBrandSourceEntry[],
  refreshed: readonly MarketBrandSourceEntry[],
): MarketBrandSourceEntry[] {
  const refreshedBrands = new Set(refreshed.map(item => item.brand).filter(Boolean))
  return [...cached.filter(item => !refreshedBrands.has(item.brand)), ...refreshed]
}

export function resolveSourceAvailability(
  catalogSources: Set<MarketSource> | null,
  responseCombos: readonly string[] = [],
): SourceAvailability {
  const statusFor = (source: MarketSource): SourceAvailabilityStatus => {
    if (catalogSources?.has(source)) return 'available'
    const responseHasSource = responseCombos.some(combo => combo.startsWith(`${source}.`))
    if (catalogSources && responseHasSource) return 'stale'
    if (!catalogSources && responseHasSource) return 'available'
    return 'unavailable'
  }

  return {
    UBIST: statusFor('UBIST'),
    IQVIA: statusFor('IQVIA'),
  }
}

export function isSourceSelectable(status: SourceAvailabilityStatus): boolean {
  return status !== 'unavailable'
}

export function sourceAvailabilityTitle(
  source: MarketSource,
  status: SourceAvailabilityStatus,
  selectedBrandLatestPeriod?: string | null,
): string | undefined {
  if (status === 'available') return undefined
  if (status === 'stale') {
    return selectedBrandLatestPeriod
      ? `${selectedBrandLatestPeriod} 이후 브랜드 데이터 없음`
      : `${source}는 마지막 관측월 이후 브랜드 데이터가 없습니다`
  }
  return `이 브랜드는 ${source} 데이터를 제공하지 않습니다`
}

export function shouldApplySupportedSourcesFromMeta(
  navAssaySources: Set<MarketSource> | null,
  cachedAssaySources: Set<MarketSource> | null,
  legacyNavSources: Set<MarketSource> | null,
): boolean {
  return !navAssaySources && !cachedAssaySources && !legacyNavSources
}
