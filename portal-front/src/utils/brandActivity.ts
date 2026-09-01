// 브랜드 활동 탭
import { apiFetch } from './apiFetch'
import type { SeriesApiResponse, TopicsApiResponse, MatrixApiResponse, SeriesData, TopicsData, MatrixData } from '../types/market'
import { buildActivitySeriesRequest, normalizeCsdMarketScope, type ActivitySeriesRequestOptions } from './brandActivityCsdMarket'
import {
  brandActivityScopeKey,
  buildBrandActivityScopeRequest,
  GENERAL_BRAND_ACTIVITY_SCOPE,
  type BrandActivityScope,
} from './brandActivityScope.ts'
import {
  buildInterestSeriesRequest,
  interestErrorReason,
  type StrategicInterestMarket,
} from './brandActivityInterest'

const nestedFilters = (atc4: string[]) => ({
  atc: { atc4 },
  analysis_level: { iqvia: { audit_code: [] as string[] } },
  channel: { visit_location: [] as string[], specialty: [] as string[] },
})

// "YYYY-MM" → "YYYY-Qn" (월 1~3=Q1 … 10~12=Q4). 형식 안 맞으면 null
function toQuarter(ym: string): string | null {
  const m = ym.match(/^(\d{4})-(\d{2})$/)
  if (!m) return null
  return `${m[1]}-Q${Math.ceil(Number(m[2]) / 3)}`
}


const TTL = 5 * 60 * 1000
type DedupStore<T> = { cache: Map<string, { data: T; at: number }>; inflight: Map<string, Promise<T>> }
const newStore = <T>(): DedupStore<T> => ({ cache: new Map(), inflight: new Map() })

function dedupBy<T>(store: DedupStore<T>, key: string, run: () => Promise<T>): Promise<T> {
  const hit = store.cache.get(key)
  if (hit && Date.now() - hit.at < TTL) return Promise.resolve(hit.data)
  const inf = store.inflight.get(key)
  if (inf) return inf
  const p = run()
    .then(data => {
      if (data != null) store.cache.set(key, { data, at: Date.now() })
      store.inflight.delete(key)
      return data
    })
    .catch(err => { store.inflight.delete(key); throw err })
  store.inflight.set(key, p)
  return p
}

const seriesStore = newStore<SeriesData | null>()
// 종별/진료과(19p) — series는 filters.channel.visit_location/specialty (List<String>). '전체'면 빈배열.
export function fetchSeries(
  selectedBrand: string,
  atc4: string[],
  scope: BrandActivityScope,
  from = '',
  to = '',
  visit = '전체',
  spec = '전체',
  entityLevel: 'brand' | 'company' = 'brand',
): Promise<SeriesData | null> {
  const key = `${selectedBrand}|${atc4.join(',')}|${from}|${to}|${visit}|${spec}|${brandActivityScopeKey(scope)}|${entityLevel}`
  return dedupBy(seriesStore, key, async () => {
    const start = toQuarter(from), end = toQuarter(to)
    const res = await apiFetch('/api/v1/market/brand/time/series', {
      method: 'POST',
      body: JSON.stringify({
        ...buildBrandActivityScopeRequest(scope, {
          ...nestedFilters(atc4),
          channel: {
            visit_location: visit && visit !== '전체' ? [visit] : [],
            specialty: spec && spec !== '전체' ? [spec] : [],
          },
        }),
        selected_brand: selectedBrand,
        ...(entityLevel === 'company' ? { entity_level: 'company' } : {}),
        mode: 'absolute',
        ...(start && end ? { window: { start, end } } : {}),
      }),
    })
    const json = (await res.json()) as SeriesApiResponse
    return json.status === 'SUCCESS' ? json.result.data : null
  })
}

interface ActivityAbsPoint { period: string; value: number }
interface ActivityEntity {
  key: string; display_name?: string; is_selected?: boolean; is_jw?: boolean
  activity?: { absolute?: ActivityAbsPoint[] }
}
interface ActivitySeriesRaw {
  status?: string
  result?: {
    data?: {
      scope?: { view?: string; market_id?: string; market_name?: string; quarters?: string[]; csd_market?: string; csd_markets?: string[] }
      period?: { months?: string[]; quarters?: string[] }
      entities?: ActivityEntity[]
    } | null
  }
}

