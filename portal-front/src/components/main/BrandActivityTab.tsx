// 원인분석 > 브랜드 활동 탭 — 6개 섹션

import { useEffect, useMemo, useState } from 'react'
import { Line, PolarArea, Bubble, Bar } from 'react-chartjs-2'
import type { ChartData } from 'chart.js'
import type { TopicsData, SeriesData, MatrixData } from '../../types/market'
import { fetchTopics, fetchSeries, fetchActivitySeries, fetchMatrix, fetchInterestSeries, type TopicsFilters, type InterestSeriesData, type InterestSeriesMap, type InterestMonthSlot, type InterestBucket } from '../../utils/brandActivity'
import { apiFetch } from '../../utils/apiFetch'
import { buildCsdMarketOptions, resolveTopicMonths, type TopicPeriodBounds } from '../../utils/brandActivityCsdMarket'
import { brandActivityScopeKey, buildBrandActivityScopeRequest, resolveBrandActivityScope, type BrandActivityScope } from '../../utils/brandActivityScope.ts'
import { selectInterestDisplayItem, type StrategicInterestMarket } from '../../utils/brandActivityInterest'
import { exportActivityUnit, exportCallSeries, exportInterestSeries, exportKeywordCrossTopics, exportKeywordTopics, type KeywordExportMode } from '../../utils/brandActivityExcel'
import { fetchKeywordCrossDatasets, keywordCrossDomain } from '../../utils/brandActivityKeywordCross.ts'
import {
  commonOpts, legendBottom, legendHoverPointer,
  TARGET_COLOR, COMPETITOR_PALETTE, fmtPeriodKor, opaqueLabelsForArc,
} from '../../utils/chartHelpers'
import { TooltipBody } from '../../utils/chartTooltips'
import { BRAND_ACTIVITY_TOOLTIPS } from '../../utils/tooltipCopy'
import KeywordShareGrid from './KeywordShareGrid'
import ChartSkeleton from './ChartSkeleton'
import SelectBox from '../ui/SelectBox'
import DateRangePicker from './DateRangePicker'
import SlideToggle from './SlideToggle'
import type { SelectOption } from '../../types/market'

// 브랜드별 색 — 자사/선택 브랜드는 TARGET_COLOR, 나머지는 경쟁 팔레트 순환
function brandColors<T extends { is_jw: boolean; is_selected: boolean }>(brands: T[]): string[] {
  let ci = 0
  return brands.map(b =>
    (b.is_jw || b.is_selected) ? TARGET_COLOR : COMPETITOR_PALETTE[ci++ % COMPETITOR_PALETTE.length])
}

const centerTitle: React.CSSProperties = {
  display: 'flex', justifyContent: 'center', alignItems: 'center', gap: 6,
  fontSize: 16, fontWeight: 600, marginBottom: 8,
}

// 컨트롤 셀렉트 옵션 
const RANK_OPTS: SelectOption[] = [{ value: 'brand', label: '브랜드' }, { value: 'company', label: '회사' }]
const CHANNEL_OPTS: SelectOption[] = [
  { value: 'all', label: '채널 전체' }, { value: 'GH', label: 'GH' }, { value: 'SHPPI', label: 'SHPPI' }, { value: 'CPPI', label: 'CPPI' },
]
const VISIT_LOCATION_VALUES = ['HOSPITAL', 'PRIV. PRACTICE'] as const
const visitLocationOptions = (allLabel: string): SelectOption[] => [
  { value: 'all', label: allLabel },
  ...VISIT_LOCATION_VALUES.map(value => ({ value, label: value })),
]
const INT_VISIT_OPTS = visitLocationOptions('종별 전체')
const INT_COLORS = { VERY: '#74C5A9', SOMEWHAT: '#FFBC5F', NOT: '#E98773' } as const
const CATEGORY_OPTS = visitLocationOptions('전체 종별')
const SPECIALTY_OPTS: SelectOption[] = [
  { value: 'all', label: '전체 진료과' },
  ...['CLINICS / GPs only', 'Cardio', 'Dermato', 'E.N.T.', 'Endo', 'Gastro', 'General Surgeons',
    'IM/FM', 'Nephro', 'Neuro', 'Neuro Surgeons', 'OB/GY', 'Ophtalmo', 'Ortho Surgeons',
    'Pediatricians', 'Psy', 'Resp', 'Rheumato (Hosp.)', 'Urologists'].map(s => ({ value: s, label: s })),
]
const INTEREST_OPTS: SelectOption[] = [
  { value: 'all', label: '전체' },
  ...['NOT AT ALL', 'SOMEWHAT USEFUL', 'VERY USEFUL'].map(s => ({ value: s, label: s })),
]
const EVOLUTION_OPTS: SelectOption[] = [
  { value: 'all', label: '전체' },
  ...['decrease', 'increase (or will begin to prescribe)', 'remain unchanged'].map(s => ({ value: s, label: s })),
]
const KEYWORD_CROSS_MODES = [
  { value: 'interest', label: 'INTEREST' },
  { value: 'evolution', label: 'Prescription Evolution' },
] as const
type KeywordCrossMode = (typeof KEYWORD_CROSS_MODES)[number]['value']
// 차트 컨트롤용 sm 셀렉트 (AnalyzePage ChartSelect와 동일 변형)
function CtlSelect(props: { options: SelectOption[]; value: string; onChange: (v: string) => void }) {
  return <div className="control-box control-box-select"><SelectBox {...props} size="sm" weight={400} /></div>
}

const Q_START_MM: Record<string, string> = { '1': '01', '2': '04', '3': '07', '4': '10' }
function axisToYM(v: string): string {
  const q = /^(\d{4})-Q([1-4])$/.exec(v)
  return q ? `${q[1]}-${Q_START_MM[q[2]]}` : v
}

type DateSec = {
  from: string; to: string
  setFrom: (v: string) => void; setTo: (v: string) => void
  apply: () => void; reset: () => void
  minYM?: string; maxYM?: string
}

const PER_YEAR = { monthly: 12, quarterly: 4 } as const

// 날짜 조정 최대 3년 제한
function threeYearMin(dataMin: string, dataMax: string): string {
  const m = /^(\d{4})-(\d{2})$/.exec(dataMax)
  if (!m) return dataMin
  const cap = `${Number(m[1]) - 3}-${m[2]}`
  return dataMin && dataMin > cap ? dataMin : cap
}

