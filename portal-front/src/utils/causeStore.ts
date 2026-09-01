// ============ 원인분석(/dynamic) 모듈 레벨 캐시 ============
import type { CauseApiResponse, CauseData, DynamicFilterContext } from '../types/market'
import {
  DEFAULT_FILTER_CONTEXT,
  buildFilterKey,
  fetchDynamicResult,
} from './dynamicMarket'

type CauseResult = CauseApiResponse['result']

const TTL = 10 * 60 * 1000

const store = new Map<string, { result: CauseResult; savedAt: number }>()
const inflight = new Map<string, Promise<CauseResult | null>>()

export function causeKey(
  brand: string,
  source: string,
  view: string,
  measure: string,
  filterKey = buildFilterKey(DEFAULT_FILTER_CONTEXT),
): string {
  return `${brand}_${source}_${view}_${measure}_${filterKey}`
}

export function getCachedResult(key: string): CauseResult | undefined {
  const e = store.get(key)
  if (!e) return undefined
  if (Date.now() - e.savedAt >= TTL) { store.delete(key); return undefined }
  return e.result
}

function dynamicToCauseResult(dyn: NonNullable<Awaited<ReturnType<typeof fetchDynamicResult>>>): CauseResult {
  return {
    brand: dyn.brand ?? dyn.brand_name ?? '',
    brand_name: dyn.brand_name,
    brand_key: dyn.brand_key,
    source: dyn.source as 'UBIST' | 'IQVIA' | undefined,
    market_meta: dyn.market_meta ?? null,
    data: dyn.data,
    reason: dyn.reason,
    markets: dyn.markets,
  }
}

export function fetchCauseResult(
  brand: string,
  source: string,
  view: string,
  measure: string,
  filters: DynamicFilterContext = DEFAULT_FILTER_CONTEXT,
): Promise<CauseResult | null> {
  const filterKey = buildFilterKey(filters)
  const reqKey = causeKey(brand, source, view, measure, filterKey)
  const cached = getCachedResult(reqKey)
  if (cached) return Promise.resolve(cached)

  const existing = inflight.get(reqKey)
  if (existing) return existing

  const p = fetchDynamicResult(
    brand,
    source as 'UBIST' | 'IQVIA',
    view,
    measure,
    filters,
  )
    .then(dyn => {
      if (!dyn) return null
      const result = dynamicToCauseResult(dyn)
      const savedAt = Date.now()
      store.set(reqKey, { result, savedAt })
      const actual = result.source === 'UBIST' || result.source === 'IQVIA' ? result.source : source
      if (actual !== source) {
        store.set(causeKey(brand, actual, view, measure, filterKey), { result, savedAt })
      }
      return result
    })
    .finally(() => inflight.delete(reqKey))

  inflight.set(reqKey, p)
  return p
}

export function snapshotBrandData(brand: string): Record<string, CauseData | null> {
  const out: Record<string, CauseData | null> = {}
  const now = Date.now()
  for (const [k, e] of store) {
    if (now - e.savedAt >= TTL) { store.delete(k); continue }
    if (k.startsWith(`${brand}_`)) out[k] = e.result.data
  }
  return out
}

export function findBrandSalesResult(brand: string, view: string): CauseResult | undefined {
  return getCachedResult(causeKey(brand, 'UBIST', view, 'sales'))
    ?? getCachedResult(causeKey(brand, 'IQVIA', view, 'sales'))
}

export { DEFAULT_FILTER_CONTEXT, buildFilterKey }