// activity/series 응답 → 기존 SeriesData 형태로 변환 (CallShareChart·엑셀·날짜슬라이스 무변경 재사용).
//   Share(ratio) = 각 월 브랜드값 / 그 월 전체합 × 100. sales_rank = 최근 월 콜 수 내림차순.
function activityToSeriesData(raw: ActivitySeriesRaw): SeriesData | null {
  const d = raw.result?.data
  if (!d) return null
  const months = d.period?.months ?? []
  const entities = d.entities ?? []
  const csdScope = normalizeCsdMarketScope(d.scope)
  const absMaps = entities.map(e => {
    const m: Record<string, number> = {}
    for (const p of (e.activity?.absolute ?? [])) m[p.period] = p.value
    return m
  })
  const totalByMonth: Record<string, number> = {}
  for (const mth of months) totalByMonth[mth] = entities.reduce((s, _e, i) => s + (absMaps[i][mth] ?? 0), 0)
  const lastM = months[months.length - 1] ?? ''
  const rankOrder = entities.map((_e, i) => i).sort((a, b) => (absMaps[b][lastM] ?? 0) - (absMaps[a][lastM] ?? 0))
  const rankByIdx: Record<number, number> = {}
  rankOrder.forEach((idx, r) => { rankByIdx[idx] = r + 1 })
  const emptyMeasure = () => ({ source: '', absolute: {}, ratio: {} })
  const brands = entities.map((e, i) => {
    const abs = absMaps[i]
    const ratio: Record<string, number> = {}
    for (const mth of months) { const t = totalByMonth[mth]; ratio[mth] = t > 0 ? (abs[mth] ?? 0) / t * 100 : 0 }
    return {
      brand_name: e.display_name || e.key,
      is_jw: !!e.is_jw,
      is_selected: !!e.is_selected,
      sales_rank: rankByIdx[i],
      series: { activity: { source: 'CSD', absolute: abs, ratio }, unit: emptyMeasure(), counting_unit: emptyMeasure(), dosage_unit: emptyMeasure() },
    }
  })
  return {
    scope: {
      view: d.scope?.view ?? GENERAL_BRAND_ACTIVITY_SCOPE.view,
      market_id: d.scope?.market_id ?? '',
      market_name: d.scope?.market_name ?? '',
      selected_brand: { brand_key: '', product_code: '' },
      quarters: d.period?.quarters ?? d.scope?.quarters ?? [],
      activity_months: months,
      measures: ['activity'],
      ...csdScope,
      csd_markets: [...csdScope.csd_markets],
    },
    brands,
  }
}

const activityStore = newStore<SeriesData | null>()
export function fetchActivitySeries(
  selectedBrand: string,
  atc4: string[],
  scope: BrandActivityScope,
  options: ActivitySeriesRequestOptions = {},
): Promise<SeriesData | null> {
  const key = `${selectedBrand}|${atc4.join(',')}|${options.entityLevel ?? 'brand'}|${options.csdChannel ?? 'TOTAL'}|${options.csdMarket ?? 'all'}|${brandActivityScopeKey(scope)}|${options.visit ?? '전체'}|${options.specialty ?? '전체'}`
  return dedupBy(activityStore, key, async () => {
    const res = await apiFetch('/api/v1/market/brand/activity/series', {
      method: 'POST',
      body: JSON.stringify(buildActivitySeriesRequest(selectedBrand, atc4, scope, options)),
    })
    return activityToSeriesData((await res.json()) as ActivitySeriesRaw)
  })
}

export interface InterestBucket { count: number; pct: number }
export interface InterestMonthSlot {
  'VERY USEFUL': InterestBucket
  'SOMEWHAT USEFUL': InterestBucket
  'NOT AT ALL': InterestBucket
  total_count: number
}
export type InterestSeriesMap = Record<string, InterestMonthSlot>
export interface InterestBrandItem {
  brand_key: string; brand_name: string; is_selected?: boolean; is_jw?: boolean; sales_rank?: number
  series: InterestSeriesMap
}
export interface InterestCompanyItem { company_name: string; series: InterestSeriesMap }
export interface InterestSeriesData {
  scope?: { csd_market?: string | null; csd_markets?: string[] }
  levels: string[]
  period: { months: string[] }
  brands: InterestBrandItem[]
  companies?: InterestCompanyItem[]
}
interface InterestApiResponse { status?: string; result?: { data?: InterestSeriesData | null } | null }