function sliceSeriesByWindow(data: SeriesData | null, rangeMode: 'monthly' | 'quarterly', fromYM: string, toYM: string): SeriesData | null {
  if (!data) return null
  if (!fromYM || !toYM) return data
  const inRange = (v: string) => { const ym = axisToYM(v); return ym >= fromYM && ym <= toYM }
  if (rangeMode === 'quarterly') {
    return { ...data, scope: { ...data.scope, quarters: (data.scope?.quarters ?? []).filter(inRange) } }
  }
  return { ...data, scope: { ...data.scope, activity_months: (data.scope?.activity_months ?? []).filter(inRange) } }
}

// 셀렉트 값 'all'(전체) → API sentinel '전체'
const mapAll = (v: string): string => (v === 'all' ? '전체' : v)

type SeriesSectionOptions = {
  readonly activityFilters?: { readonly visit: string; readonly specialty: string }
  readonly entityLevel?: 'brand' | 'company'
  readonly activityChannel?: string
  readonly csdMarket?: string
}

function useSeriesSection(productName: string, atcKey: string, marketScope: BrandActivityScope, rangeMode: 'monthly' | 'quarterly', options: SeriesSectionOptions = {}): DateSec & { data: SeriesData | null; fullMonths: string[] } {
  const requestScopeKey = brandActivityScopeKey(marketScope)
  const [rawData, setRawData] = useState<SeriesData | null>(null)
  const [from, setFrom] = useState('')     // 피커 입력
  const [to, setTo] = useState('')
  const [winFrom, setWinFrom] = useState('')  // 적용된 슬라이스 창
  const [winTo, setWinTo] = useState('')
  const [synced, setSynced] = useState(false)
  const visit = options.activityFilters?.visit ?? '전체'
  const specialty = options.activityFilters?.specialty ?? '전체'
  const entityLevel = options.entityLevel ?? 'brand'
  const activityChannel = options.activityChannel
  const csdMarket = options.csdMarket

  useEffect(() => {
    if (!atcKey) return
    let alive = true
    const req = activityChannel !== undefined
      ? fetchActivitySeries(productName, atcKey.split(','), marketScope, { entityLevel, csdChannel: activityChannel, csdMarket, visit, specialty })
      : fetchSeries(productName, atcKey.split(','), marketScope, '', '', visit, specialty, entityLevel)  // ⑥ 처방량+활동량
    req.then(d => { if (alive) setRawData(d) }).catch(() => {})
    return () => { alive = false }
  }, [productName, atcKey, marketScope, requestScopeKey, visit, specialty, entityLevel, activityChannel, csdMarket])

  const axis = rangeMode === 'quarterly' ? (rawData?.scope?.quarters ?? []) : (rawData?.scope?.activity_months ?? [])
  const dataMin = axis.length ? axisToYM(axis[0]) : ''
  const dataMax = axis.length ? axisToYM(axis[axis.length - 1]) : ''
  const yearStart = axis.length ? axisToYM(axis[Math.max(0, axis.length - PER_YEAR[rangeMode])]) : ''

  // Adjust state during render — 최초 로드 시 디폴트 최근 1년으로 1회 세팅
  if (rawData && !synced && axis.length > 0) {
    setSynced(true)
    setFrom(yearStart); setTo(dataMax)
    setWinFrom(yearStart); setWinTo(dataMax)
  }

  const data = useMemo(
    () => sliceSeriesByWindow(rawData, rangeMode, winFrom, winTo),
    [rawData, rangeMode, winFrom, winTo],
  )

  const apply = () => { setWinFrom(from); setWinTo(to) }
  const reset = () => { setFrom(yearStart); setTo(dataMax); setWinFrom(yearStart); setWinTo(dataMax) }
  return {
    data, from, to, setFrom, setTo, apply, reset,
    minYM: dataMax ? threeYearMin(dataMin, dataMax) : undefined,
    maxYM: dataMax || undefined,
    // ★ 원본(슬라이스 전) 월 축 — 18p/20p 날짜 피커가 3년 범위를 잡으려면 창(1년)이 아닌 전체 축이 필요
    fullMonths: rawData?.scope?.activity_months ?? [],
  }
}

function useDateRange(months: string[]): DateSec {
  const [from, setFrom] = useState('')
  const [to, setTo] = useState('')
  const [synced, setSynced] = useState(false)
  const dataMin = months.length ? axisToYM(months[0]) : ''
  const dataMax = months.length ? axisToYM(months[months.length - 1]) : ''
  const yearStart = months.length ? axisToYM(months[Math.max(0, months.length - 12)]) : ''
  // Adjust state during render — 월 축 로드되면 최근 1년으로 1회 세팅
  if (months.length > 0 && !synced) {
    setSynced(true)
    setFrom(yearStart); setTo(dataMax)
  }
  return {
    from, to, setFrom, setTo,
    apply: () => {},
    reset: () => { setFrom(yearStart); setTo(dataMax) },
    minYM: dataMax ? threeYearMin(dataMin, dataMax) : undefined,
    maxYM: dataMax || undefined,
  }
}

// topics 섹션(18p·20p) — 적용된 필터로 fetch. apply(f)로 조회 시 재fetch.
type TopicsQuery = {
  data: TopicsData | null
  error: string | null
  isLoading: boolean
  apply: (filters: TopicsFilters | null) => void
}

type TopicPeriodResponse = {
  readonly result?: {
    readonly meta?: { readonly period?: TopicPeriodBounds }
  }
}

type TopicSourceMonths = {
  readonly months: readonly string[]
  readonly error: string | null
}

function useTopicSourceMonths(productName: string, atcKey: string, marketScope: BrandActivityScope): TopicSourceMonths {
  const requestScopeKey = brandActivityScopeKey(marketScope)
  const requestKey = `${productName}|${atcKey}|${requestScopeKey}`
  const [result, setResult] = useState<{ key: string; value: TopicSourceMonths }>({
    key: '',
    value: { months: [], error: null },
  })

  useEffect(() => {
    if (!productName || !atcKey) return
    let alive = true
    apiFetch('/api/v1/market/brand/topics', {
      method: 'POST',
      body: JSON.stringify({
        ...buildBrandActivityScopeRequest(marketScope, {
          atc: { atc4: atcKey.split(',') },
          analysis_level: { iqvia: { audit_code: [] } },
          channel: { visit_location: [], specialty: [] },
        }),
        selected_brand: productName,
        top_n: 1,
      }),
    })
      .then(async response => {
        if (!response.ok) throw new Error(`brand_topic_period_request_failed:${response.status}`)
        const payload = (await response.json()) as TopicPeriodResponse
        const months = resolveTopicMonths(payload.result?.meta?.period ?? null)
        if (months.length === 0) throw new Error('brand_topic_period_response_missing')
        if (alive) setResult({ key: requestKey, value: { months, error: null } })
      })
      .catch(error => {
        if (!alive) return
        setResult({
          key: requestKey,
          value: {
            months: [],
            error: error instanceof Error ? error.message : '키워드 기간 조회에 실패했습니다.',
          },
        })
      })
    return () => { alive = false }
  }, [productName, atcKey, marketScope, requestScopeKey, requestKey])

  return result.key === requestKey ? result.value : { months: [], error: null }
}

