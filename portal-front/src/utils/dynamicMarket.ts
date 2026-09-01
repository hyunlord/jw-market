// /api/v1/market/dynamic + 필터 옵션 API — AnalyzePage ATC 필터 영역
import { apiFetch } from './apiFetch'
import {
  parseFilterOptionsFetchResult,
  type FilterOptionsFetchResult,
} from './filterOptionsResult'
import type {
  AnalysisResult,
  DynamicApiResponse,
  DynamicCauseResult,
  DynamicFilterContext,
  DynamicRequestBody,
  FilterDimension,
  DimensionHierarchy,
  FilterOptionValue,
  FilterOptionsResponse,
  AtcOptionItem,
  AtcOptionsTree,
} from '../types/market'
import {
  buildDeepAnalysisRequest,
  parseDeepAnalysisError,
  type DeepAnalysisFormalContext,
  type DeepAnalysisRequestError,
} from './deepAnalysisRequest'
import {
  analysisLevelForHierarchyRequest,
  applyHierarchySelectionDefaults,
  reconcileHierarchySelections,
} from './moleculeStrengthHierarchy'
import { resolveAtcNavigationTree } from './atcNavigationTree'
import { mergeMarketBrandResults, type MarketBrandSourceEntry } from './sourceAvailability'
import { parseDynamicMarketError } from './dynamicMarketError'

/** JW Strategic → strategic / Market Standard → general (/analysis view 파라미터) */
export type DeepAnalysisView = 'strategic' | 'general'

export type DeepAnalysisFetchResult =
  | { readonly ok: true; readonly data: AnalysisResult }
  | { readonly ok: false; readonly error: DeepAnalysisRequestError }

export function assayToDeepView(assay: string): DeepAnalysisView {
  return assay === 'market' ? 'general' : 'strategic'
}

export function isJwBrand(brand: string): boolean | null {
  try {
    const raw = sessionStorage.getItem('marketBrandsResult')
    if (!raw) return null
    const arr = JSON.parse(raw) as { brand?: string }[]
    if (!Array.isArray(arr) || arr.length === 0) return null
    return arr.some(x => x.brand === brand)
  } catch {
    return null
  }
}

export async function refreshMarketBrands(brand = ''): Promise<void> {
  try {
    const res = await apiFetch('/api/v1/market/brands', {
      method: 'POST',
      body: JSON.stringify({ query: brand, marketId: '' }),
    })
    const d = (await res.json()) as { status?: string; result?: unknown }
    if (d.status === 'SUCCESS' && Array.isArray(d.result)) {
      const refreshed = d.result as MarketBrandSourceEntry[]
      if (!brand) {
        sessionStorage.setItem('marketBrandsResult', JSON.stringify(refreshed))
        return
      }
      let cached: MarketBrandSourceEntry[] = []
      try {
        const parsed = JSON.parse(sessionStorage.getItem('marketBrandsResult') ?? '[]')
        if (Array.isArray(parsed)) cached = parsed
      } catch { /* stale cache is replaced by the exact brand result */ }
      sessionStorage.setItem('marketBrandsResult', JSON.stringify(mergeMarketBrandResults(cached, refreshed)))
    }
  } catch { /* noop — 기존 캐시 유지 */ }
}

let marketStatusInflight: Promise<void> | null = null
export async function ensureMarketStatusResult(): Promise<void> {
  if (sessionStorage.getItem('marketStatusResult')) return
  if (marketStatusInflight) return marketStatusInflight
  marketStatusInflight = (async () => {
    try {
      const res = await apiFetch('/api/v1/market/status', {
        method: 'POST',
        body: JSON.stringify({ marketId: '' }),
      })
      const d = (await res.json()) as { status?: string; result?: unknown }
      if (d.status === 'SUCCESS' && d.result) {
        sessionStorage.setItem('marketStatusResult', JSON.stringify(d.result))
      }
    } catch { /* noop — 제품군만 빈 채로(치명 아님) */ }
    finally { marketStatusInflight = null }
  })()
  return marketStatusInflight
}