const interestStore = newStore<InterestSeriesData | null>()
export function fetchInterestSeries(
  selectedBrand: string,
  market: StrategicInterestMarket,
  visit = '전체',
  spec = '전체',
  csdMarket = 'all',
): Promise<InterestSeriesData | null> {
  const key = `${selectedBrand}|${market.marketId}|${csdMarket}|${visit}|${spec}`
  return dedupBy(interestStore, key, async () => {
    const res = await apiFetch('/api/v1/market/brand/interest/time/series', {
      method: 'POST',
      body: JSON.stringify(buildInterestSeriesRequest({
        selectedBrand,
        market,
        visit,
        specialty: spec,
        csdMarket,
      })),
    })
    if (!res.ok) {
      const reason = interestErrorReason(res.status, await res.text())
      throw new Error(`brand_interest_request_failed:${res.status}:${reason}`)
    }
    const json = (await res.json()) as InterestApiResponse
    if (json.status !== 'SUCCESS') {
      throw new Error(`brand_interest_response_failed:${interestErrorReason(res.status, JSON.stringify(json))}`)
    }
    return json.result?.data ?? null
  })
}

// topics 필터 — 종별(visit_location)/진료과(specialty)/유용성(interest)/처방변화(prescription_evolution). 값은 '전체' 또는 HTML 원값 그대로.
export interface TopicsFilters {
  visit_location?: string
  specialty?: string
  interest?: string
  prescription_evolution?: string
  period_start?: string  
  period_end?: string  
}
const topicsStore = newStore<TopicsData | null>()
export function fetchTopics(selectedBrand: string, atc4: string[], scope: BrandActivityScope, f: TopicsFilters = {}): Promise<TopicsData | null> {
  const visit = f.visit_location ?? '전체'
  const spec = f.specialty ?? '전체'
  const interest = f.interest ?? '전체'
  const evo = f.prescription_evolution ?? '전체'
  const ps = f.period_start ?? ''
  const pe = f.period_end ?? ''
  const key = `${selectedBrand}|${atc4.join(',')}|${brandActivityScopeKey(scope)}|${visit}|${spec}|${interest}|${evo}|${ps}|${pe}`
  return dedupBy(topicsStore, key, async () => {
    const res = await apiFetch('/api/v1/market/brand/topics', {
      method: 'POST',
      body: JSON.stringify({
        ...buildBrandActivityScopeRequest(scope, nestedFilters(atc4)),
        selected_brand: selectedBrand,
        visit_location: visit, specialty: spec, // 최상위 필드(확정)
        ...(interest !== '전체' ? { interest } : {}),
        ...(evo !== '전체' ? { prescription_evolution: evo } : {}),
        ...(ps && pe ? { period_start: ps, period_end: pe } : {}),
        top_n: 7,
      }),
    })
    if (!res.ok) {
      throw new Error(`brand_topics_request_failed:${res.status}`)
    }
    const json = (await res.json()) as TopicsApiResponse
    if (json.status !== 'SUCCESS') {
      throw new Error('brand_topics_response_failed')
    }
    return json.result.data
  })
}

//   응답은 result.code 없음 → status:'SUCCESS' + result.csd_present로 판정. null=판정 보류(로딩/실패, 활성 유지)
interface PresenceResponse {
  result?: { csd_present?: boolean } | null
  status?: string
}
export async function fetchBrandPresence(brand: string): Promise<boolean | null> {
  try {
    const res = await apiFetch('/api/v1/market/brand/presence', {
      method: 'POST',
      body: JSON.stringify({ brand }),
    })
    const json = (await res.json()) as PresenceResponse
    if (json.status !== 'SUCCESS') return null
    return typeof json.result?.csd_present === 'boolean' ? json.result.csd_present : null
  } catch {
    return null
  }
}

const matrixStore = newStore<MatrixData | null>()
export function fetchMatrix(selectedBrand: string, atc4: string[], scope: BrandActivityScope, _from = '', _to = ''): Promise<MatrixData | null> {
  const key = `${selectedBrand}|${atc4.join(',')}|${brandActivityScopeKey(scope)}`
  return dedupBy(matrixStore, key, async () => {
    const res = await apiFetch('/api/v1/market/brand/matrix', {
      method: 'POST',
      body: JSON.stringify({
        ...buildBrandActivityScopeRequest(scope, nestedFilters(atc4)),
        selected_brand: selectedBrand,
        visit_location: '전체', specialty: '전체',
      }),
    })
    const json = (await res.json()) as MatrixApiResponse
    return json.status === 'SUCCESS' ? json.result.data : null
  })
}