function useTopicsQuery(
  productName: string,
  atcKey: string,
  marketScope: BrandActivityScope,
  defFrom: string,
  defTo: string,
  initialFilters: TopicsFilters | null = null,
): TopicsQuery {
  const requestScopeKey = brandActivityScopeKey(marketScope)
  const [applied, setApplied] = useState<TopicsFilters | null>(initialFilters)
  const [result, setResult] = useState<{ key: string; data: TopicsData | null; error: string | null }>({
    key: '',
    data: null,
    error: null,
  })
  const effectiveFrom = applied?.period_start ?? defFrom
  const effectiveTo = applied?.period_end ?? defTo
  const visit = applied?.visit_location
  const specialty = applied?.specialty
  const interest = applied?.interest
  const evolution = applied?.prescription_evolution
  const requestKey = effectiveFrom && effectiveTo
    ? `${productName}|${atcKey}|${requestScopeKey}|${effectiveFrom}|${effectiveTo}|${visit ?? ''}|${specialty ?? ''}|${interest ?? ''}|${evolution ?? ''}`
    : ''
  useEffect(() => {
    if (!atcKey || !effectiveFrom || !effectiveTo || !requestKey) return
    let alive = true
    fetchTopics(productName, atcKey.split(','), marketScope, {
      period_start: effectiveFrom,
      period_end: effectiveTo,
      visit_location: visit,
      specialty,
      interest,
      prescription_evolution: evolution,
    })
      .then(next => {
        if (alive) setResult({ key: requestKey, data: next, error: null })
      })
      .catch(() => {
        if (alive) {
          setResult({
            key: requestKey,
            data: null,
            error: '데이터를 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.',
          })
        }
    })
    return () => { alive = false }
  }, [productName, atcKey, marketScope, requestScopeKey, effectiveFrom, effectiveTo, visit, specialty, interest, evolution, requestKey])
  const settled = result.key === requestKey
  return {
    data: settled ? result.data : null,
    error: settled ? result.error : null,
    isLoading: Boolean(requestKey) && !settled,
    apply: setApplied,
  }
}

function TopicsQueryResult({ query }: { query: TopicsQuery }) {
  if (query.error) {
    return (
      <div role="alert" style={{ padding: '56px 16px', textAlign: 'center', color: '#b42318', fontSize: 14 }}>
        {query.error}
      </div>
    )
  }
  return <KeywordShareGrid data={query.data} isLoading={query.isLoading} />
}