export function brandSourcesForAssay(brand: string, assay: 'jw' | 'market'): Set<'UBIST' | 'IQVIA'> | null {
  try {
    const raw = sessionStorage.getItem('marketBrandsResult')
    if (!raw) return null
    const arr = JSON.parse(raw) as { brand?: string; general_sources?: string[]; strategic_sources?: string[]; sources?: string[] }[]
    const b = Array.isArray(arr) ? arr.find(x => x.brand === brand) : undefined
    if (!b) return null
    const list = assay === 'jw' ? b.strategic_sources : b.general_sources
    const eff = (list ?? b.sources ?? []).filter((s): s is 'UBIST' | 'IQVIA' => s === 'UBIST' || s === 'IQVIA')
    return eff.length ? new Set(eff) : null
  } catch {
    return null
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function isAnalysisResult(value: unknown): value is AnalysisResult {
  if (!isRecord(value) || !isRecord(value.data)) return false
  return isRecord(value.data.forecast)
    && isRecord(value.data.simulation)
    && Array.isArray(value.data.events)
}

function isAnalysisResponse(value: unknown): value is { status: 'SUCCESS'; result: AnalysisResult } {
  return isRecord(value)
    && value.status === 'SUCCESS'
    && isAnalysisResult(value.result)
}

export async function fetchDeepAnalysis(
  brand: string,
  view: DeepAnalysisView,
  context?: DeepAnalysisFormalContext,
): Promise<DeepAnalysisFetchResult> {
  if (!brand) {
    return {
      ok: false,
      error: parseDeepAnalysisError(400, {
        error: 'brand_required',
        message: '분석할 브랜드를 확인할 수 없습니다.',
      }),
    }
  }
  try {
    const res = await apiFetch('/api/v1/market/analysis', {
      method: 'POST',
      body: JSON.stringify(buildDeepAnalysisRequest(brand, view, context)),
    })
    let payload: unknown
    try {
      payload = await res.json()
    } catch {
      payload = { message: `분석 요청이 HTTP ${res.status}로 실패했습니다.` }
    }
    if (res.ok && isAnalysisResponse(payload)) {
      return { ok: true, data: payload.result }
    }
    return { ok: false, error: parseDeepAnalysisError(res.status, payload) }
  } catch {
    return {
      ok: false,
      error: parseDeepAnalysisError(0, {
        error: 'network_error',
        message: '분석 데이터를 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.',
      }),
    }
  }
}

export const UBIST_FILTER_DIMENSIONS = [
  { key: 'seller', label: '판매사' },
  { key: 'molecule', label: '성분' },             
  { key: 'molecule_strength', label: '성분용량' }, 
  { key: 'form', label: '제형' },
  { key: 'route', label: '투여 경로' },
  { key: 'reimbursement', label: '급여 구분' },
  { key: 'specialty', label: '진료과' },
  { key: 'facility', label: '종별' },
] as const

export const IQVIA_FILTER_DIMENSIONS = [
  { key: 'mfr_name_kor', label: '제조사' },
  { key: 'molecule_type', label: '성분구분' },
  { key: 'molecule_desc', label: '성분명' },
  { key: 'pack_desc', label: 'PACK DESC' },
  { key: 'strength', label: '함량' },
  { key: 'nhi_type', label: 'NHI 구분' },
  { key: 'audit_code', label: 'Audit Code' },
] as const

export type UbistFilterKey = (typeof UBIST_FILTER_DIMENSIONS)[number]['key']
export type IqviaFilterKey = (typeof IQVIA_FILTER_DIMENSIONS)[number]['key']

export const DEFAULT_FILTER_CONTEXT: DynamicFilterContext = {
  assayMode: 'jw',
  atc4: [],
  analysisLevel: {},
}

export function buildFilterKey(ctx: DynamicFilterContext): string {
  const sorted = Object.keys(ctx.analysisLevel).sort().map(k =>
    `${k}:${[...ctx.analysisLevel[k]!].sort().join('|')}`
  ).join(';')
  return `${ctx.assayMode}|${ctx.atc4.slice().sort().join(',')}|${sorted}`
}

function sourceToApi(source: 'UBIST' | 'IQVIA'): string {
  return source === 'IQVIA' ? 'iqvia' : 'ubist'
}

function viewToKind(view: string): string {
  return view === 'competitive_dynamics' ? 'competitive_dynamics' : 'market_landscape'
}

export function buildDynamicRequest(params: {
  brand: string
  source: 'UBIST' | 'IQVIA'
  measure: string
  view: string
  filters: DynamicFilterContext
}): DynamicRequestBody {
  const { brand, source, measure, view, filters } = params
  // ⚠️ options.top_n 보내면 jwai extra_forbidden 422 — options는 period_range 필요할 때만
  const body: DynamicRequestBody = {
    source: sourceToApi(source),
    measure,
    filters: {},
  }

  if (filters.assayMode === 'jw') {
    body.filters = {
      focus_brand_key: brand,
      view_kind: viewToKind(view),
      // 전략뷰: 빈 배열 = 시장 전체 select-all (생략해도 동일)
      ...(filters.atc4.length > 0 ? { atc4: filters.atc4 } : {}),
    }
  } else {
    // Market Standard (일반뷰) — atc4 또는 molecule 필수
    // 빈 atc4[] 보내면 400 "at least one ATC4 or molecule filter is required"
    // → atc4 있으면 그대로, 없으면 focus_brand_key로 브랜드 ATC4 합집합 (dynamic.md)
    const srcKey = source === 'IQVIA' ? 'iqvia' : 'ubist'
    const level: Record<string, string[]> = {}
    for (const [k, v] of Object.entries(filters.analysisLevel)) {
      if (v.length > 0) level[k] = v
    }
    const requestLevel = analysisLevelForHierarchyRequest(source, level)
    const analysisLevel = Object.keys(requestLevel).length > 0
      ? { analysis_level: { [srcKey]: requestLevel } }
      : {}
    body.filters = filters.atc4.length > 0
      ? { atc4: filters.atc4, ...analysisLevel }
      : { focus_brand_key: brand, ...analysisLevel }
  }
  return body
}

export function parseDynamicResponse(json: DynamicApiResponse): DynamicCauseResult | null {
  if (json.status !== 'SUCCESS') return null
  const inner = json.result?.result
  if (!inner || json.result?.status !== 'SUCCESS') return null
  return inner
}

export async function fetchDynamicResult(
  brand: string,
  source: 'UBIST' | 'IQVIA',
  view: string,
  measure: string,
  filters: DynamicFilterContext,
): Promise<DynamicCauseResult | null> {
  const body = buildDynamicRequest({ brand, source, measure, view, filters })
  const res = await apiFetch('/api/v1/market/dynamic', {
    method: 'POST',
    body: JSON.stringify(body),
  })
  let payload: unknown
  try {
    payload = await res.json()
  } catch {
    payload = {}
  }
  if (!res.ok) throw parseDynamicMarketError(res.status, payload)
  const json = payload as DynamicApiResponse
  if (json.status !== 'SUCCESS') throw parseDynamicMarketError(res.status, payload)
  return parseDynamicResponse(json)
}

// 포탈 POST /api/v1/market/dynamic/filter/options (b73b380에서 정상화 — 2026-07-13 실측 200)
//   응답 envelope { status, result: FilterOptionsResponse }. (이전 임시로 고객사 GET /dynamic-market/filter-options 우회하던 걸 포탈로 복귀)
const foCache = new Map<string, { data: FilterOptionsResponse; at: number }>()
const foInflight = new Map<string, Promise<FilterOptionsFetchResult>>()
const FO_TTL = 5 * 60 * 1000
const fullAtcTreeCache = new Map<string, AtcOptionsTree>()
const fullAtcTreeInflight = new Map<string, Promise<AtcOptionsTree | null>>()

export function fetchFullAtcTree(params: {
  view: 'general' | 'strategic'
  source: 'UBIST' | 'IQVIA'
  measure: string
  brand: string
}): Promise<AtcOptionsTree | null> {
  const key = [params.view, params.source, params.measure, params.brand].join('|')
  const cached = fullAtcTreeCache.get(key)
  if (cached) return Promise.resolve(cached)
  const inflight = fullAtcTreeInflight.get(key)
  if (inflight) return inflight

  const request = (async (): Promise<AtcOptionsTree | null> => {
    const result = await fetchFilterOptions({
      view: params.view,
      source: params.source,
      measure: params.measure,
      brand: params.brand,
      atc4Codes: [],
    })
    if (!result.ok) return null
    const tree = result.data.atc
    if (!tree || !Object.values(tree).some(value => Array.isArray(value) && value.length > 0)) {
      return null
    }
    fullAtcTreeCache.set(key, tree)
    return tree
  })().finally(() => fullAtcTreeInflight.delete(key))

  fullAtcTreeInflight.set(key, request)
  return request
}

export async function fetchBrandDefaultScope(params: {
  view: 'general' | 'strategic'
  source: 'UBIST' | 'IQVIA'
  measure: string
  brand: string
}): Promise<string[]> {
  try {
    const response = await apiFetch('/api/v1/market/dynamic/brand/default-scope', {
      method: 'POST',
      body: JSON.stringify({
        view: params.view,
        source: sourceToApi(params.source),
        measure: params.measure === 'sales' ? 'sales' : 'qty',
        brand: params.brand,
      }),
    })
    if (!response.ok) return []
    const json = (await response.json()) as {
      status?: string
      result?: { atc4_codes?: unknown }
    }
    const codes = json.status === 'SUCCESS' ? json.result?.atc4_codes : undefined
    return Array.isArray(codes)
      ? [...new Set(codes.map(String).map(code => code.trim()).filter(Boolean))]
      : []
  } catch {
    return []
  }
}

export function fetchFilterOptions(params: {
  view: 'general' | 'strategic'
  source: 'UBIST' | 'IQVIA'
  measure: string
  brand: string
  atc4Codes?: string[]
  selections?: Record<string, string[]>
}): Promise<FilterOptionsFetchResult> {
  const measureQ = params.measure === 'sales' ? 'sales' : 'qty'
  const atc4 = (params.atc4Codes ?? []).filter(Boolean)
  const selections = Object.fromEntries(
    Object.entries(params.selections ?? {})
      .filter(([, values]) => values.length > 0)
      .sort(([left], [right]) => left.localeCompare(right))
      .map(([dimension, values]) => [dimension, [...values].sort()]),
  )
  const selectionsJson = JSON.stringify(selections)
  const key = [
    params.view,
    sourceToApi(params.source),
    measureQ,
    params.brand,
    atc4.join(','),
    selectionsJson,
  ].join('|')

  const hit = foCache.get(key)
  if (hit && Date.now() - hit.at < FO_TTL) {
    return Promise.resolve({ ok: true, data: hit.data })
  }
  const inf = foInflight.get(key)
  if (inf) return inf

  const p = (async (): Promise<FilterOptionsFetchResult> => {
    try {
      const res = await apiFetch('/api/v1/market/dynamic/filter/options', {
        method: 'POST',
        body: JSON.stringify({
          view: params.view,
          source: sourceToApi(params.source),
          measure: measureQ,
          brand: params.brand,
          selections: JSON.stringify(params.selections ?? {}),
          ...(atc4.length ? { atc4_codes: atc4 } : {}),
        }),
      })
      let json: unknown = null
      try {
        json = await res.json()
      } catch {
        // The status still distinguishes HTTP failures from malformed success payloads.
      }
      return parseFilterOptionsFetchResult(res.ok, res.status, json)
    } catch {
      return { ok: false, reason: 'network' }
    }
  })()
    .then(result => {
      if (result.ok) foCache.set(key, { data: result.data, at: Date.now() })
      foInflight.delete(key)
      return result
    })
    .catch((): FilterOptionsFetchResult => {
      foInflight.delete(key)
      return { ok: false, reason: 'network' }
    })

  foInflight.set(key, p)
  return p
}

export function dimensionsForSource(source: 'UBIST' | 'IQVIA') {
  return source === 'IQVIA' ? IQVIA_FILTER_DIMENSIONS : UBIST_FILTER_DIMENSIONS
}

export function dimensionValues(
  dimensions: FilterDimension[] | undefined,
  dimensionType: string,
): FilterOptionValue[] {
  return dimensions?.find(d => d.dimension_type === dimensionType)?.values ?? []
}

export function displayDimensions(
  source: 'UBIST' | 'IQVIA',
  dimensions: FilterDimension[],
): { key: string; label: string }[] {
  return dimensionsForSource(source)
    .filter(d => dimensionValues(dimensions, d.key).length > 0)
    .map(d => ({ key: d.key, label: d.label }))
}

export function emptyAnalysisLevel(source: 'UBIST' | 'IQVIA'): Record<string, string[]> {
  const keys = dimensionsForSource(source).map(d => d.key)
  return Object.fromEntries(keys.map(k => [k, []]))
}

/** Filters 팝업 디폴트 = 각 차원 전체 선택 (빈 배열은 '미선택') */
export function fullAnalysisLevel(
  source: 'UBIST' | 'IQVIA',
  dimensions: FilterDimension[],
  dimensionHierarchies: readonly DimensionHierarchy[] = [],
): Record<string, string[]> {
  const keys = dimensionsForSource(source).map(d => d.key)
  const full = Object.fromEntries(keys.map(k => [
    k,
    dimensionValues(dimensions, k).map(v => v.key),
  ]))
  return applyHierarchySelectionDefaults(full, dimensionHierarchies)
}

export function reconcileAnalysisLevel(
  prev: Record<string, string[]>,
  source: 'UBIST' | 'IQVIA',
  dimensions: FilterDimension[],
  dimensionHierarchies: readonly DimensionHierarchy[] = [],
): Record<string, string[]> {
  const full = fullAnalysisLevel(source, dimensions, dimensionHierarchies)
  if (!Object.values(prev).some(v => v.length > 0)) return full

  const next: Record<string, string[]> = {}
  for (const d of dimensionsForSource(source)) {
    const avail = dimensionValues(dimensions, d.key).map(v => v.key)
    const availSet = new Set(avail)
    const sel = prev[d.key] ?? []
    if (sel.length === 0) {
      next[d.key] = []
      continue
    }
    const pruned = sel.filter(v => availSet.has(v))
    if (sel.length > avail.length) {
      next[d.key] = [...avail]
    } else {
      next[d.key] = pruned
    }
  }
  return reconcileHierarchySelections(next, dimensionHierarchies)
}

export function mergeFilterOptions(
  api: FilterOptionsResponse | null,
  fallbackAtc4: string[],
  fullAtcTree?: AtcOptionsTree,
): {
  atcTree: AtcOptionsTree
  dimensions: FilterDimension[]
  dimensionHierarchies: DimensionHierarchy[]
} {
  const scopedAtcTree = api?.atc ?? {
    atc4: fallbackAtc4.map(code => ({
      key: code,
      value: code,
      label: code,
      level: 'atc4',
      default: true,
      selected: true,
    })),
  }
  const atcTree = fullAtcTree
    ? resolveAtcNavigationTree(fullAtcTree, scopedAtcTree)
    : scopedAtcTree
  // dimensions + channel_axis(facility/specialty/audit_code) 합침 — 스웨거 응답이 분리되어 옴
  const dimensions = [...(api?.dimensions ?? [])]
  const has = (t: string) => dimensions.some(d => d.dimension_type === t)
  const ubist = api?.channel_axis?.ubist
  const iqvia = api?.channel_axis?.iqvia
  if (ubist?.facility?.length && !has('facility')) {
    dimensions.push({ dimension_type: 'facility', label: '종별', values: ubist.facility })
  }
  if (ubist?.specialty?.length && !has('specialty')) {
    dimensions.push({ dimension_type: 'specialty', label: '진료과', values: ubist.specialty })
  }
  if (iqvia?.audit_code?.length && !has('audit_code')) {
    dimensions.push({ dimension_type: 'audit_code', label: 'Audit Code', values: iqvia.audit_code })
  }
  return {
    atcTree,
    dimensions,
    dimensionHierarchies: api?.dimension_hierarchies ?? [],
  }
}

export function selectedAtc4FromLevels(levels: {
  atc1: string[]
  atc2: string[]
  atc3: string[]
  atc4: string[]
}): string[] {
  if (levels.atc4.length > 0) return levels.atc4
  return []
}

export function cascadeAtcOptions(
  tree: AtcOptionsTree,
  level: 'atc1' | 'atc2' | 'atc3' | 'atc4',
  selectedParents: { atc1: string[]; atc2: string[]; atc3: string[] },
): AtcOptionItem[] {
  const all = tree[level] ?? []
  if (level === 'atc1') return all
  if (level === 'atc2') {
    if (selectedParents.atc1.length === 0) return all
    return all.filter(i => !i.parent || selectedParents.atc1.includes(i.parent))
  }
  if (level === 'atc3') {
    if (selectedParents.atc2.length === 0) return all
    return all.filter(i => !i.parent || selectedParents.atc2.includes(i.parent))
  }
  if (selectedParents.atc3.length === 0) return all
  return all.filter(i => !i.parent || selectedParents.atc3.includes(i.parent))
}