export default function BrandActivityTab({
  productName,
  atcCodes,
  strategicMarkets,
}: {
  productName: string
  atcCodes: string[]
  strategicMarkets: readonly StrategicInterestMarket[]
}) {
  // 탭 진입 직후 cause가 아직 로드 전이면 atcCodes=[]이라 스킵 → 로드되면 atcKey 변경으로 재실행.
  const atcKey = atcCodes.join(',')
  const activityScope = useMemo(
    () => resolveBrandActivityScope(strategicMarkets[0]),
    [strategicMarkets],
  )

  const [appliedRank17, setAppliedRank17] = useState<'brand' | 'company'>('brand')
  const [appliedCh17, setAppliedCh17] = useState('all')   // 채널(csd_channel) — 조회 눌러야 적용
  const marketScopeKey = `${productName}|${atcKey}`
  const [appliedMarket17, setAppliedMarket17] = useState({ scopeKey: '', value: 'all' })
  const appliedMarket17Value = appliedMarket17.scopeKey === marketScopeKey ? appliedMarket17.value : 'all'
  const s17 = useSeriesSection(productName, atcKey, activityScope, 'monthly', { entityLevel: appliedRank17, activityChannel: appliedCh17 === 'all' ? 'TOTAL' : appliedCh17, csdMarket: appliedMarket17Value })
  const [intRankDraft, setIntRankDraft] = useState('brand')    
  const [intBrandDraft, setIntBrandDraft] = useState('')       
  const [intMarketDraft, setIntMarketDraft] = useState('all') 
  const [intCatDraft, setIntCatDraft] = useState('all')       
  const [intSpecDraft, setIntSpecDraft] = useState('all')     
  const [intFetch, setIntFetch] = useState({ scopeKey: '', market: 'all', visit: '전체', spec: '전체' })   // 서버 필터(재fetch) — 조회 시 확정
  const [intDisp, setIntDisp] = useState({ rank: 'brand', brand: '', from: '', to: '' })     // 표시(rank/brand)+기간 — 조회 시 확정
  const [interestData, setInterestData] = useState<InterestSeriesData | null>(null)
  const [interestFailure, setInterestFailure] = useState<{ key: string; message: string } | null>(null)
  // Strategic market stays the hidden brand-set identity. The visible market
  // selector is the CSD source-sheet grain returned by Channel's resolver.
  const interestMarket = strategicMarkets[0]
  const interestMarketId = interestMarket?.marketId
  const interestMarketName = interestMarket?.marketName
  const appliedInterestCsdMarket = intFetch.scopeKey === marketScopeKey ? intFetch.market : 'all'
  const interestRequestKey = `${productName}|${interestMarketId ?? ''}|${appliedInterestCsdMarket}|${intFetch.visit}|${intFetch.spec}`
  const missingInterestMarket = !atcKey || !interestMarketId || !interestMarketName
  const interestError = missingInterestMarket
    ? '시장 정보를 불러오지 못했습니다.'
    : interestFailure?.key === interestRequestKey ? interestFailure.message : ''
  useEffect(() => {
    if (!atcKey || !interestMarket || !interestMarketId || !interestMarketName) return
    let alive = true
    fetchInterestSeries(productName, interestMarket, intFetch.visit, intFetch.spec, appliedInterestCsdMarket)
      .then(data => {
        if (!alive) return
        setInterestData(data)
        setInterestFailure(null)
      })
      .catch(error => {
        if (!alive) return
        setInterestData(null)
        setInterestFailure({
          key: interestRequestKey,
          message: error instanceof Error ? error.message : 'INTEREST 조회에 실패했습니다.',
        })
      })
    return () => { alive = false }
  }, [productName, atcKey, intFetch, appliedInterestCsdMarket, interestMarket, interestMarketId, interestMarketName, interestRequestKey])
  const intMonths = interestData?.period?.months ?? []      
  const intLevels = interestData?.levels ?? ['VERY USEFUL', 'SOMEWHAT USEFUL', 'NOT AT ALL'] 
  const dInt = useDateRange(intMonths)   
  const [intDispSynced, setIntDispSynced] = useState(false)
  if (!intDispSynced && dInt.from && dInt.to) { setIntDispSynced(true); setIntDisp(d => ({ ...d, from: dInt.from, to: dInt.to })) }
  const itemsFor = (rank: string) =>
    rank === 'company'
      ? (interestData?.companies ?? []).map(c => ({ key: c.company_name, label: c.company_name, is_selected: false, series: c.series }))
      : (interestData?.brands ?? []).map(b => ({ key: b.brand_key, label: b.brand_name, is_selected: !!b.is_selected, series: b.series }))
  // 3-2 draft 옵션 = draft rank 기준 (드롭다운은 즉시 갱신, 차트는 조회 시 적용)
  const intDraftItems = itemsFor(intRankDraft)
  const effIntBrandDraft = selectInterestDisplayItem(intDraftItems, intBrandDraft)?.key ?? ''
  const intAppliedItems = itemsFor(intDisp.rank)
  const intSel = selectInterestDisplayItem(intAppliedItems, intDisp.brand)
  const s22 = useSeriesSection(productName, atcKey, activityScope, 'quarterly')
  const { months: topicMonths, error: topicPeriodError } = useTopicSourceMonths(productName, atcKey, activityScope)
  const d18 = useDateRange([...topicMonths])   // ② 날짜 — Keyword 소스 월 축
  const d20 = useDateRange([...topicMonths])   // ④ 날짜 — Keyword 소스 월 축
  const defTopicFrom = topicMonths.length ? axisToYM(topicMonths[Math.max(0, topicMonths.length - 12)]!) : ''
  const defTopicTo = topicMonths.length ? axisToYM(topicMonths[topicMonths.length - 1]!) : ''
  const t18Result = useTopicsQuery(productName, atcKey, activityScope, defTopicFrom, defTopicTo)   // 18p 종별/진료과
  const t20Result = useTopicsQuery(
    productName,
    atcKey,
    activityScope,
    defTopicFrom,
    defTopicTo,
    { interest: '전체' },
  )   // 20p 유용성/처방변화
  const t18 = topicPeriodError ? { ...t18Result, error: topicPeriodError, isLoading: false } : t18Result
  const t20 = topicPeriodError ? { ...t20Result, error: topicPeriodError, isLoading: false } : t20Result

  const [matrixData, setMatrixData] = useState<MatrixData | null>(null)
  useEffect(() => {
    if (!atcKey) return
    let alive = true
    fetchMatrix(productName, atcKey.split(','), activityScope).then(d => { if (alive) setMatrixData(d) }).catch(() => {})
    return () => { alive = false }
  }, [productName, atcKey, activityScope])

  const [rank17, setRank17] = useState('brand')
  const [ch17, setCh17] = useState('all')
  const [evo20, setEvo20] = useState<KeywordCrossMode>('interest')
  const [appliedEvo20, setAppliedEvo20] = useState<KeywordExportMode>('interest')
  const [appliedKeyword20, setAppliedKeyword20] = useState('all')
  const [keywordExportError, setKeywordExportError] = useState<string | null>(null)
  const [cat18, setCat18] = useState('all')
  const [spec18, setSpec18] = useState('all')
  const [kw20, setKw20] = useState('all')
  const [market17, setMarket17] = useState({ scopeKey: '', value: 'all' })
  const market17Value = market17.scopeKey === marketScopeKey ? market17.value : 'all'
  const marketOpts17: SelectOption[] = useMemo(() => [...buildCsdMarketOptions(s17.data?.scope.csd_markets ?? [])], [s17.data?.scope.csd_markets])
  const marketOpts: SelectOption[] = [...buildCsdMarketOptions(interestData?.scope?.csd_markets ?? [])]
  const intMarketDraftValue = marketOpts.some(option => option.value === intMarketDraft) ? intMarketDraft : 'all'

  const brand22Opts: SelectOption[] = (s22.data?.brands ?? []).map(b => ({ value: b.brand_name, label: b.brand_name }))
  const [brand22, setBrand22] = useState('')
  const effBrand22 = brand22 || s22.data?.brands?.find(b => b.is_selected)?.brand_name || s22.data?.brands?.[0]?.brand_name || ''

  const excelCall = (section: string, data: SeriesData | null) => () => {
    if (data) exportCallSeries({ productName, section, data })
  }
  const excelTopics = (section: string, data: TopicsData | null) => () => { if (data) exportKeywordTopics({ productName, section, data }) }
  const excelActivity = () => { if (s22.data) exportActivityUnit({ productName, brandName: effBrand22, data: s22.data }) }

  // 조회/새로고침 시 종별·진료과·유용성·처방변화 필터 적용 (셀렉트 값 그대로 API로 — 'all'만 '전체' sentinel)
  const query18 = () => t18.apply({ visit_location: mapAll(cat18), specialty: mapAll(spec18), period_start: d18.from, period_end: d18.to })
  const reset18 = () => { setCat18('all'); setSpec18('all'); t18.apply(null) }   // null = 디폴트 1년 기간으로 복귀(날짜 피커도 sec.reset()로 1년 복귀)
  const queryInt = () => {
    setIntFetch({ scopeKey: marketScopeKey, market: intMarketDraftValue, visit: mapAll(intCatDraft), spec: mapAll(intSpecDraft) })
    setIntDisp({ rank: intRankDraft, brand: effIntBrandDraft, from: dInt.from, to: dInt.to })
  }
  const resetInt = () => {
    setIntRankDraft('brand'); setIntBrandDraft(''); setIntMarketDraft('all'); setIntCatDraft('all'); setIntSpecDraft('all')
    setIntFetch({ scopeKey: marketScopeKey, market: 'all', visit: '전체', spec: '전체' })
    dInt.reset()
    setIntDisp({ rank: 'brand', brand: '', from: '', to: '' }); setIntDispSynced(false)   // 기간은 아래 sync 블록이 1년으로 재설정
  }
  const intBrandOpts: SelectOption[] = intDraftItems.map(x => ({ value: x.key, label: x.label }))
  const excelInt = () => {
    if (!intSel) return
    const sMonths = intMonths.filter(m => (!intDisp.from || axisToYM(m) >= intDisp.from) && (!intDisp.to || axisToYM(m) <= intDisp.to))
    const s = intSel.series
    const bucketArr = (level: 'VERY USEFUL' | 'SOMEWHAT USEFUL' | 'NOT AT ALL') => ({
      count: sMonths.map(m => s[m]?.[level]?.count ?? null),
      pct: sMonths.map(m => s[m]?.[level]?.pct ?? null),   // 이미 %
    })
    exportInterestSeries({
      productName, brandName: intSel.label, months: sMonths,
      very: bucketArr('VERY USEFUL'), somewhat: bucketArr('SOMEWHAT USEFUL'), not: bucketArr('NOT AT ALL'),
      total: sMonths.map(m => s[m]?.total_count ?? null),
    })
  }
  const applyKeywordMode = (mode: KeywordCrossMode, keyword = 'all') => {
    const exportMode = mode === 'interest' ? 'interest' : 'prescription_evolution'
    setAppliedEvo20(exportMode)
    setAppliedKeyword20(keyword)
    t20.apply({
      ...(mode === 'interest'
        ? { interest: mapAll(keyword) }
        : { prescription_evolution: mapAll(keyword) }),
      period_start: d20.from,
      period_end: d20.to,
    })
  }
  const query20 = () => applyKeywordMode(evo20, kw20)
  const reset20 = () => {
    const exportMode = evo20 === 'interest' ? 'interest' : 'prescription_evolution'
    setKw20('all')
    setAppliedEvo20(exportMode)
    setAppliedKeyword20('all')
    t20.apply(evo20 === 'interest' ? { interest: '전체' } : { prescription_evolution: '전체' })
  }
  const excelKeywordCross = async () => {
    setKeywordExportError(null)
    try {
      const values = appliedKeyword20 === 'all'
        ? keywordCrossDomain(matrixData, appliedEvo20)
        : [appliedKeyword20]
      const datasets = await fetchKeywordCrossDatasets({
        mode: appliedEvo20,
        values,
        fetchData: filter => fetchTopics(productName, atcKey.split(','), activityScope, {
          ...filter,
          period_start: t20.data?.scope.period_start ?? d20.from,
          period_end: t20.data?.scope.period_end ?? d20.to,
        }),
      })
      exportKeywordCrossTopics({ productName, mode: appliedEvo20, datasets })
    } catch {
      setKeywordExportError('선택한 기준의 엑셀 데이터를 불러오지 못했습니다.')
    }
  }

  // 섹션별 날짜 컨트롤 블록 — 조회/새로고침 시 sec(날짜) + onQuery/onResetExtra(필터) 동시 적용
  const dateBlock = (
    sec: DateSec, showReset: boolean, onExcel?: () => void,
    mode: 'monthly' | 'quarterly' = 'monthly', onQuery?: () => void, onResetExtra?: () => void,
  ) => (
    <>
      <div className="control-box control-box-date">
        <DateRangePicker from={sec.from} to={sec.to} mode={mode} minYM={sec.minYM} maxYM={sec.maxYM} onFromChange={sec.setFrom} onToChange={sec.setTo} />
      </div>
      <button type="button" className="btn-date-search" onClick={() => { sec.apply(); onQuery?.() }}>조회</button>
      {showReset && <button type="button" className="btn-date-reset" onClick={() => { sec.reset(); onResetExtra?.() }}>새로고침</button>}
      {onExcel && <><div className="in-sepa-line" /><button type="button" className="btn-excel-down" onClick={onExcel}>엑셀다운로드</button></>}
    </>
  )

  return (
    <section className="chart-section" data-brand-activity-csd-market="backend-scope" data-brand-activity-topic-period="keyword-source">
      <div className="chat-section-title-n">Channel Dynamics</div>
      <div className="chart-widget chart-widget-full">
        <div className="chart-widget-header">
          <div className="chart-widget-title-group">
            <h2 className="chart-widget-title">채널별 콜 수 추이 및 Share</h2>
            <div className="btn-icon btn-icon-info"><div className="chart-tooltip"><TooltipBody text={BRAND_ACTIVITY_TOOLTIPS.channelShare} /></div></div>
          </div>
          <div className="chart-widget-controls">
            <CtlSelect options={RANK_OPTS} value={rank17} onChange={setRank17} />
            <div className="in-sepa-line" />
            <CtlSelect options={marketOpts17} value={market17Value} onChange={value => setMarket17({ scopeKey: marketScopeKey, value })} />
            <CtlSelect options={CHANNEL_OPTS} value={ch17} onChange={setCh17} />
            {dateBlock(s17, true, excelCall('채널별 콜 수 추이 및 Share', s17.data), 'monthly',
              () => { setAppliedRank17(rank17 as 'brand' | 'company'); setAppliedCh17(ch17); setAppliedMarket17({ scopeKey: marketScopeKey, value: market17Value }) },
              () => { setRank17('brand'); setAppliedRank17('brand'); setCh17('all'); setAppliedCh17('all'); setMarket17({ scopeKey: marketScopeKey, value: 'all' }); setAppliedMarket17({ scopeKey: marketScopeKey, value: 'all' }) })}
          </div>
        </div>
        <div className="chart-widget-body"><CallShareChart data={s17.data} /></div>
      </div>

      {/* 브랜드별 키워드 점유 구조 (표 — 완전) ===== */}
      <div className="chat-section-title-n">Keyword</div>
      <div className="chart-widget chart-widget-full">
        <div className="chart-widget-header">
          <div className="chart-widget-title-group">
            <h2 className="chart-widget-title">브랜드별 키워드 점유 구조</h2>
            <div className="btn-icon btn-icon-info"><div className="chart-tooltip"><TooltipBody text={BRAND_ACTIVITY_TOOLTIPS.keywordShare} /></div></div>
          </div>
          <div className="chart-widget-controls">
            <CtlSelect options={CATEGORY_OPTS} value={cat18} onChange={setCat18} />
            <CtlSelect options={SPECIALTY_OPTS} value={spec18} onChange={setSpec18} />
            {dateBlock(d18, true, excelTopics('브랜드별 키워드 점유 구조', t18.data), 'monthly', query18, reset18)}
          </div>
        </div>
        <div className="chart-widget-body">
          <TopicsQueryResult query={t18} />
        </div>
      </div>

      <div className="chat-section-title-n">고객 Perception</div>
      <div className="chart-widget chart-widget-full">
        <div className="chart-widget-header">
          <div className="chart-widget-title-group">
            <h2 className="chart-widget-title">INTEREST</h2>
            <div className="btn-icon btn-icon-info"><div className="chart-tooltip"><TooltipBody text={BRAND_ACTIVITY_TOOLTIPS.interest} /></div></div>
          </div>
          <div className="chart-widget-controls">
            <CtlSelect options={RANK_OPTS} value={intRankDraft} onChange={setIntRankDraft} />
            {intBrandOpts.length > 0 && <CtlSelect options={intBrandOpts} value={effIntBrandDraft} onChange={setIntBrandDraft} />}
            <div className="in-sepa-line" />
            {marketOpts.length > 1 && <CtlSelect options={marketOpts} value={intMarketDraftValue} onChange={setIntMarketDraft} />}
            <CtlSelect options={INT_VISIT_OPTS} value={intCatDraft} onChange={setIntCatDraft} />
            <CtlSelect options={SPECIALTY_OPTS} value={intSpecDraft} onChange={setIntSpecDraft} />
            {dateBlock(dInt, true, excelInt, 'monthly', queryInt, resetInt)}
          </div>
        </div>
        <div className="chart-widget-body">
          {interestError
            ? <div role="alert" style={{ padding: '32px 16px', textAlign: 'center', color: '#b42318' }}>{interestError}</div>
            : <InterestPerceptionChart series={intSel?.series ?? null} months={intMonths} levels={intLevels} fromYM={intDisp.from} toYM={intDisp.to} />}
        </div>
      </div>

      {/* 키워드 × Prescription Evolution (표 재활용 — 완전) ===== */}
      <div className="chart-widget chart-widget-full">
        <div className="chart-widget-header">
          <div className="chart-widget-title-group">
            <h2 className="chart-widget-title">키워드 X {evo20 === 'interest' ? 'INTEREST' : 'Prescription Evolution'}</h2>
            <div className="btn-icon btn-icon-info"><div className="chart-tooltip"><TooltipBody text={BRAND_ACTIVITY_TOOLTIPS.keywordCross} /></div></div>
          </div>
          <div className="chart-widget-controls">
            <CtlSelect options={evo20 === 'interest' ? INTEREST_OPTS : EVOLUTION_OPTS} value={kw20} onChange={setKw20} />
            {dateBlock(d20, true, undefined, 'monthly', query20, reset20)}
            <div className="in-sepa-line" />
            <SlideToggle
              className="slide-toggle--kw"
              options={KEYWORD_CROSS_MODES}
              value={evo20}
              onChange={v => { setEvo20(v); setKw20('all'); applyKeywordMode(v) }}
            />
            <div className="in-sepa-line" />
            <button
              type="button"
              className="btn-excel-down"
              onClick={() => { void excelKeywordCross() }}
            >엑셀다운로드</button>
          </div>
        </div>
        {keywordExportError && <div role="alert" style={{ padding: '8px 16px', color: '#b42318', fontSize: 13 }}>{keywordExportError}</div>}
        <div className="chart-widget-body">
          <TopicsQueryResult query={t20} />
        </div>
      </div>

      {/* Prescription frequency × Prescription evolution (버블) ===== */}
      <div className="chart-widget chart-widget-full">
        <div className="chart-widget-header">
          <div className="chart-widget-title-group">
            <h2 className="chart-widget-title">Prescription frequency X evolution</h2>
            <div className="btn-icon btn-icon-info"><div className="chart-tooltip"><TooltipBody text={BRAND_ACTIVITY_TOOLTIPS.perception} /></div></div>
          </div>
        </div>
        <div className="chart-widget-body"><MatrixBubbleChart data={matrixData} /></div>
      </div>

      {/* 활동량 추이와 단위별 처방량 (라인 3 + 영역) ===== */}
      <div className="chat-section-title-n">활동량 및 단위별 처방량</div>
      <div className="chart-widget chart-widget-full">
        <div className="chart-widget-header">
          <div className="chart-widget-title-group">
            <h2 className="chart-widget-title">활동량 및 단위별 처방량 추이</h2>
            <div className="btn-icon btn-icon-info"><div className="chart-tooltip"><TooltipBody text={BRAND_ACTIVITY_TOOLTIPS.activityVolume} /></div></div>
          </div>
          <div className="chart-widget-controls">
            {brand22Opts.length > 0 && <CtlSelect options={brand22Opts} value={effBrand22} onChange={setBrand22} />}
            {dateBlock(s22, true, excelActivity, 'quarterly')}
          </div>
        </div>
        <div className="chart-widget-body"><ActivityUnitChart data={s22.data} selectedBrand={effBrand22} /></div>
      </div>
    </section>
  )
}

// 콜 수(활동량) 추이 라인 + 최근 시점 Share 폴라 (17p·19p) ──
function CallShareChart({ data }: { data: SeriesData | null }) {
  if (!data) return <ChartSkeleton legendItems={6} />
  const months = data.scope?.activity_months ?? []
  const brands = data.brands ?? []
  const colors = brandColors(brands)
  const lastM = months[months.length - 1] ?? ''

  const lineData = {
    labels: months.map(m => m.replace('-', '.')),
    datasets: brands.map((b, i) => ({
      label: b.brand_name,
      data: months.map(m => b.series.activity.absolute[m] ?? null),
      borderColor: colors[i], backgroundColor: colors[i],
      fill: false, tension: 0.3, pointRadius: 2,
    })),
  }
  // 폴라(최근 시점 콜 수 Share) — 각 브랜드의 최근 월 share(%) = 반지름
  const polarData = {
    labels: brands.map(b => b.brand_name),
    datasets: [{
      data: brands.map(b => b.series.activity.ratio[lastM] ?? 0),
      backgroundColor: colors.map(c => c + '99'),
      borderColor: colors,
      borderWidth: 1,
    }],
  }

  return (
    <div className="chart-layout-split">
      <div className="chart-layout-split-main">
        <div style={centerTitle}><span>콜 수 추이</span></div>
        <div style={{ height: 429 }}>
          <Line data={lineData} options={{
            ...commonOpts,
            interaction: { mode: 'index', intersect: false },
            plugins: {
              legend: { ...legendBottom, ...legendHoverPointer },
              tooltip: {
                mode: 'index', intersect: false,
                // sales_rank 오름차순 (1위→…) — 경쟁순위 차트와 동일 패턴
                itemSort: (a, b) => {
                  const ra = brands[a.datasetIndex]?.sales_rank ?? 1e9
                  const rb = brands[b.datasetIndex]?.sales_rank ?? 1e9
                  return ra - rb
                },
                callbacks: {
                  title: items => fmtPeriodKor(months[items[0]?.dataIndex ?? 0] ?? ''),
                  label: ctx => {
                    const b = brands[ctx.datasetIndex]
                    const m = months[ctx.dataIndex] ?? ''
                    const share = b ? (b.series.activity.ratio[m] ?? 0) : 0
                    const v = ctx.parsed.y ?? 0
                    const calls = Math.round(v).toLocaleString()
                    const periodRank = brands.filter(x => (x.series.activity.absolute[m] ?? 0) > v).length + 1
                    return v === 0
                      ? `${ctx.dataset.label} (콜 수 ${calls}, Share ${share.toFixed(1)}%)`
                      : `${periodRank}위 ${ctx.dataset.label} (콜 수 ${calls}, Share ${share.toFixed(1)}%)`
                  },
                },
              },
            },
            scales: {
              x: { grid: { display: false }, ticks: { maxTicksLimit: 12, autoSkip: true } },
              y: { title: { display: true, text: '콜 수' }, ticks: { callback: v => Number(v).toLocaleString() } },
            },
          }} />
        </div>
      </div>
      <div className="chart-layout-split-sub">
        <div style={centerTitle}><span>콜 수 Share (최근 시점 : {fmtPeriodKor(lastM)} 기준)</span></div>
        <div style={{ height: 429 }}>
          <PolarArea data={polarData} options={{
            ...commonOpts,
            scales: {
              r: {
                ticks: {
                  z: 1,
                  backdropColor: 'rgba(0,0,0,0)',
                  callback: (v: number | string) => {
                    const n = Number(v)
                    return `${Number.isInteger(n) ? n : n.toFixed(1)}%`
                  },
                },
              },
            },
            plugins: {
              legend: { ...legendBottom, labels: { ...legendBottom.labels, generateLabels: opaqueLabelsForArc }, onClick: () => {} },
              tooltip: {
                callbacks: {
                  title: () => '',
                  label: ctx => {
                    const b = brands[ctx.dataIndex]
                    const v = b ? (b.series.activity.ratio[lastM] ?? 0) : 0
                    const periodRank = brands.filter(x => (x.series.activity.ratio[lastM] ?? 0) > v).length + 1
                    const share = Number(ctx.parsed.r ?? ctx.parsed).toFixed(1)
                    return v === 0
                      ? `${ctx.label} (Share ${share}%)`
                      : `${periodRank}위 ${ctx.label} (Share ${share}%)`
                  },
                },
              },
            },
          }} />
        </div>
      </div>
    </div>
  )
}

function InterestPerceptionChart({ series, months, levels, fromYM, toYM }: {
  series: InterestSeriesMap | null; months: string[]; levels: string[]; fromYM: string; toYM: string
}) {
  if (!series) return <ChartSkeleton legendItems={3} />
  const sMonths = months.filter(m => (!fromYM || axisToYM(m) >= fromYM) && (!toYM || axisToYM(m) <= toYM))
  if (sMonths.length === 0) return <div style={{ padding: '60px 0', textAlign: 'center', color: '#aaa' }}>데이터가 없습니다.</div>
  const bucketOf = (slot: InterestMonthSlot | undefined, level: string): InterestBucket | undefined => {
    if (!slot) return undefined
    if (level === 'VERY USEFUL') return slot['VERY USEFUL']
    if (level === 'SOMEWHAT USEFUL') return slot['SOMEWHAT USEFUL']
    if (level === 'NOT AT ALL') return slot['NOT AT ALL']
    return undefined
  }
  const colorOf = (level: string) => level.startsWith('VERY') ? INT_COLORS.VERY : level.startsWith('SOMEWHAT') ? INT_COLORS.SOMEWHAT : INT_COLORS.NOT
  const shortOf = (level: string) => level.startsWith('VERY') ? 'VERY' : level.startsWith('SOMEWHAT') ? 'SOMEWHAT' : 'NOT'
  const totals = sMonths.map(m => series[m]?.total_count ?? 0)
  const cntByLevel: Record<string, number[]> = {}
  levels.forEach(l => { cntByLevel[l] = sMonths.map(m => bucketOf(series[m], l)?.count ?? 0) })
  const barData = {
    labels: sMonths.map(m => m.replace('-', '.')),
    datasets: levels.map(level => ({
      label: shortOf(level),
      data: sMonths.map(m => { const b = bucketOf(series[m], level); return b ? b.pct : null }),
      backgroundColor: colorOf(level) + '66',       
      hoverBackgroundColor: colorOf(level) + '99',  
      stack: 'int',
    })),
  }
  return (
    <div>
      <div style={centerTitle}><span>응답 분포 추이</span></div>
      <div style={{ height: 429 }}>
        <Bar data={barData} options={{
          ...commonOpts,
          interaction: { mode: 'index', intersect: false },
          plugins: {
            legend: { ...legendBottom, ...legendHoverPointer },
            tooltip: {
              mode: 'index', intersect: false,
              callbacks: {
                title: items => {
                  const di = items[0]?.dataIndex ?? 0
                  return `${fmtPeriodKor(sMonths[di] ?? '')} (총 응답 수 : ${(totals[di] ?? 0).toLocaleString()})`
                },
                label: ctx => {
                  const level = levels[ctx.datasetIndex] ?? ''
                  const pct = Math.round(ctx.parsed.y ?? 0)
                  const cnt = cntByLevel[level]?.[ctx.dataIndex] ?? 0
                  return `${shortOf(level)} (응답 비율 : ${pct}%, 응답 수 : ${cnt.toLocaleString()})`
                },
              },
            },
          },
          scales: {
            x: { stacked: true, grid: { display: false }, ticks: { maxTicksLimit: 12, autoSkip: true } },
            y: { stacked: true, min: 0, max: 100, title: { display: true, text: '응답 비율 (%)' }, ticks: { callback: v => `${v}` } },
          },
        }} />
      </div>
    </div>
  )
}

// Interest × 처방빈도 버블 (차트3·4 구조) ──
// X = rx_frequency_score, Y = interest_score(evolution 대체), 면적 = event_count, 평균 십자 점선
function MatrixBubbleChart({ data }: { data: MatrixData | null }) {
  if (!data) return <ChartSkeleton legendItems={4} />
  const brands = (data.brands ?? []).filter(b => b.event_count > 0)  // event_count=0이면 score 3종 null → 제외
  if (brands.length === 0) return <div style={{ padding: '40px 0', textAlign: 'center', color: '#aaa' }}>데이터가 없습니다.</div>
  const colors = brandColors(brands)
  const avg = data.market_average ?? { rx_frequency_score: 0, prescription_evolution_score: 0 }

  // X = 처방빈도, Y = 처방변화(prescription_evolution_score, 2026-07-08 실측 확정), 면적 = 응답 수
  const bubbleData = {
    datasets: brands.map((b, i) => ({
      label: b.brand_name,
      data: [{ x: b.rx_frequency_score, y: b.prescription_evolution_score, r: Math.max(6, Math.sqrt(b.event_count) / 2), brand: b.brand_name, count: b.event_count }],
      backgroundColor: colors[i] + 'cc',
      borderColor: colors[i],
    })),
  }
  return (
    <div style={{ height: 429 }}>
      <Bubble data={bubbleData} options={{
        ...commonOpts,
        plugins: {
          legend: { ...legendBottom, onClick: () => {} },
          tooltip: {
            callbacks: {
              title: () => '',
              label: ctx => {
                const r = ctx.raw as { brand: string; x: number; y: number; count: number }
                return [r.brand, `처방빈도 : ${r.x.toFixed(3)}`, `처방변화 : ${r.y.toFixed(3)}`, `응답 수 : ${r.count.toLocaleString()}`]
              },
            },
          },
          annotation: {
            annotations: {
              avgX: { type: 'line' as const, xMin: avg.rx_frequency_score, xMax: avg.rx_frequency_score, borderColor: 'rgba(0,0,0,0.3)', borderDash: [5, 5] },
              avgY: { type: 'line' as const, yMin: avg.prescription_evolution_score, yMax: avg.prescription_evolution_score, borderColor: 'rgba(0,0,0,0.3)', borderDash: [5, 5] },
            },
          },
        },
        scales: {
          x: { title: { display: true, text: 'Prescription frequency score' } },
          y: { title: { display: true, text: 'Prescription evolution score' } },
        },
      }} />
    </div>
  )
}

//단위별 처방량 누적막대 + 활동량 라인 (차트1 이중축) ──
function ActivityUnitChart({ data, selectedBrand }: { data: SeriesData | null; selectedBrand: string }) {
  if (!data) return <ChartSkeleton rightAxis legendItems={4} />
  const brs = data.brands ?? []
  const sel = brs.find(b => b.brand_name === selectedBrand)
    ?? brs.find(b => b.is_selected) ?? brs[0]
  if (!sel) return <div style={{ padding: '40px 0', textAlign: 'center', color: '#aaa' }}>데이터가 없습니다.</div>
  const quarters = data.scope?.quarters ?? []

  const quarterMonths = (q: string): string[] => {
    const m = /^(\d{4})-Q(\d)$/.exec(q)
    if (!m) return []
    const start = (Number(m[2]) - 1) * 3 + 1
    return [start, start + 1, start + 2].map(mm => `${m[1]}-${String(mm).padStart(2, '0')}`)
  }
  const activityByQuarter = (q: string): number =>
    quarterMonths(q).reduce((s, mm) => s + (sel.series.activity.absolute[mm] ?? 0), 0)

  const lineMeasures: { key: 'unit' | 'dosage_unit' | 'counting_unit'; label: string; color: string }[] = [
    { key: 'unit', label: 'UNIT', color: '#29ABE2' },
    { key: 'dosage_unit', label: 'DOSAGE UNIT', color: '#F26D8B' },
    { key: 'counting_unit', label: 'COUNT UNIT', color: '#F7931E' },
  ]
  const chartData: ChartData<'line', number[], string> = {
    // x축 라벨은 설계서대로 raw 분기 "YYYY-Qn" (툴팁 제목만 "YYYY년 N분기")
    labels: quarters,
    datasets: [
      ...lineMeasures.map(m => ({
        label: m.label,
        data: quarters.map(q => sel.series[m.key].absolute[q] ?? 0),
        borderColor: m.color, backgroundColor: m.color,
        yAxisID: 'y', fill: false, tension: 0.3, pointRadius: 2, borderWidth: 2,
      })),
      {
        label: '활동량 (콜 수)',
        data: quarters.map(activityByQuarter),
        borderColor: '#7CC5E8', backgroundColor: 'rgba(197, 232, 247, 0.55)',
        yAxisID: 'y1', fill: 'origin', tension: 0.3,
        pointRadius: 0, pointHoverRadius: 0, borderWidth: 0,
        order: 1,   // 라인들 뒤에 영역이 깔리도록
      },
    ],
  }
  return (
    <div>
      <div style={{ height: 429 }}>
        <Line data={chartData} options={{
          ...commonOpts,
          interaction: { mode: 'index', intersect: false },
          plugins: {
            legend: { ...legendBottom, ...legendHoverPointer },
            tooltip: {
              mode: 'index', intersect: false,
              callbacks: {
                title: items => fmtPeriodKor(quarters[items[0]?.dataIndex ?? 0] ?? ''),
                label: ctx => `${ctx.dataset.label} : ${Math.round(ctx.parsed.y ?? 0).toLocaleString()}`,
                afterBody: items => {
                  const q = quarters[items[0]?.dataIndex ?? 0] ?? ''
                  const IND = '    '
                  const parts = quarterMonths(q).map(mm =>
                    `${IND}• ${parseInt(mm.slice(5), 10)}월 : ${Math.round(sel.series.activity.absolute[mm] ?? 0).toLocaleString()}`)
                  return parts.length ? [`${IND}[월별 활동량]`, ...parts] : []
                },
              },
            },
          },
          scales: {
            x: { grid: { display: false } },
            y: { position: 'left', title: { display: true, text: '처방량 (수량)' }, ticks: { callback: v => Number(v).toLocaleString() } },
            y1: { position: 'right', grid: { drawOnChartArea: false }, title: { display: true, text: '활동량 (콜 수)' }, ticks: { callback: v => Number(v).toLocaleString() } },
          },
        }} />
      </div>
    </div>
  )
}
