import { useState, useEffect, useCallback, useRef } from 'react'
import { useLocation, useNavigate, Navigate } from 'react-router-dom'
import {
  Chart as ChartJS,
  CategoryScale, LinearScale, PointElement, LineElement, BarElement,
  ArcElement, RadialLinearScale, Title, Tooltip, Legend, Filler,
  type LegendItem,
} from 'chart.js'
import { Line, Bar, Bubble, Doughnut } from 'react-chartjs-2'
import annotationPlugin, { type AnnotationOptions } from 'chartjs-plugin-annotation'
import Sidebar from '../components/main/Sidebar'
import SkelBar from '../components/main/SkelBar'
import DateRangePicker from '../components/main/DateRangePicker'
import SelectBox from '../components/ui/SelectBox'
import { useAuth } from '../context/AuthContext'
import { fetchCauseResult, snapshotBrandData, findBrandSalesResult, getCachedResult, causeKey, DEFAULT_FILTER_CONTEXT, buildFilterKey } from '../utils/causeStore'
import { dynamicMarketErrorMessage } from '../utils/dynamicMarketError'
import {
  brandSourcesForAssay,
  refreshMarketBrands,
  isJwBrand,
} from '../utils/dynamicMarket'
import {
  brandObservedSourcePeriods,
  isSourceSelectable,
  resolveSourceAvailability,
  shouldApplySupportedSourcesFromMeta,
  sourceAvailabilityTitle,
} from '../utils/sourceAvailability'
import type {
  CauseData, SelectOption, RankItem, DynamicFilterContext,
} from '../types/market'
import {
  TARGET_COLOR, COMPETITOR_PALETTE, OTHERS_COLOR,
  CONTRIB_UP, CONTRIB_DOWN, CONTRIB_BASE,
  EI_CATEGORIES, GC_MS_CATEGORIES,
  PERIOD_MONTHLY, UNIT_OPTIONS,
  fmtPeriodKor, fmtBaekman, ymStartOf, ymEndOf,
  commonOpts, legendBottom, legendHoverPointer,
  eiCategoryKey, gcMsCategoryKey, getBrandDisplayLabel,
  selectRankTrackerKeys,
  onPeriodInputChange,
  opaqueLabelsForArc,
  solidTooltipLabelColor,
  contribLabelPlugin, legendTextAlignPlugin,
  aggregateByUnit, aggregatePeriodsAndItems, aggregatePeriodsAnd10ptItems, type PeriodUnit,
} from '../utils/chartHelpers'
import Modals from '../components/main/Modals'
import { TooltipBody } from '../utils/chartTooltips'
import {
  CHART_TOOLTIPS,
  buildDynamicChartTooltips,
  buildMarketSizeGrowthTooltip,
} from '../utils/tooltipCopy'
import * as analyzeExcel from '../utils/analyzeExcel'
import MarketTopNav from '../components/main/MarketTopNav'
import ChartSkeleton from '../components/main/ChartSkeleton'
import AnalysisLevelChart from '../components/main/AnalysisLevelChart'
import BrandActivityTab from '../components/main/BrandActivityTab'
import { fetchBrandPresence } from '../utils/brandActivity'
import {
  readStrategicInterestMarkets,
  type StrategicInterestMarket,
} from '../utils/brandActivityInterest'
import { selectBrandCagr } from '../utils/brandCagr'
import MeasureToggle from '../components/main/MeasureToggle'
import SlideToggle from '../components/main/SlideToggle'
import { AgentChatProvider, AgentChatPanel, AgentChatTrigger } from '../components/main/AgentChat'
import AnalyzeFilterBar from '../components/main/AnalyzeFilterBar'
import { isLevelTop5Others, levelTop5BrandLabel, levelTop5EmptyMessage } from '../utils/levelTop5'
import { formatMarketGrowthPct } from '../utils/marketGrowthDisplay'

ChartJS.register(
  CategoryScale, LinearScale, PointElement, LineElement, BarElement,
  ArcElement, RadialLinearScale, Title, Tooltip, Legend, Filler,
  annotationPlugin, legendTextAlignPlugin
)

// 툴팁 색상 표시를 네모 → 동그라미
ChartJS.defaults.plugins.tooltip.usePointStyle = true
// 툴팁 색 동그라미 = 아래 범례와 동일(불투명·테두리 없음) — 전역 디폴트라 모든 차트 적용
ChartJS.defaults.plugins.tooltip.callbacks.labelColor = solidTooltipLabelColor
// 툴팁 항목 줄 간격 (따닥 붙지 않게)
ChartJS.defaults.plugins.tooltip.bodySpacing = 8
// 툴팁 상/하/좌/우 padding 20px (수정사항_20260526 공통)
ChartJS.defaults.plugins.tooltip.padding = 15
// 축 값 등 기본 폰트 13px (기획자 공통)
ChartJS.defaults.font.size = 13
// 모든 라인 차트 borderWidth 2px / hover 포인트 4px (수정사항_20260526 공통)
ChartJS.defaults.elements.line.borderWidth = 2
ChartJS.defaults.elements.point.hoverRadius = 4

// ============ 페이지 전용 상수 ============

interface ChatItem {
  uid: string
  title: string
  date: string
  pinned?: boolean
}
const MOCK_PINNED_LIST: ChatItem[] = []
const MOCK_NORMAL_LIST: ChatItem[] = []

const resolveKpiPayloadState = (
  kpi: object | null | undefined,
  isCauseLoading: boolean,
): { readonly loading: boolean; readonly noData: boolean } => {
  const hasData = kpi != null && Object.keys(kpi).length > 0
  return {
    loading: isCauseLoading,
    noData: !isCauseLoading && !hasData,
  }
}

const ASSAY_OPTIONS: SelectOption[] = [
  { value: 'jw', label: 'JW Strategic' },
  { value: 'market', label: 'Market Standard' },
]

const ANALYZE_TABS = ['Market Landscape', 'Competitive Dynamics', '브랜드 활동'] as const
type AnalyzeTab = typeof ANALYZE_TABS[number]
const TAB_KO_SUB: Partial<Record<AnalyzeTab, string>> = {
  'Market Landscape': '전체 치료 시장',
  'Competitive Dynamics': '직접 경쟁 시장',
}
// 클릭 비활성 탭 (현재 없음 — 브랜드 활동 탭 활성화됨, 2026-07)
const DISABLED_TABS: ReadonlySet<AnalyzeTab> = new Set<AnalyzeTab>()

// HHI 구간 밴드 — 경쟁 시장(~1500) / 부분 집중(1500~2500) / 과속·독과점(2500~)
// from/to 오름차순(경쟁→부분→과속)으로 두고 범례도 이 순서로 노출.
// 배경은 16%(피그마 스펙), 범례 동그라미는 흰 배경에서 식별되도록 좀 더 진하게(0.4).
const HHI_BANDS_META = [
  { key: 'hhiBandLow', label: '경쟁 시장', from: 0, to: 1500, rgb: '116,197,169' },      // #74C5A9
  { key: 'hhiBandMid', label: '부분 집중', from: 1500, to: 2500, rgb: '255,188,95' },     // #FFBC5F
  { key: 'hhiBandHigh', label: '과점/독과점', from: 2500, to: Infinity, rgb: '233,135,115' }, // #E98773
] as const
const HHI_LINE_COLOR = '#00A9E5'
const DYNAMIC_MARKET_ERROR_MESSAGE = dynamicMarketErrorMessage(null)

// 소수점 유지 — Math.round를 쓰면 작은 처방량(예: 7,227/1e4=0.72) 값이 0/1로 양극화되어 차트 면적이 부풀려 보임
const toYUnit = (raw: number, measure: 'sales' | 'volume'): number =>
  measure === 'sales' ? raw / 1e8 : raw / 1e4
const yTitleFor = (measure: 'sales' | 'volume'): string =>
  measure === 'sales' ? '매출 (억원)' : '처방량 (만)'

// ============ 차트 컨트롤 셀렉트 (디자인시스템 SelectBox — sm 40px / weight 400) ============

function ChartSelect(props: {
  wrapperClassName?: string
  options: SelectOption[]
  value: string
  onChange: (value: string) => void
}) {
  return <SelectBox {...props} size="sm" weight={400} />
}

// 차트 info 툴팁 본문은 복사본 레지스트리와 동적 빌더에서 가져온다.
// (정적: TOOLTIP_COPY / 동적: buildDynamicChartTooltips)

function InfoTooltip({ text }: { text: string }) {
  return (
    <div className="btn-icon btn-icon-info">
      <div className="chart-tooltip"><TooltipBody text={text} /></div>
    </div>
  )
}

// ============ AnalyzePage ============

// 외부에서 productName 변화 시 통째로 재마운트 시키기 위해 export 컴포넌트와 본체를 분리.
// 같은 URL `/market/analyze`로 다른 브랜드 navigate 시 React Router가 컴포넌트를 재사용해
// 내부 state(lazy initializer로 받은 productName 등)가 안 바뀌는 문제를 key로 해결.
export default function AnalyzePage() {
  const location = useLocation()
  const navState = location.state as { productName?: string } | null
  const productName = navState?.productName ?? ''
  if (!productName) return <Navigate to="/market" replace />
  return <AnalyzePageInner key={productName} />
}

function AnalyzePageInner() {
  const [alertMessage, setAlertMessage] = useState('')
  const { user } = useAuth()
  const location = useLocation()
  const navigate = useNavigate()
  // navigate state — sources(구), generalSources/strategicSources/assay(검색 진입 시 경쟁 브랜드 대응)
  const navState = location.state as {
    productName?: string; sources?: string[]
    generalSources?: string[]; strategicSources?: string[]; assay?: 'jw' | 'market'
  } | null
  const productName = navState?.productName ?? '리바로'
  const navInitialAssay: 'jw' | 'market' = (isJwBrand(productName) === false || navState?.assay === 'market') ? 'market' : 'jw'
  const assayLockedToMarket =
    isJwBrand(productName) === false
    && !(navState?.strategicSources ?? []).some(s => s === 'UBIST' || s === 'IQVIA')

  // 사이드바 / 프로필
  const [sidebarOpen, setSidebarOpen] = useState(false)
  // top-navigation 스크롤 슬라이드 — 내리면 숨김, 올리면 표시 (떨림 방지: 5px 임계, 상단 50px 안에서는 무조건 표시)
  const [navHidden, setNavHidden] = useState(false)
  const navHiddenRef = useRef(false)
  const lastScrollRef = useRef(0)
  // 상단 스크롤 버튼 — scrollTop > 300px일 때 표시
  const scrollRef = useRef<HTMLDivElement>(null)
  const [showScrollTop, setShowScrollTop] = useState(false)
  const handleContentScroll = (e: React.UIEvent<HTMLDivElement>) => {
    const el = e.currentTarget
    const top = el.scrollTop
    const diff = top - lastScrollRef.current
    // 상단 스크롤 버튼 표시 여부
    setShowScrollTop(top > 300)
    if (top < 50) {
      if (navHiddenRef.current) { navHiddenRef.current = false; setNavHidden(false) }
      lastScrollRef.current = top
      return
    }
    // ⚠️ 바닥 근처에선 nav 토글 금지 — navHidden margin-top(-88px)이 스크롤 컨테이너 clientHeight를
    if (el.scrollHeight - top - el.clientHeight < 120) { lastScrollRef.current = top; return }
    if (Math.abs(diff) < 5) return
    if (diff > 0 && !navHiddenRef.current) { navHiddenRef.current = true; setNavHidden(true) }
    else if (diff < 0 && navHiddenRef.current) { navHiddenRef.current = false; setNavHidden(false) }
    lastScrollRef.current = top
  }
  const [activeChatId, setActiveChatId] = useState<string | null>(null)

  // 전역 조회 파라미터 — 초기 assay는 검색으로 넘어온 값(경쟁 브랜드=Market Standard), 없으면 JW Strategic
  const [assayValue, setAssayValue] = useState<'jw' | 'market'>(navInitialAssay)
  const [appliedFilters, setAppliedFilters] = useState<DynamicFilterContext>(() => ({ ...DEFAULT_FILTER_CONTEXT, assayMode: navInitialAssay }))
  const appliedFilterKey = buildFilterKey(appliedFilters)
  const [analyzeTab, setAnalyzeTab] = useState<AnalyzeTab>('Market Landscape')
  // 브랜드 CSD 존재 여부 (/brand/presence). false면 브랜드 활동 탭 비활성. null=로딩/판정보류(활성 유지)
  const [csdPresent, setCsdPresent] = useState<boolean | null>(null)
  // Market Standard 전환 시 Competitive Dynamics에 머물고 있었으면 Market Landscape로 복귀
  if (assayValue === 'market' && analyzeTab === 'Competitive Dynamics') setAnalyzeTab('Market Landscape')
  // CSD 없는 브랜드에서 브랜드 활동 탭에 머물고 있었으면 Market Landscape로 복귀
  if (csdPresent === false && analyzeTab === '브랜드 활동') setAnalyzeTab('Market Landscape')
  const brandSourcesFromNav = ((): Set<'UBIST' | 'IQVIA'> | null => {
    const srcs = (navState?.sources ?? []).filter((s): s is 'UBIST' | 'IQVIA' => s === 'UBIST' || s === 'IQVIA')
    return srcs.length > 0 ? new Set(srcs) : null
  })()
  // 검색 진입 시 넘어온 assay별 출처. 최신 카탈로그가 없을 때만 탐색 상태를 폴백으로 쓴다.
  const navSourcesForAssay = (assay: string): Set<'UBIST' | 'IQVIA'> | null => {
    const raw = assay === 'jw' ? navState?.strategicSources : navState?.generalSources
    const eff = (raw ?? []).filter((s): s is 'UBIST' | 'IQVIA' => s === 'UBIST' || s === 'IQVIA')
    return eff.length ? new Set(eff) : null
  }
  const assaySources = (assay: string): Set<'UBIST' | 'IQVIA'> =>
    brandSourcesForAssay(productName, assay === 'jw' ? 'jw' : 'market')
    ?? navSourcesForAssay(assay)
    ?? brandSourcesFromNav ?? new Set(['UBIST', 'IQVIA'])
  const initialSource: 'UBIST' | 'IQVIA' = (() => {
    const eff = assaySources(navInitialAssay)
    if (eff.has('UBIST')) return 'UBIST'
    if (eff.has('IQVIA')) return 'IQVIA'
    return 'UBIST'
  })()
  // IQVIA는 분기 데이터만 제공 → 기간 단위 디폴트를 quarterly로 시작
  const initialPeriod = initialSource === 'IQVIA' ? 'quarterly' : 'monthly'
  const [sourceToggle, setSourceToggle] = useState<'UBIST' | 'IQVIA'>(initialSource)
  // IQVIA + 처방량(volume)일 때 단위 선택 (v0.8)
  const [unitMeasure, setUnitMeasure] = useState<'unit' | 'dosage_unit' | 'counting_unit'>('unit')
  // 차트별 측정(매출/처방량) 독립 토글 (v0.8) — 차트1/5/7/8
  const [w1Measure, setW1Measure] = useState<'sales' | 'volume'>('sales')
  const [w5Measure, setW5Measure] = useState<'sales' | 'volume'>('sales')
  // 신규 차트 "주요 고객 분석 레벨별 매출 추이 및 M/S"
  const [wMsMeasure, setWMsMeasure] = useState<'sales' | 'volume'>('sales')
  const [w7Measure, setW7Measure] = useState<'sales' | 'volume'>('sales')
  const [w8Measure, setW8Measure] = useState<'sales' | 'volume'>('sales')
  // 매출/처방량 → 실제 cause measure (처방량+IQVIA는 선택 단위)
  const toMeasure = useCallback(
    (m: 'sales' | 'volume'): string => m === 'sales' ? 'sales' : sourceToggle === 'IQVIA' ? unitMeasure : 'volume',
    [sourceToggle, unitMeasure],
  )

  // causeCache 초기값 — 모듈 캐시(causeStore)에 이 브랜드의 TTL 유효 데이터가 있으면 즉시 주입
  // (페이지 왕복·Dashboard prefetch로 채워진 캐시 → 재진입 시 스켈레톤 없이 바로 차트)
  const [causeCache, setCauseCache] = useState<Record<string, CauseData | null>>(() => snapshotBrandData(productName))
  // 첫 cause 응답 도착 시 데이터 timeline에 맞춰 from/to 1회 동기화했는지
  const [hasSyncedDataRange, setHasSyncedDataRange] = useState(false)
  // 브랜드별 지원 source — 카탈로그의 assay별(strategic/general) 출처가 정본이다.
  const [supportedSources, setSupportedSources] = useState<Set<'UBIST' | 'IQVIA'>>(() => assaySources(assayValue))
  const observedSourcePeriods = brandObservedSourcePeriods(productName)
  const sourceAvailability = resolveSourceAvailability(
    supportedSources,
    Object.keys(observedSourcePeriods).map(source => `${source.toUpperCase()}.sales`),
  )
  // cause market_meta 폴백 — 현재 assay의 nav/cache source와 legacy nav source가 모두 없을 때만 적용
  const applySupportedFromMeta = (mm: { is_dual_source?: boolean; source_label?: string } | null | undefined) => {
    const cachedAssaySources = brandSourcesForAssay(productName, assayValue === 'jw' ? 'jw' : 'market')
    if (!shouldApplySupportedSourcesFromMeta(
      navSourcesForAssay(assayValue),
      cachedAssaySources,
      brandSourcesFromNav,
    )) return
    if (mm?.is_dual_source === true) {
      setSupportedSources(new Set(['UBIST', 'IQVIA']))
    } else if (mm?.is_dual_source === false && (mm.source_label === 'UBIST' || mm.source_label === 'IQVIA')) {
      setSupportedSources(new Set([mm.source_label]))
    }
  }

  // ★ assay 전환 시: supportedSources를 그 assay 출처로 갱신 + 현재 sourceToggle이 미지원이면 자동 보정
  const [lastAssayForSources, setLastAssayForSources] = useState(assayValue)
  if (assayValue !== lastAssayForSources) {
    setLastAssayForSources(assayValue)
    const next = assaySources(assayValue)
    setSupportedSources(next)
    if (!next.has(sourceToggle)) setSourceToggle(next.has('UBIST') ? 'UBIST' : 'IQVIA')
  }

  // ★ 진입 시 /brands 최신화 (로그인 캐시가 general/strategic_sources 추가 전이면 stale) → 갱신 후 supportedSources 재도출
  const [brandsRefreshed, setBrandsRefreshed] = useState(false)
  useEffect(() => { refreshMarketBrands(productName).then(() => setBrandsRefreshed(true)).catch(() => {}) }, [productName])
  const [lastBrandsRefreshed, setLastBrandsRefreshed] = useState(brandsRefreshed)
  if (brandsRefreshed !== lastBrandsRefreshed) {
    setLastBrandsRefreshed(brandsRefreshed)
    // 자사 25개 브랜드는 갱신된 marketBrandsResult로 supportedSources 재도출 (경쟁 브랜드는 nav 값이라 무영향)
    const next = assaySources(assayValue)
    setSupportedSources(next)
    if (!next.has(sourceToggle)) setSourceToggle(next.has('UBIST') ? 'UBIST' : 'IQVIA')
  }

  // 차트별 UI 컨트롤
  // 차트 2: 브랜드/회사 토글 (PDF v0.8 Page 10)
  const [rankingToggle, setRankingToggle] = useState<'브랜드' | '회사'>('브랜드')       // 차트에 반영(조회 후)
  const [rankingToggleInput, setRankingToggleInput] = useState<'브랜드' | '회사'>('브랜드') // 셀렉트 바인딩(조회 전)
  // 차트 2 막대: 범례에서 숨긴 브랜드/회사 key (rank-slot 모델이라 chart.js 기본 dataset 토글 대신 직접 관리)
  const [hiddenRankKeys, setHiddenRankKeys] = useState<Set<string>>(new Set())
  // 브랜드↔회사 전환 시 숨김 초기화 (키 체계가 달라 잔존 시 오작동) — Adjust state during render
  const [lastRankToggle, setLastRankToggle] = useState(rankingToggle)
  if (rankingToggle !== lastRankToggle) {
    setLastRankToggle(rankingToggle)
    setHiddenRankKeys(new Set())
  }
  const [lvTop5Key, setLvTop5Key] = useState('class')          
  const [lvTop5KeyInput, setLvTop5KeyInput] = useState('class') 
  // 차트 8 sub-class 선택 (Level 변경 시 default_value로 자동 리셋)
  const [lvSubValue, setLvSubValue] = useState<string | null>(null)          
  const [lvSubValueInput, setLvSubValueInput] = useState<string | null>(null) 
  const [hiddenLvKeys, setHiddenLvKeys] = useState<Set<string>>(new Set())
  const [custTargetIdx, setCustTargetIdx] = useState(0)          
  const [custTargetIdxInput, setCustTargetIdxInput] = useState(0) 

  //   Input(컨트롤 바인딩) → 조회 클릭 → Applied(차트 슬라이싱) 복사
  //   단위(period) 변경 시 from/to 자동 초기화
  // ⚠️ from/to 초기값은 절대 하드코딩 금지 — 응답 도착 후 PDF 명세대로 sync (sliceLastN 헬퍼)
  // 차트 1: Market Size & Growth (IQVIA 브랜드는 quarterly 디폴트, 기간 5년 = 60개월)
  const [w1PeriodInput, setW1PeriodInput] = useState(initialPeriod)
  const [w1FromInput, setW1FromInput] = useState('')
  const [w1ToInput, setW1ToInput] = useState('')
  const [w1Period, setW1Period] = useState(initialPeriod)
  const [w1From, setW1From] = useState('')
  const [w1To, setW1To] = useState('')

  // 차트 2: HHI + 경쟁 순위 — 백엔드가 yearly 데이터만 제공 → default yearly, 기간 5년 (5 point)
  const [w2PeriodInput, setW2PeriodInput] = useState('yearly')
  const [w2FromInput, setW2FromInput] = useState('')
  const [w2ToInput, setW2ToInput] = useState('')
  const [, setW2Period] = useState('yearly')   // 차트 2 yearly 고정 — 값은 안 읽고 applyW2/reset에서 세팅만
  const [w2From, setW2From] = useState('')
  const [w2To, setW2To] = useState('')

  // 차트 5 / 신규 차트(분석 Level별)는 AnalysisLevelChart 컴포넌트 내부에서 level/channel/기간 상태 자체 관리

  // "조회" 클릭 핸들러 — Input → Applied 복사 (chart 1/2/6/7/8)
  const applyW1 = () => { setW1Period(w1PeriodInput); setW1From(w1FromInput); setW1To(w1ToInput) }
  const applyW2 = () => { setW2Period(w2PeriodInput); setW2From(w2FromInput); setW2To(w2ToInput); setRankingToggle(rankingToggleInput) }
  // applyW4: 차트 6은 w4Window(1y~5y) state 변경이 즉시 데이터 반영 → 별도 apply 함수 불필요
  const applyW7 = () => { setW7Period(w7PeriodInput); setW7From(w7FromInput); setW7To(w7ToInput); setCustTargetIdx(custTargetIdxInput) }
  const applyW8 = () => { setW8Period(w8PeriodInput); setW8From(w8FromInput); setW8To(w8ToInput); setLvTop5Key(lvTop5KeyInput); setLvSubValue(lvSubValueInput) }

  // "새로고침" 클릭 핸들러는 각 차트 onClick에 inline으로 — sync 직후 상태로 복귀 (하드코딩 금지)

  // 차트 6(w4): Market Contribution — 기간설정 UI
  // 1차 토글 (1년/2년/3년/4년/5년): growth_contribution.windows.{1y..5y} 데이터 driver (✅ 백엔드 지원)
  // 2차 (월별/분기별/년별 + DatePicker): 명세서 "기획 미확정" — UI만 유지, 데이터 영향 없음
  const [w4Window, setW4Window] = useState<'1y' | '2y' | '3y' | '4y' | '5y'>('5y')
  const [w4PeriodInput, setW4PeriodInput] = useState('yearly')
  const [w4FromInput, setW4FromInput] = useState('')
  const [w4ToInput, setW4ToInput] = useState('')

  // 차트 7: 주요 고객별 Top5 (IQVIA 브랜드는 quarterly 디폴트, 기간 10 point)
  const [w7PeriodInput, setW7PeriodInput] = useState(initialPeriod)
  const [w7FromInput, setW7FromInput] = useState('')
  const [w7ToInput, setW7ToInput] = useState('')
  const [w7Period, setW7Period] = useState(initialPeriod)
  const [w7From, setW7From] = useState('')
  const [w7To, setW7To] = useState('')

  // 차트 8: Level Top5 (IQVIA 브랜드는 quarterly 디폴트, 기간 10 point)
  const [w8PeriodInput, setW8PeriodInput] = useState(initialPeriod)
  const [w8FromInput, setW8FromInput] = useState('')
  const [w8ToInput, setW8ToInput] = useState('')
  const [w8Period, setW8Period] = useState(initialPeriod)
  const [w8From, setW8From] = useState('')
  const [w8To, setW8To] = useState('')

  // IQVIA 출처 시 "월별" 옵션 숨김 (수정사항_20260526)
  const periodOptionsForChart = sourceToggle === 'IQVIA'
    ? PERIOD_MONTHLY.filter(o => o.value !== 'monthly')
    : PERIOD_MONTHLY
  const currentDefaultPeriod = sourceToggle === 'IQVIA' ? 'quarterly' : 'monthly'

  // React 공식 패턴: "Adjusting state during a render" — useEffect로 setState하면 cascading 렌더 룰 위반
  // (https://react.dev/learn/you-might-not-need-an-effect — Storing information from previous renders)
  // 1) 브랜드(productName) 변경 시 supportedSources/marketDef 리셋
  const [lastProductName, setLastProductName] = useState(productName)
  if (productName !== lastProductName) {
    setLastProductName(productName)
    setSupportedSources(new Set(['UBIST', 'IQVIA']))
  }
  // 2) sourceToggle 변경 시: 차트 기간 단위 + from/to를 새 출처 기본값으로 리셋 (Input/Applied 모두)
  //    UBIST=월별 / IQVIA=분기별. 출처가 바뀌면 API·데이터 범위가 통째로 바뀌므로 기본 단위·범위로 복귀.
  //    ⚠️ 이전엔 IQVIA로 갈 때 monthly→quarterly만 보정 → IQVIA→UBIST 복귀 시 분기별·IQVIA 날짜가 잔존하던 버그.
  const [lastSourceForCorrection, setLastSourceForCorrection] = useState(sourceToggle)
  if (sourceToggle !== lastSourceForCorrection) {
    setLastSourceForCorrection(sourceToggle)
    setW1PeriodInput(currentDefaultPeriod); setW1Period(currentDefaultPeriod)
    // 차트 2는 yearly 고정이라 출처 기간 리셋 대상 아님 (건드리면 datepicker mode가 yearly에서 벗어남)
    setW7PeriodInput(currentDefaultPeriod); setW7Period(currentDefaultPeriod)
    setW8PeriodInput(currentDefaultPeriod); setW8Period(currentDefaultPeriod)
    setHasSyncedDataRange(false)   // from/to를 새 출처 데이터 범위로 재동기화 (아래 sync 블록 재실행)
  }

  const view = analyzeTab === 'Competitive Dynamics' ? 'competitive_dynamics' : 'market_landscape'
  const currentMarketResult = (
    getCachedResult(causeKey(productName, sourceToggle, view, 'sales', appliedFilterKey))
    ?? findBrandSalesResult(productName, view)
  )
  const activeMarketMeta = currentMarketResult?.market_meta
  // 브랜드 활동 API(/brand/*)의 filters.atc4 필수 값 — dynamic 응답 market_meta.atc_codes에서 추출
  const atcCodesLive = currentMarketResult?.market_meta?.atc_codes ?? []
  // 한 번 얻은 값을 sticky state로 유지 (key={productName} 리마운트라 브랜드별 격리, atc_codes는 source 무관).
  const [atcSticky, setAtcSticky] = useState<string[]>([])
  if (atcCodesLive.length > 0 && atcSticky.join(',') !== atcCodesLive.join(',')) setAtcSticky(atcCodesLive)
  const atcCodes = atcCodesLive.length > 0 ? atcCodesLive : atcSticky
  const activeMeta = currentMarketResult?.market_meta
  const strategicView = activeMeta?.view === 'strategic_ml' || activeMeta?.view === 'strategic_cd'
    ? activeMeta.view
    : null
  const fallbackMarket = activeMeta?.view_source_id?.trim() && activeMeta.market_name?.trim()
    && strategicView
    ? { viewKind: strategicView, marketId: activeMeta.view_source_id.trim(), marketName: activeMeta.market_name.trim() }
    : null
  const strategicMarketsLive = strategicView
    ? readStrategicInterestMarkets(productName, strategicView, fallbackMarket)
    : []
  const strategicMarketsLiveKey = strategicMarketsLive.map(market => `${market.viewKind}:${market.marketId}:${market.marketName}`).join('|')
  const [strategicMarketsSticky, setStrategicMarketsSticky] = useState<readonly StrategicInterestMarket[]>([])
  const strategicMarketsStickyKey = strategicMarketsSticky.map(market => `${market.viewKind}:${market.marketId}:${market.marketName}`).join('|')
  if (strategicMarketsLiveKey && strategicMarketsLiveKey !== strategicMarketsStickyKey) {
    setStrategicMarketsSticky(strategicMarketsLive)
  }
  const strategicMarkets = strategicMarketsLive.length > 0 ? strategicMarketsLive : strategicMarketsSticky
  const marketDef = appliedFilters.atc4.length > 0
    ? appliedFilters.atc4.join(', ')
    : atcCodes.join(', ')
  const causeFor = (m: 'sales' | 'volume'): CauseData | undefined => {
    const v = causeCache[causeKey(productName, sourceToggle, view, toMeasure(m), appliedFilterKey)]
    return v ?? undefined
  }
  const isMeasureLoading = (m: 'sales' | 'volume') =>
    !(causeKey(productName, sourceToggle, view, toMeasure(m), appliedFilterKey) in causeCache)
  const isCauseLoading = isMeasureLoading('sales')

  // 원인분석 진입 시 브랜드 CSD 존재 여부 헬스체크 (/brand/presence) — 브랜드 활동 탭 활성/비활성 결정.
  useEffect(() => {
    fetchBrandPresence(productName).then(setCsdPresent)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // API 호출 — 필요한 measure만 캐싱 (sales는 항상 + 차트별 측정)
  useEffect(() => {
    const needed = new Set(['sales', toMeasure(w1Measure), toMeasure(w5Measure), toMeasure(wMsMeasure), toMeasure(w7Measure), toMeasure(w8Measure)])
    const fKey = buildFilterKey(appliedFilters)
    needed.forEach(measure => {
      const key = causeKey(productName, sourceToggle, view, measure, fKey)
      // 캐시 hit여도 assay 전환 후 supportedSources를 해당 응답 meta로 재동기화
      // (Market Standard가 UBIST-only로 덮은 뒤 JW 복귀 시 IQVIA 비활성 잔존 방지)
      if (key in causeCache) {
        const cached = getCachedResult(key)
        if (cached?.market_meta) applySupportedFromMeta(cached.market_meta)
        return
      }
      const requestedSource = sourceToggle
      fetchCauseResult(productName, requestedSource, view, measure, appliedFilters).then(result => {
        if (!result) {
          setAlertMessage(current => current || DYNAMIC_MARKET_ERROR_MESSAGE)
          return
        }
        const mm = result.market_meta
        const actualSource = result.source
        const storeSource = (actualSource === 'UBIST' || actualSource === 'IQVIA') ? actualSource : requestedSource
        const storeKey = causeKey(productName, storeSource, view, measure, fKey)
        setCauseCache(prev => ({ ...prev, [storeKey]: result.data }))
        applySupportedFromMeta(mm)
        if (actualSource && actualSource !== requestedSource && (actualSource === 'UBIST' || actualSource === 'IQVIA')) {
          setSourceToggle(actualSource)
        }
      }).catch(error => {
        setAlertMessage(current => current || dynamicMarketErrorMessage(error))
      })
    })
  }, [productName, sourceToggle, view, appliedFilters, w1Measure, w5Measure, wMsMeasure, w7Measure, w8Measure, toMeasure, causeCache])

  // ---- 차트 데이터 계산 ----

  const sales = causeFor('sales')  // 측정 토글 없는 차트(KPI/2/3/4/6) 기준
  const kpi = sales?.kpi
  const eiMatrix = sales?.ei_ms_matrix
  // growth_contribution: windows[w4Window]이 있으면 그 데이터, 없으면 최상위(5y 기본) fallback
  const gcBase = sales?.growth_contribution
  const gc = gcBase?.windows?.[w4Window] ?? gcBase
  const brandRank = sales?.brand_ranking_stacked
  const compRank = sales?.company_ranking_stacked
  const srcData = causeFor(w1Measure)?.sources_data                  // 차트1 (market_size_series)
  const hhiSrc = sales?.sources_data                                 // 차트2 HHI (측정 토글 없음 → sales 고정)
  const custComp = causeFor(w7Measure)?.target_customer_competition  // 차트7
  const custViewLen = custComp?.views?.length ?? 0
  if (custViewLen > 0 && custTargetIdx >= custViewLen) setCustTargetIdx(0)
  if (custViewLen > 0 && custTargetIdxInput >= custViewLen) setCustTargetIdxInput(0)
  const lvTop5 = causeFor(w8Measure)?.level_top5_trend               // 차트8

  // ---- 데이터 timeline 추출 + PDF 명세 N개 slice ----
  // PDF v0.8 각 차트 기간 명세:
  //   차트 1·5: 5년 (UBIST 60개월 / IQVIA 20분기 — 단위는 토글이지만 기간 자체는 동일)
  //   차트 2(yearly 고정): 5 point
  //   차트 7·8: 10 point
  //   차트 6(w4): growth_contribution period (5년 전 대비 현재 — 백엔드 고정)
  // ⚠️ 백엔드가 명세보다 더 많이 보내도(예: 64개월) 프론트에서 마지막 N개만 slice
  const mss = sales?.sources_data?.market_size_series ?? []
  const mssPeriods = mss.map(p => p.period)
  // 헬퍼: timeline 배열의 마지막 n개 → { from, to }
  // IQVIA raw가 "YYYY-Qn"이어도 from/to는 항상 "YYYY-MM" 형식으로 정규화 (ymStartOf/ymEndOf).
  // 이유: DateRangePicker·filter는 "YYYY-MM" 가정 → 분기 형식 그대로 흘리면 picker disabled/input 표시/filter 비교가 다 깨짐.
  const sliceLastRange = (periods: string[], n: number): { from: string; to: string } | null => {
    if (periods.length === 0) return null
    const sliced = periods.slice(-n)
    return { from: ymStartOf(sliced[0]!), to: ymEndOf(sliced[sliced.length - 1]!) }
  }
  // 차트 1·7·8 글로벌 timeline (mss 기반)
  const w1Range = sliceLastRange(mssPeriods, 60)  // 마지막 5년 = 60개월
  const w7Range = sliceLastRange(mssPeriods, 10)  // 마지막 10 point
  const w8Range = sliceLastRange(mssPeriods, 10)  // 마지막 10 point
  // 차트 2: yearly 5point — 정확히 마지막 5년 (`(lastY-4)-01 ~ lastY-12`)
  // 차트 2: 데이터 최신 기준 5년(60개월) — 시장 데이터 마지막 시점에서 정확히 5년 전부터
  // (lastY-4-01 ~ lastY-12로 calc하면 데이터 최신이 4월이어도 12월로 표시되는 버그)
  const w2Range = sliceLastRange(mssPeriods, 60)
  // 차트 6(w4): 선택된 windows의 period_start/period_end — w4Window 변경에 따라 자동 갱신
  const w4DataFrom = gc?.period_start
  const w4DataTo = gc?.period_end
  // onPeriodInputChange용 default (단위 변경 시 reset할 첫/마지막)
  const gFromEff = w1Range?.from ?? ''
  const gToEff = w1Range?.to ?? ''

  // Adjust state during render — 첫 cause 응답 도착 시 1회만 from/to 동기화 (그 후 사용자 조작 우선)
  if (!hasSyncedDataRange && mssPeriods.length > 0) {
    setHasSyncedDataRange(true)
    // 차트 1: 5년 = 마지막 60개월
    if (w1Range) {
      setW1FromInput(w1Range.from); setW1ToInput(w1Range.to)
      setW1From(w1Range.from); setW1To(w1Range.to)
    }
    // 차트 2: yearly 5 point
    if (w2Range) {
      setW2FromInput(w2Range.from); setW2ToInput(w2Range.to)
      setW2From(w2Range.from); setW2To(w2Range.to)
    }
    // 차트 7·8: 10 point
    if (w7Range) {
      setW7FromInput(w7Range.from); setW7ToInput(w7Range.to)
      setW7From(w7Range.from); setW7To(w7Range.to)
    }
    if (w8Range) {
      setW8FromInput(w8Range.from); setW8ToInput(w8Range.to)
      setW8From(w8Range.from); setW8To(w8Range.to)
    }
    // 차트 6: growth_contribution period (Input만 — Applied state 없음)
    if (w4DataFrom && w4DataTo) {
      setW4FromInput(w4DataFrom); setW4ToInput(w4DataTo)
    }
  }

  // w4Window 변경 시 datepicker 표시도 새 windows의 period로 sync (Adjust state during render 패턴)
  const [lastW4Window, setLastW4Window] = useState(w4Window)
  if (w4Window !== lastW4Window) {
    setLastW4Window(w4Window)
    if (w4DataFrom && w4DataTo) {
      setW4FromInput(w4DataFrom); setW4ToInput(w4DataTo)
    }
  }

  // 차트 1: Market Size & Growth (이중 Y축 라인)
  // 1) raw filter — w1From ~ w1To 범위
  // 2) aggregateByUnit — 표시 단위(월/분기/년)에 맞게 value 합산 + period 라벨 변환
  // ★ raw period가 "YYYY-Qn"(IQVIA)일 수 있어 ymStartOf로 "YYYY-MM" 변환 후 비교.
  //   문자열 직접 비교 시 'Q'(81) > 숫자(48~) 때문에 "2023-Q4" <= "2023-12"가 False가 되어 분기 누락 발생
  const w1Raw = (srcData?.market_size_series ?? []).filter(p => {
    const pYM = ymStartOf(p.period)
    return (!w1From || pYM >= w1From) && (!w1To || pYM <= w1To)
  })
  const w1Series = aggregateByUnit(w1Raw, w1Period as PeriodUnit)
  // 성장률 라인 — API 응답값 mom_growth_pct 직접 사용 (프론트 계산/집계 없음, yoy_growth_pct 미사용).
  //   출처별 라벨: UBIST=CMGR / IQVIA=CQGR (출처 토글 시 API 자체가 바뀌므로 sourceToggle로 분기).
  const growthLabel = sourceToggle === 'IQVIA' ? 'CQGR' : 'CMGR'
  const yoyData: (number | null)[] = w1Series.map(p => p.mom_growth_pct ?? null)
  const w1ChartData = {
    labels: w1Series.map(p => p.period),
    datasets: [
      {
        // PDF v0.8: Y축은 단위 환산 정수 (매출: /1e8 억 / 처방량: /1e4 만). 툴팁은 raw 값 그대로
        // raw가 분기 단위(IQVIA)면 "분기간 시장 규모", 월 단위면 "월간 시장 규모"
        label: mssPeriods[0]?.charAt(5) === 'Q' ? '분기간 시장 규모' : '월간 시장 규모',
        data: w1Series.map(p => toYUnit(p.value, w1Measure)),
        yAxisID: 'y' as const,
        fill: true,
        backgroundColor: '#E5F6FC',
        borderColor: 'transparent',  // 하늘색 테두리 선 제거 — 면적만 표시
        borderWidth: 0,
        tension: 0.3,
        pointRadius: 0,
        pointHoverRadius: 4,  // hover 시 해당 점 동그라미 (연간 성장률 점과 동일: chart.js 기본 hoverRadius 4 / borderWidth 1)
        pointHoverBackgroundColor: '#ffffff',
        pointHoverBorderColor: '#00A9E5',
        pointHoverBorderWidth: 1,
        order: 1,  // 아래 레이어 (chart.js: order 큰 게 아래)
      },
      {
        label: growthLabel,
        // API mom_growth_pct 직접 사용 (출처별 CMGR/CQGR)
        data: yoyData,
        yAxisID: 'y1' as const,
        borderColor: '#4472C4',
        backgroundColor: '#4472C4',  // 범례/툴팁 동그라미 채움
        borderWidth: 2,
        fill: false,
        tension: 0,  // 직선 (곡선 X)
        pointRadius: 0,
        pointHoverRadius: 4,  // hover 시 채운 동그라미
        pointHoverBackgroundColor: '#4472C4',
        pointHoverBorderColor: '#ffffff',
        pointHoverBorderWidth: 1,
        spanGaps: true,
        order: 0,  // 위 레이어 — 면적에 가려지지 않도록
      },
    ],
  }

  // 엑셀 핸들러
  const exportChart1Excel = () => analyzeExcel.exportChart1({ productName, sourceToggle, w1Measure, w1Period, w1Series, yoyData })

  // 차트 2-A: HHI 라인 — 토글에 따라 브랜드 HHI(hhi_series_5y) ↔ 회사 HHI(company_concentration_trend) 분기
  // w2From/w2To 연도 범위로 필터 (수정사항_20260526: 기간 적용)
  const yFrom = w2From.slice(0, 4)
  const yTo = w2To.slice(0, 4)
  // 두 응답 구조를 {year, hhi}[]로 정규화 후 동일 로직 적용 (브랜드는 {year,hhi} / 회사는 {periods[], hhi_values[]})
  const companyHhi = sales?.company_concentration_trend
  const hhiPointsRaw: { year: string; hhi: number }[] = rankingToggle === '회사'
    ? (companyHhi?.periods ?? []).map((p, i) => ({ year: p, hhi: companyHhi?.hhi_values?.[i] ?? 0 }))
    : (hhiSrc?.hhi_series_5y ?? []).map(h => ({ year: h.year.toString(), hhi: h.hhi }))
  const w2HhiFiltered = hhiPointsRaw.filter(h => h.year >= yFrom && h.year <= yTo)
  const hhiAllValues = hhiPointsRaw.map(h => h.hhi).filter(v => Number.isFinite(v) && v > 0)
  const hhiAxis = ((): { min?: number; max?: number; step?: number } => {
    if (hhiAllValues.length === 0) return {}
    const lo = Math.min(...hhiAllValues), hi = Math.max(...hhiAllValues)
    const rawStep = Math.max(hi - lo, 1) / 5
    const mag = 10 ** Math.floor(Math.log10(rawStep))
    const norm = rawStep / mag
    const step = (norm <= 1 ? 1 : norm <= 2 ? 2 : norm <= 2.5 ? 2.5 : norm <= 5 ? 5 : 10) * mag
    let min = Math.floor(lo / step) * step
    let max = Math.ceil(hi / step) * step
   
    if (lo - min < step * 0.15) min -= step
    if (max - hi < step * 0.15) max += step
    return { min, max, step }
  })()
  
  const hhiBandAnnotations = ((): Record<string, AnnotationOptions> => {
    if (hhiAxis.min == null || hhiAxis.max == null) return {}
    const out: Record<string, AnnotationOptions> = {}
    for (const b of HHI_BANDS_META) {
      const yMin = Math.max(b.from, hhiAxis.min)
      const yMax = Math.min(b.to, hhiAxis.max)
      if (yMin >= yMax) continue  // 축 범위 밖(역전) 구간은 그리지 않음
      out[b.key] = {
        type: 'box' as const,
        yMin, yMax,
        backgroundColor: `rgba(${b.rgb},0.16)`,
        borderWidth: 0,
        drawTime: 'beforeDatasetsDraw' as const,
        adjustScaleRange: false,  // 밴드가 Y축 자동 범위를 늘리지 않게 (hhiAxis 고정 유지)
      }
    }
    return out
  })()
  const w2HhiData = {
    labels: w2HhiFiltered.map(h => h.year),
    datasets: [{
      label: rankingToggle === '회사' ? '회사 HHI (시장 집중도)' : 'HHI (시장 집중도)',
      data: w2HhiFiltered.map(h => h.hhi),
      borderColor: '#00A9E5',
      backgroundColor: '#E5F6FC',
      tension: 0.3,
      fill: false,
      pointRadius: 5,
      pointHoverRadius: 7,
    }],
  }

  // 차트 2-B: 경쟁 순위 누적 막대 — w2From/w2To 연도 범위로 필터 (차트 2-A와 같은 yFrom/yTo 재사용)
  const rankData = rankingToggle === '브랜드' ? brandRank : compRank
  const rankYearsAll = rankData?.years ?? []
  const rankYears = rankYearsAll.filter(y => y >= yFrom && y <= yTo)
  // yearly가 응답에서 통째로 빠져도 크래시 없이 빈 배열로 degrade (방어적)
  const rankYearly = rankData?.yearly ?? []
  const rankKeySet = new Set<string>()
  rankYearly.forEach(y => {
    if (y.year < yFrom || y.year > yTo) return  // 필터 범위 밖 연도는 브랜드 목록에도 영향 X
    y.rankings.forEach(r => {
      const k = rankingToggle === '브랜드' ? r.brand : r.company
      if (k) rankKeySet.add(k)
    })
  })
  const rankAttrOf = (key: string) =>
    rankYearly[rankYearly.length - 1]?.rankings?.find(r => (rankingToggle === '브랜드' ? r.brand : r.company) === key) ??
    rankYearly[0]?.rankings?.find(r => (rankingToggle === '브랜드' ? r.brand : r.company) === key)

  const rankSelYear = rankYears[rankYears.length - 1]
  const rankOfKey = (k: string) =>
    rankYearly.find(y => y.year === rankSelYear)?.rankings
      ?.find(r => (rankingToggle === '브랜드' ? r.brand : r.company) === k)?.rank ?? 999
  const rankAllKeys = [...rankKeySet]
  const rankOthersKeys = rankAllKeys.filter(k => rankAttrOf(k)?.is_others === true)
  const rankTargetKey = rankAllKeys.find(k => rankAttrOf(k)?.is_target === true)
  const rankNormalKeys = rankAllKeys
    .filter(k => rankAttrOf(k)?.is_others !== true && rankAttrOf(k)?.is_target !== true)
    .sort((a, b) => rankOfKey(a) - rankOfKey(b))
  const shownRankKeys = selectRankTrackerKeys(rankNormalKeys, rankTargetKey, rankOfKey)
  const rankKeyArr = [...shownRankKeys, ...rankOthersKeys]
  const rankColorMap = new Map<string, string>()
  let compColorIdx = 0
  // ⚠️ 색은 이미 계산된 rankTargetKey/rankOthersKeys로 판정 (rankAttrOf = last→first 폴백이라 신뢰 가능).
  //   이전엔 rankYearly[0](첫 연도)에서만 is_others를 찾아, 첫 연도에 '기타' 항목이 없는 시장(예: 아일리아 2021년
  //   실제 브랜드가 100% 다 표시)에선 기타가 undefined → 경쟁사 팔레트(주황)를 받던 버그. 기타는 항상 회색이어야 함.
  const rankOthersSet = new Set(rankOthersKeys)
  rankKeyArr.forEach(key => {
    if (key === rankTargetKey) rankColorMap.set(key, TARGET_COLOR)
    else if (rankOthersSet.has(key)) rankColorMap.set(key, OTHERS_COLOR)
    else rankColorMap.set(key, COMPETITOR_PALETTE[compColorIdx++ % COMPETITOR_PALETTE.length])
  })
  const keyOf = (r: RankItem) => (rankingToggle === '브랜드' ? r.brand : r.company)
  const itemIn = (year: string, key: string): RankItem | undefined =>
    rankYearly.find(y => y.year === year)?.rankings?.find(r => keyOf(r) === key)
  const lastDisplayYear = rankYears[rankYears.length - 1]
  const rankLabelOf = (key: string) =>
    getBrandDisplayLabel(itemIn(lastDisplayYear, key) ?? itemIn(rankYears[0], key), key, rankingToggle === '회사')
  const w2RankData = {
    labels: rankYears,
    datasets: rankKeyArr.map(key => {
      return {
        label: rankLabelOf(key),
        data: rankYears.map(year => {
          const base = itemIn(year, key)?.ms_pct ?? 0
          return base
        }),
        backgroundColor: (rankColorMap.get(key) ?? '#999') + '66',  // opacity 40% (PDF)
        hoverBackgroundColor: rankColorMap.get(key) ?? '#999',      // mouseover 100%
        stack: 'rank',
        hidden: hiddenRankKeys.has(key),
      }
    }),
  }

  // 차트 3: Brand Trajectory Map (E/I Matrix) — 가속/감속(CAGR vs 시장) × 강한/완만(momentum 부호) 4분류
  // eiCategoryKey는 helpers.ts로 이동 — kpi.market_cagr_5y_pct는 인자로 전달
  const marketCagrForEi = kpi?.market_cagr_5y_pct ?? 0
  const eiByCategory = EI_CATEGORIES.map(cat =>
    (eiMatrix?.data ?? []).filter(b => eiCategoryKey(b, marketCagrForEi) === cat.key)
  )
  // 툴팁이 ctx.datasetIndex/dataIndex로 eiByCategory를 재조회하는 패턴은 fragile (인덱스 매핑 어긋나면 b=undefined → 툴팁 빈 값).
  // dataset.data에 브랜드 정보를 직접 박아서 ctx.raw로 바로 추출 — 인덱스 의존 제거.
  const w3BubbleData = {
    datasets: EI_CATEGORIES.map((cat, ci) => ({
      label: cat.label,
      data: eiByCategory[ci]!.map(b => ({
        x: b.share_pct,
        y: b.ei,
        r: Math.max(5, Math.sqrt(b.value_recent / 1e9) * 2.5),
        brand: b.brand,
        share_pct: b.share_pct,
        ei: b.ei,
        cagr_5y_pct: b.cagr_5y_pct,
        momentum_score: b.momentum_score,
      })),
      backgroundColor: cat.color + 'cc',
      borderColor: cat.color,
    })),
  }

  // 차트 4: Growth Contribution & M/S Matrix (4분면 카테고리 색상 + 범례 — 수정사항_20260526)
  const gcMsMatrix = sales?.growth_contribution_ms_matrix
  const gcMsAvg = gcMsMatrix?.ms_avg_pct ?? 0
  const w4BubbleData = {
    datasets: GC_MS_CATEGORIES.map(cat => {
      const items = (gcMsMatrix?.data ?? []).filter(b => gcMsCategoryKey(b, gcMsAvg) === cat.key)
      return {
        label: cat.label,
        data: items.map(b => ({
          x: b.share_pct,
          y: b.contribution_pct,
          r: Math.max(5, Math.sqrt(b.value_recent / 1e9) * 2.5),
          brand: b.brand, // 툴팁용 (chart.js raw로 접근)
          value_recent: b.value_recent,
        })),
        backgroundColor: cat.color + 'cc',
        borderColor: cat.color,
      }
    }),
  }



  // 차트 6: 시장 매출변화 기여도 (플로팅 막대) — 데이터는 풀 원 단위 (Y축 콤마 표시)
  const gcContribs = gc?.by_brand.top_contributors ?? []
  const gcOthers = gc?.by_brand.others_total ?? 0
  const gcStart = gc?.market_start ?? 0
  // 🔧 백엔드가 top_contributors 마지막 항목으로 '기타'를 이미 포함시키는 케이스 있음 (others_total은 0).
  //   우리가 무조건 별도 '기타' 막대를 추가하면 중복(빈 막대) → 백엔드 포함 여부 감지 후 분기.
  const gcBackendHasOthers = gcContribs.length > 0 && gcContribs[gcContribs.length - 1]!.brand === '기타'
  // reduce로 누적 breakpoint 계산 (mutation 없이) — 원 단위
  const gcCumBps = gcContribs.reduce<number[]>(
    (acc, c) => [...acc, (acc[acc.length - 1] ?? gcStart) + c.contribution],
    [gcStart]
  )
  const gcLastCum = gcCumBps[gcCumBps.length - 1] ?? gcStart
  // 백엔드 응답의 period_start/period_end 그대로 사용 (원복)
  const gcLabels = [
    gc?.period_start ?? '시작',
    ...gcContribs.map(c => c.brand),
    ...(gcBackendHasOthers ? [] : ['기타']),
    gc?.period_end ?? '현재',
  ]
  const gcFloatData: (number | [number, number])[] = [
    [0, gcStart],
    ...gcContribs.map((_, i) => [gcCumBps[i]!, gcCumBps[i + 1]!] as [number, number]),
    ...(gcBackendHasOthers ? [] : [[gcLastCum, gcLastCum + gcOthers] as [number, number]]),
    [0, gc?.market_end ?? 0],
  ]
  const gcBgColors = [
    CONTRIB_BASE,  // 시작 (기준)
    ...gcContribs.map(c => (c.contribution >= 0 ? CONTRIB_UP : CONTRIB_DOWN)),
    ...(gcBackendHasOthers ? [] : [gcOthers >= 0 ? CONTRIB_UP : CONTRIB_DOWN]),  // 기타
    CONTRIB_BASE,  // 현재 (기준)
  ]

  // 차트 6-B: 회사별 기여도 — API by_company.top_contributors 직접 사용 (수정사항_20260526). 원 단위 통일
  const ccContribs = gc?.by_company?.top_contributors ?? []
  const ccBackendHasOthers = ccContribs.length > 0 && ccContribs[ccContribs.length - 1]!.company === '기타'
  const ccEnd = gc?.market_end ?? 0
  const ccCumBps = ccContribs.reduce<number[]>(
    (acc, c) => [...acc, (acc[acc.length - 1] ?? gcStart) + c.contribution],
    [gcStart]
  )
  const ccLastCum = ccCumBps[ccCumBps.length - 1] ?? gcStart
  const ccOthers = ccEnd - ccLastCum
  // by_company.others_total이 있으면 그대로 사용 (원 단위), 없으면 시각적 잔여 사용
  const ccOthersValue = gc?.by_company?.others_total ?? ccOthers
  const ccLabels = [
    gc?.period_start ?? '시작',
    ...ccContribs.map(c => c.company),
    ...(ccBackendHasOthers ? [] : ['기타']),
    gc?.period_end ?? '현재',
  ]
  const ccFloatData: (number | [number, number])[] = [
    [0, gcStart],
    ...ccContribs.map((_, i) => [ccCumBps[i]!, ccCumBps[i + 1]!] as [number, number]),
    ...(ccBackendHasOthers ? [] : [[ccLastCum, ccEnd] as [number, number]]),
    [0, ccEnd],
  ]
  const ccBgColors = [
    CONTRIB_BASE,  // 시작 (기준)
    ...ccContribs.map(c => (c.contribution >= 0 ? CONTRIB_UP : CONTRIB_DOWN)),
    ...(ccBackendHasOthers ? [] : [ccOthers >= 0 ? CONTRIB_UP : CONTRIB_DOWN]),  // 기타
    CONTRIB_BASE,  // 현재 (기준)
  ]
  // ⚠️ 차트 6은 호버 무관 opacity 1 고정 (기획자 요청) — 모든 막대(시작/contribs/기타/현재)가
  // 항상 보여주는 누적값이라 dim/highlight 패턴 불필요. gcBgColors/ccBgColors를 그대로 사용.

  // 차트 6 툴팁용 — 시장 성장량(market_growth 우선, 없으면 market_end-market_start) 기준 기타 contribution_pct 계산
  const w4MarketGrowth = gc?.market_growth ?? ((gc?.market_end ?? 0) - (gc?.market_start ?? 0))
  const gcOthersPct = w4MarketGrowth ? (gcOthers / w4MarketGrowth) * 100 : 0
  const ccOthersPct = w4MarketGrowth ? (ccOthersValue / w4MarketGrowth) * 100 : 0
  // PDF 형식 툴팁 (native chart.js) — title 비우고 label 콜백이 multi-line 배열 반환
  // (chart 7과 동일하게 각 줄에 같은 색 동그라미 자동 표시)
  const fmtContribTooltipFor = (
    labels: string[],
    contribs: { contribution_pct?: number; is_jw?: boolean }[],
    othersPct: number,
  ) => ({
    title: () => '',
    label: (ctx: { dataIndex: number; raw: unknown }): string | string[] => {
      const idx = ctx.dataIndex
      const label = labels[idx] ?? ''
      const raw = ctx.raw
      if (!Array.isArray(raw)) return ''
      const start = raw[0] as number
      const end = raw[1] as number
      const fmtWon = (n: number) => fmtBaekman(n)
      if (idx === 0 || idx === labels.length - 1) {
        return [label, `- 매출 : ${fmtWon(end)}`]
      }
      const delta = end - start
      const sign = delta >= 0 ? '+' : ''
      const c = label === '기타' ? null : contribs[idx - 1]
      const titleText = c?.is_jw ? `${label} (JW)` : label
      const pct = label === '기타' ? othersPct : (contribs[idx - 1]?.contribution_pct ?? null)
      const pctStr = pct == null ? '-' : `${pct >= 0 ? '+' : ''}${pct.toFixed(1)}%`
      return [
        titleText,
        `- 성장기여액 : ${sign}${fmtBaekman(delta)}`,
        `- 성장기여도 : ${pctStr}`,
      ]
    },
  })

  // 차트 7: 주요 고객별 Top5 라인 + 도넛 — 자사/경쟁1~5/기타 색상 (PDF)
  // ⚠️ 셀렉트박스 1개만 (차트 8과 다른 구조). target_type="채널" 단일 차원으로 응답:
  const custView = custComp?.views?.[custTargetIdx]
  // ⚠️ filter는 raw monthly 단계에서 (w7From/w7To = "YYYY-MM" 형식과 일치) → 그 후 집계
  const custPeriodsBase = custView?.periods ?? []
  const custBrandsBase = custView?.trend_brands ?? []
  const w7DataFrom = ymStartOf(custPeriodsBase[0] ?? '')
  const w7DataTo = ymEndOf(custPeriodsBase[custPeriodsBase.length - 1] ?? '')
  const custFilteredIndices: number[] = []
  custPeriodsBase.forEach((p, i) => {
    // raw가 "YYYY-Qn"이어도 ymStartOf로 "YYYY-MM" 변환 후 비교 (문자열 직접 비교 시 'Q' > 숫자 버그)
    const pYM = ymStartOf(p)
    if ((!w7From || pYM >= w7From) && (!w7To || pYM <= w7To)) custFilteredIndices.push(i)
  })
  const custPeriodsFiltered = custFilteredIndices.map(i => custPeriodsBase[i]!)
  const custBrandsFiltered = custBrandsBase.map(b => ({
    ...b,
    value_series: custFilteredIndices.map(i => (b.value_series ?? [])[i] ?? 0),
  }))
  const custAggregated = aggregatePeriodsAndItems(custPeriodsFiltered, custBrandsFiltered, w7Period as PeriodUnit)
  const custPeriodsAgg = custAggregated.periods
  const custBrandsAgg = custAggregated.items
  // 정렬 규칙 (사용자 요청): rank 오름차순 + target이 5등 이내면 1~5위 (target 포함), 6등 이상이면 top5 + target 추가, 마지막 '기타'
  const custTrendBrands = (() => {
    const all = custBrandsAgg
    const isOthers = (b: typeof all[number]) => b.brand === '기타'
    const others = all.find(isOthers)
    const ranked = [...all.filter(b => !isOthers(b))].sort((a, b) => (a.rank ?? 999) - (b.rank ?? 999))
    const target = ranked.find(b => b.is_target === true)
    const targetRank = target?.rank ?? 999
    const head = !target || targetRank <= 5
      ? ranked.slice(0, 5)  // 1~5위 (target은 rank 기준 자동 포함)
      : [...ranked.filter(b => b.is_target !== true).slice(0, 5), target]  // top5 + target
    return others ? [...head, others] : head
  })()
  let custCompIdx = 0
  const custBrandColor = new Map(custTrendBrands.map(b => {
    const c = b.is_target ? TARGET_COLOR
      : b.brand === '기타' ? OTHERS_COLOR
      : COMPETITOR_PALETTE[custCompIdx++ % COMPETITOR_PALETTE.length]
    return [b.brand, c]
  }))
  // 차트 7 기간 슬라이싱 — 위에서 raw filter + 집계 모두 완료. 여기선 인덱스 그대로 사용
  const w7PeriodsAll = custPeriodsAgg
  const w7IdxMap = w7PeriodsAll.map((p, i) => ({ p, i }))
  const w7Periods = w7IdxMap.map(x => x.p)
  const w7Idxs = w7IdxMap.map(x => x.i)
  const w5LineData = {
    labels: w7Periods,
    datasets: custTrendBrands.map(b => {
      const color = custBrandColor.get(b.brand) ?? OTHERS_COLOR
      // 라인은 항상 풀 컬러 — hover 시 해당 x의 점들만 활성
      return {
        label: b.brand,
        // PDF v0.8: Y축은 단위 환산 (툴팁은 b.value_series[origIdx] raw 그대로 사용)
        data: w7Idxs.map(i => {
          const v = b.value_series[i]
          return v == null ? null : toYUnit(v, w7Measure)
        }),
        borderColor: color,
        backgroundColor: color,
        fill: false,
        tension: 0.3,
        pointRadius: 3,
        pointBackgroundColor: color,
        pointBorderColor: color,
      }
    }),
  }
  // 도넛(M/S)도 라인과 동일한 custTrendBrands 정렬 사용 — top5(+target)+기타
  // 백엔드 composition은 brand→pct 매핑 lookup으로만 사용, custTrendBrands에 없는 brand들의 pct는 '기타'에 합산
  const compositionMap = new Map((custView?.composition ?? []).map(c => [c.brand, c.pct]))
  const chartBrandNames = new Set(custTrendBrands.map(b => b.brand))
  const extraOthersPct = (custView?.composition ?? [])
    .filter(c => !chartBrandNames.has(c.brand) && c.brand !== '기타')
    .reduce((s, c) => s + c.pct, 0)
  const w5DoughnutData = {
    labels: custTrendBrands.map(b => b.brand),
    datasets: [{
      data: custTrendBrands.map(b => {
        const base = compositionMap.get(b.brand) ?? 0
        return b.brand === '기타' ? base + extraOthersPct : base
      }),
      // Default opacity 0.4 / Mouseover 시 opacity 1 (수정사항_20260526)
      backgroundColor: custTrendBrands.map(b => (custBrandColor.get(b.brand) ?? OTHERS_COLOR) + '66'),
      hoverBackgroundColor: custTrendBrands.map(b => custBrandColor.get(b.brand) ?? OTHERS_COLOR),
    }],
  }

  // 차트 8: Level Top5 누적 막대 + 가로 막대 — 자사/경쟁1~5/기타 색상 (PDF)
  // Level 셀렉트 노출 조건: ① by_level[key].empty === false 이고 ② 'Brand' 레벨이 아닐 것
  // (Brand 레벨은 전 브랜드에서 사용하지 않기로 결정됨 — 백엔드 응답에 와도 강제 숨김)
  const isBrandLevel = (l: { key: string; label: string }) => l.key === 'Brand' || l.label === 'Brand'
  const lvValidKeys = (lvTop5?.available_levels ?? [])
    .filter(l => !lvTop5?.by_level?.[l.key]?.empty && !isBrandLevel(l))
    .map(l => l.key)
  // Adjust state during render: 현재 lvTop5Key가 노출 목록에 없으면 default_level 또는 첫 항목으로 자동 보정
  if (lvValidKeys.length > 0 && !lvValidKeys.includes(lvTop5Key)) {
    const defKey = (lvTop5 as { default_level?: string } | undefined)?.default_level
    const next = defKey && lvValidKeys.includes(defKey) ? defKey : lvValidKeys[0]!
    if (next !== lvTop5Key) setLvTop5Key(next)
  }
  const lvD = lvTop5?.by_level?.[lvTop5Key]
  // Sub 옵션도 응답이 직접 제공: all_options + default_option (없으면 values에서 추출하는 기존 로직 fallback)
  const lvSubOptions: string[] = lvD?.all_options
    ?? (lvD?.values?.map(v => v.value).filter((v): v is string => !!v) ?? [])
  if (lvD && lvSubOptions.length > 0 && (lvSubValue == null || !lvSubOptions.includes(lvSubValue))) {
    const def = lvD.default_option
      ?? lvD.default_value
      ?? lvD.values?.find(v => v.is_default)?.value
      ?? lvSubOptions[0]!
    if (def !== lvSubValue) setLvSubValue(def)
  }
  // 선택된 sub의 brands_in_value 데이터는 여전히 values에서 lookup (옵션과 데이터가 분리된 응답 구조)
  const lvSelectedSub = lvD?.values?.find(v => v.value === lvSubValue) ?? lvD?.values?.[0]
  const isFilteredMemberScopeUnavailable = lvTop5?.reason === 'filtered_member_scope_unavailable'
  const levelTop5TerminalState = isFilteredMemberScopeUnavailable
    ? {
        brands_in_value: [],
        data_quality: { available: false, reason: lvTop5.reason },
      }
    : lvSelectedSub
  const lvTop5EmptyMessage = levelTop5EmptyMessage(levelTop5TerminalState)
  // ── 차트 8 컨트롤 Input(조회 전) — 드롭다운 옵션/값은 "입력 레벨" 기준. 차트는 위 applied 기준. ──
  if (lvValidKeys.length > 0 && !lvValidKeys.includes(lvTop5KeyInput)) {
    const defKeyIn = (lvTop5 as { default_level?: string } | undefined)?.default_level
    const nextIn = defKeyIn && lvValidKeys.includes(defKeyIn) ? defKeyIn : lvValidKeys[0]!
    if (nextIn !== lvTop5KeyInput) setLvTop5KeyInput(nextIn)
  }
  const lvDInput = lvTop5?.by_level?.[lvTop5KeyInput]
  const lvSubOptionsInput: string[] = lvDInput?.all_options
    ?? (lvDInput?.values?.map(v => v.value).filter((v): v is string => !!v) ?? [])
  if (lvDInput && lvSubOptionsInput.length > 0 && (lvSubValueInput == null || !lvSubOptionsInput.includes(lvSubValueInput))) {
    const defIn = lvDInput.default_option ?? lvDInput.default_value ?? lvDInput.values?.find(v => v.is_default)?.value ?? lvSubOptionsInput[0]!
    if (defIn !== lvSubValueInput) setLvSubValueInput(defIn)
  }
  const lvHideKey = `${lvTop5Key}|${lvSubValue ?? ''}`
  const [lastLvHideKey, setLastLvHideKey] = useState(lvHideKey)
  if (lvHideKey !== lastLvHideKey) {
    setLastLvHideKey(lvHideKey)
    setHiddenLvKeys(new Set())
  }
  const lvPeriodsLen = lvD?.periods_10pt?.length ?? 10
  // 1단계: 응답 series 길이 정규화 (10pt 보장)
  const lvBrandsAll = (lvSelectedSub?.brands_in_value ?? []).map(b => {
    const vs = b.value_series_10pt ?? []
    const ms = b.ms_series_10pt ?? []
    return {
      ...b,
      value_series_10pt: vs.length > lvPeriodsLen ? vs.slice(-lvPeriodsLen) : vs,
      ms_series_10pt: ms.length > lvPeriodsLen ? ms.slice(-lvPeriodsLen) : ms,
    }
  })
  const lvBrandsRaw = (() => {
    const others = lvBrandsAll.find(b => b.is_others)
    const normal = lvBrandsAll.filter(b => !b.is_others)
    const ranked = [...normal].sort((a, b) => (a.rank ?? 1e9) - (b.rank ?? 1e9))
    const target = ranked.find(b => b.is_target || b.brand === productName)
    const targetRank = target?.rank ?? 1e9
    const shown = (!target || targetRank <= 5)
      ? ranked.slice(0, 5)                                         
      : [...ranked.filter(b => b !== target).slice(0, 5), target]  
    const shownSet = new Set(shown)
    const hidden = normal.filter(b => !shownSet.has(b))
    if (hidden.length === 0) return others ? [...shown, others] : shown
    const len = others?.value_series_10pt.length || shown[0]?.value_series_10pt.length || 0
    const addArr = (base: number[], add: number[]) => base.map((v, i) => v + (add[i] ?? 0))
    let ov = others?.value_series_10pt.slice() ?? new Array<number>(len).fill(0)
    let om = others?.ms_series_10pt.slice() ?? new Array<number>(len).fill(0)
    for (const h of hidden) { ov = addArr(ov, h.value_series_10pt); om = addArr(om, h.ms_series_10pt) }
    const mergedOthers = {
      brand: others?.brand ?? '기타',
      company: others?.company,
      rank: others?.rank,
      is_jw: others?.is_jw,
      is_target: false,
      is_others: true,
      value_recent_100m: others?.value_recent_100m ?? 0,
      value_series_10pt: ov,
      ms_series_10pt: om,
      data_quality: others?.data_quality,
    }
    return [...shown, mergedOthers]
  })()
  // 2단계: 차트 8 기간 슬라이싱 — raw monthly 단계에서 w8From/w8To filter (집계 후 filter하면 "2026-Q1" > "2026-04" 비교 버그)
  const w8PeriodsBase = lvD?.periods_10pt ?? []
  // 단위 변경 시 reset할 effective from/to — 자기 timeline(10 point) 기준
  // IQVIA raw "YYYY-Qn" 호환 (DateRangePicker는 "YYYY-MM" 가정)
  const w8DataFrom = ymStartOf(w8PeriodsBase[0] ?? '')
  const w8DataTo = ymEndOf(w8PeriodsBase[w8PeriodsBase.length - 1] ?? '')
  const w8FilteredIndices: number[] = []
  w8PeriodsBase.forEach((p, i) => {
    // raw가 "YYYY-Qn"(IQVIA)일 수 있어 ymStartOf로 "YYYY-MM" 정규화 후 비교
    const pYM = ymStartOf(p)
    if ((!w8From || pYM >= w8From) && (!w8To || pYM <= w8To)) w8FilteredIndices.push(i)
  })
  const w8PeriodsRawFiltered = w8FilteredIndices.map(i => w8PeriodsBase[i]!)
  const lvBrandsRawFiltered = lvBrandsRaw.map(b => ({
    ...b,
    value_series_10pt: w8FilteredIndices.map(i => b.value_series_10pt[i] ?? 0),
    ms_series_10pt: w8FilteredIndices.map(i => b.ms_series_10pt[i] ?? 0),
  }))
  // 3단계: w8Period 단위 집계 (sum) — 차트 5·7과 동일 정책 (기획자 확인: "합산")
  const w8Aggregated = aggregatePeriodsAnd10ptItems(w8PeriodsRawFiltered, lvBrandsRawFiltered, w8Period as PeriodUnit)
  const w8Periods = w8Aggregated.periods
  const lvBrands = w8Aggregated.items
  type LvBrand = (typeof lvBrands)[number]
  const lvIsOthers = (brand: LvBrand) => isLevelTop5Others(brand)
  let lvCompIdx = 0
  const lvBrandColor = new Map(lvBrands.map(b => {
    // target 보정: 백엔드 is_target 없을 때도 productName과 일치하면 TARGET_COLOR 강제
    const isTarget = b.is_target || b.brand === productName
    const c = isTarget ? TARGET_COLOR
      : lvIsOthers(b) ? OTHERS_COLOR
      : COMPETITOR_PALETTE[lvCompIdx++ % COMPETITOR_PALETTE.length]
    return [b.brand, c]
  }))
  const lvColorOf = (b: LvBrand) => lvBrandColor.get(b.brand) ?? OTHERS_COLOR
  const lvVisible = lvBrands.filter(b => !hiddenLvKeys.has(b.brand))
  const lvLastIdx = w8Periods.length - 1
  const lvFixedOrder: LvBrand[] = [...lvVisible].sort((a, b) => {
    const ao = lvIsOthers(a) ? 1 : 0, bo = lvIsOthers(b) ? 1 : 0
    if (ao !== bo) return ao - bo
    return (b.value_series_10pt[lvLastIdx] ?? -Infinity) - (a.value_series_10pt[lvLastIdx] ?? -Infinity)
  })
  const lvSlotsByPeriod: LvBrand[][] = w8Periods.map(() => lvFixedOrder)
  const lvMaxSlots = lvSlotsByPeriod.reduce((m, arr) => Math.max(m, arr.length), 0)
  const w6StackedData = {
    labels: w8Periods,
    datasets: Array.from({ length: lvMaxSlots }, (_, slot) => ({
      label: `lvslot-${slot}`,
      data: w8Periods.map((_, i) => {
        const v = lvSlotsByPeriod[i][slot]?.value_series_10pt[i]
        return v == null ? null : toYUnit(v, w8Measure)
      }),
      backgroundColor: w8Periods.map((_, i) => {
        const b = lvSlotsByPeriod[i][slot]
        return b ? lvColorOf(b) + '66' : 'transparent'
      }),
      hoverBackgroundColor: w8Periods.map((_, i) => {
        const b = lvSlotsByPeriod[i][slot]
        return b ? lvColorOf(b) : 'transparent'
      }),
      stack: 'lv',
    })),
  }
  const lvBrandsRev = [...lvBrands].reverse()
  const w6HBarData = {
    labels: lvBrandsRev.map(b => b.brand),
    datasets: [{
      label: 'M/S (%)',
      data: lvBrandsRev.map(b => b.ms_series_10pt[b.ms_series_10pt.length - 1] ?? 0),
      // Default opacity 0.4 / Mouseover 시 opacity 1 (수정사항_20260526)
      backgroundColor: lvBrandsRev.map(b => (lvBrandColor.get(b.brand) ?? OTHERS_COLOR) + '66'),
      hoverBackgroundColor: lvBrandsRev.map(b => lvBrandColor.get(b.brand) ?? OTHERS_COLOR),
    }],
  }

  const exportChart2Excel = () => analyzeExcel.exportChart2({ productName, sourceToggle, rankingToggle, w2HhiFiltered, rankYears, rankKeyArr, lastDisplayYear, itemIn, rankLabelOf, hiddenNormalKeys: [] })
  const exportChart7Excel = () => analyzeExcel.exportChart7({ productName, sourceToggle, w7Measure, targetName: custView?.target_name ?? '', custTrendBrands, w7Periods, w7Idxs, compositionMap, extraOthersPct })
  const exportChart8Excel = () => analyzeExcel.exportChart8({ productName, sourceToggle, w8Measure, levelLabel: lvTop5?.available_levels?.find(l => l.key === lvTop5Key)?.label ?? lvTop5Key, subValue: lvSubValue ?? '', lvBrands, w8Periods })
  const exportChart9Excel = () => analyzeExcel.exportChart9({ productName, sourceToggle, window: w4Window, gc, gcContribs, gcOthers, gcOthersPct, gcBackendHasOthers, ccContribs, ccOthersValue, ccOthersPct, ccBackendHasOthers })

  // 동적 옵션
  // Level 셀렉트박스 노출 옵션 — empty: true 인 레벨(예: 리바로 Molecule) + Brand 레벨 숨김
  const lvTop5Levels = (lvTop5?.available_levels ?? [])
    .filter(l => !lvTop5?.by_level?.[l.key]?.empty && !isBrandLevel(l))
  const custTargets = custComp?.targets ?? []

  const fmtMarketSize = (raw: number): [string, string] => {
  return [Math.round(raw / 1e6).toLocaleString(), '백만원']
}

  // commonOpts, legendBottom, fmtPeriodKor, fmtEok, fmtFullWon은 utils/chartHelpers로 이동

  // 차트 2/5/7/8 sub 툴팁 — 동적 빌드 (referenceLabel + 토글 따라 변경)
  // 상단 "기준" 표시와 동일 로직으로 데이터 마지막 시점 라벨 추출
  const referenceLabel = (() => {
    if (!gToEff) return ''
    const y = gToEff.slice(0, 4)
    const tail = gToEff.slice(5)
    if (sourceToggle === 'IQVIA') {
      if (tail.startsWith('Q')) return `${y}년 ${tail.slice(1)}분기`
      const m = parseInt(tail)
      return Number.isFinite(m) ? `${y}년 ${Math.ceil(m / 3)}분기` : ''
    }
    const m = parseInt(tail)
    return Number.isFinite(m) ? `${y}년 ${m}월` : ''
  })()
  const tt = buildDynamicChartTooltips({
    referenceLabel,
    rankingToggle,
    m5Label: w5Measure === 'sales' ? '매출' : '처방량',
    m7Label: w7Measure === 'sales' ? '매출' : '처방량',
    m8Label: w8Measure === 'sales' ? '매출' : '처방량',
  })
  // 신규 차트 "주요 고객 분석 레벨별 매출 추이 및 M/S" 툴팁 — 분석 Level별 텍스트를 wMsMeasure 라벨로 빌드
  const ttMs = buildDynamicChartTooltips({
    referenceLabel,
    rankingToggle,
    m5Label: wMsMeasure === 'sales' ? '매출' : '처방량',
    m7Label: w7Measure === 'sales' ? '매출' : '처방량',
    m8Label: w8Measure === 'sales' ? '매출' : '처방량',
  })

  return (
    <AgentChatProvider onAlert={setAlertMessage}>
    <div className={`wrap ${sidebarOpen ? 'open' : 'close'}`}>
      <Sidebar
        pinnedList={MOCK_PINNED_LIST}
        normalList={MOCK_NORMAL_LIST}
        activeChatId={activeChatId}
        onToggleSidebar={() => setSidebarOpen(p => !p)}
        onNewChat={() => navigate('/market/chat')}
        onSelectChat={uid => setActiveChatId(uid)}
        onDeleteModal={() => {}}
        onChangeNameModal={() => {}}
        onPinChat={() => {}}
        onUnpinChat={() => {}}
        hideChatHistory
      />

      <div className="container-wrap dashboard">
        {/* top-navigation — 원인분석·심층분석 공용 (스크롤 내리면 navHidden으로 위로 슬라이드) */}
        <MarketTopNav navHidden={navHidden} onAlertMessage={setAlertMessage} />

        <div
          ref={scrollRef}
          className={`content-wrap scroll-container analyze${navHidden ? ' nav-hidden' : ''}`}
          onScroll={handleContentScroll}
        >
          <div className="content">
            <div className="content-inner">
              <div className="dashboard-inner">

                {/* ── sticky 섹션 타이틀 ── */}
                <section className="status-section">
                  <div className="section-title">
                    <div className="left-wrap">
                      {productName}
                      <div className="inner-tab">
                        <ul>
                          <li>
                            <a href="#" className="on" onClick={e => e.preventDefault()}>원인분석</a>
                          </li>
                          <li>
                            <a href="#" onClick={e => { e.preventDefault(); navigate('/market/deep-analyze', { state: { productName, sources: navState?.sources, generalSources: navState?.generalSources, strategicSources: navState?.strategicSources, assay: assayValue } }) }}>심층분석</a>
                          </li>
                        </ul>
                      </div>
                      <SelectBox
                        wrapperClassName="assay-select"
                        options={ASSAY_OPTIONS}
                        value={assayValue}
                        disabled={analyzeTab === 'Competitive Dynamics' || assayLockedToMarket}
                        onChange={v => {
                          setAssayValue(v === 'market' ? 'market' : 'jw')
                          setAppliedFilters({
                            ...DEFAULT_FILTER_CONTEXT,
                            assayMode: v === 'jw' ? 'jw' : 'market',
                          })
                        }}
                      />
                    </div>
                    <AgentChatTrigger />
                  </div>
                </section>

                {/* ── 탭 + 출처 헤더 (상단 sticky 고정) ── */}
                <section className="analyze-top-sticky">
                  <div className="analyze-top-wrap">
                    <div className="analyze-tab">
                      <ul>
                        {ANALYZE_TABS.map(tab => {
                          // Market Standard 모드에서는 Competitive Dynamics 탭 숨김
                          if (tab === 'Competitive Dynamics' && assayValue === 'market') return null
                          const csdDisabled = tab === '브랜드 활동' && csdPresent === false
                          const isDisabled = DISABLED_TABS.has(tab) || csdDisabled
                          return (
                            <li key={tab}>
                              <a
                                href="#"
                                className={`${analyzeTab === tab ? 'on' : ''}${isDisabled ? ' is-disabled' : ''}`}
                                style={isDisabled ? { opacity: 0.4, cursor: 'not-allowed' } : undefined}
                                title={csdDisabled ? '이 브랜드는 브랜드 활동 데이터를 제공하지 않습니다' : undefined}
                                onClick={e => {
                                  e.preventDefault()
                                  if (isDisabled) return
                                  setAnalyzeTab(tab)
                                  scrollRef.current?.scrollTo({ top: 0 })  // 탭 전환 시 스크롤 최상단
                                }}
                              >{tab}{TAB_KO_SUB[tab] && <span style={{ fontSize: 'calc(1em - 2px)' }}> ({TAB_KO_SUB[tab]})</span>}</a>
                            </li>
                          )
                        })}
                      </ul>
                    </div>
                    <div className="info-right-wrap">
                      <div className="toggle-wrap">
                        <span>출처</span>
                        <SlideToggle<'UBIST' | 'IQVIA'>
                          className="slide-toggle--source"
                          options={(['UBIST', 'IQVIA'] as const).map(s => ({
                            value: s,
                            label: s,
                            disabled: !isSourceSelectable(sourceAvailability[s]),
                            title: sourceAvailabilityTitle(
                              s,
                              sourceAvailability[s],
                              observedSourcePeriods[s.toLowerCase()]
                                ?? (s === sourceToggle ? activeMarketMeta?.selected_brand_latest_period : null),
                            ),
                          }))}
                          value={sourceToggle}
                          onChange={setSourceToggle}
                        />
                      </div>
                      <div className="bx-line" />
                      <div className="bx-info"><span>기준</span>{!sales ? <SkelBar w={72} h={15} inline /> : (() => {
                        // 데이터 마지막 시점(gToEff) → IQVIA는 분기, UBIST는 월
                        // gToEff는 "YYYY-MM"(UBIST) 또는 "YYYY-Qn"(IQVIA) 두 형식 모두 가능
                        if (!gToEff) return '—'
                        const y = gToEff.slice(0, 4)
                        const tail = gToEff.slice(5)
                        if (sourceToggle === 'IQVIA') {
                          // 분기 형식("Q4")이면 그대로, 월 형식("04")이면 분기 환산
                          if (tail.startsWith('Q')) return `${y}년 ${tail.slice(1)}분기`
                          const m = parseInt(tail)
                          return Number.isFinite(m) ? `${y}년 ${Math.ceil(m / 3)}분기` : '—'
                        }
                        const m = parseInt(tail)
                        return Number.isFinite(m) ? `${y}년 ${m}월` : '—'
                      })()}</div>
                      <div className="bx-line" />
                      <div className="bx-info">
                        시장 정의
                        <i className="icon-ex" style={{ position: 'relative', cursor: 'pointer' }}>
                          {marketDef && <div className="chart-tooltip">{marketDef}</div>}
                        </i>
                      </div>
                    </div>
                  </div>
                </section>

                {analyzeTab !== '브랜드 활동' && (
                  <AnalyzeFilterBar
                    productName={productName}
                    source={sourceToggle}
                    measure={toMeasure('sales')}
                    assayMode={assayValue === 'jw' ? 'jw' : 'market'}
                    fallbackAtc4={atcCodes}
                    applied={appliedFilters}
                    onApply={(next) => setAppliedFilters(next)}
                    onReset={() => setAppliedFilters({
                      ...DEFAULT_FILTER_CONTEXT,
                      assayMode: assayValue === 'jw' ? 'jw' : 'market',
                    })}
                  />
                )}

                {analyzeTab === '브랜드 활동' ? (
                  <BrandActivityTab
                    productName={productName}
                    atcCodes={atcCodes}
                    strategicMarkets={strategicMarkets}
                  />
                ) : (
                <section className="chart-section">

                  {/* KPI 카드 */}
                  <div className="card-grid card-grid-status analyze">
                    {(() => {
                      // 라벨은 항상 실제 텍스트, 값(.stat-card-value)만 로딩 시 스켈레톤 바
                      const { loading, noData } = resolveKpiPayloadState(kpi, isCauseLoading)
                      const skel = <span className="stat-card-value-skel chart-skel-shimmer" />
                      const brandCagr = selectBrandCagr(kpi?.brand_cagr_5y_pct, kpi?.brand_cagr_3y_pct)
                      return (
                        <>
                          <article className="stat-card">
                            <p className="stat-card-label">전체 시장 규모</p>
                            <p className="stat-card-value">{loading ? skel : noData ? '—' : kpi?.market_size_recent == null ? '—' : (() => { const [v, u] = fmtMarketSize(kpi.market_size_recent); return <>{v}<span>{u}</span></> })()}</p>
                          </article>
                          <article className="stat-card">
                            <p className="stat-card-label">매출 및 시장 내 M/S</p>
                            <p className="stat-card-value">{loading ? skel : noData || kpi?.target_brand_sales == null || kpi.target_share_pct == null ? '—' : <>{Math.round(kpi.target_brand_sales / 1e6).toLocaleString()}<span>백만원 ({kpi.target_share_pct.toFixed(2)}%)</span></>}</p>
                          </article>
                          <article className="stat-card">
                            <p className="stat-card-label">시장 순위</p>
                            <p className="stat-card-value">{loading ? skel : (() => {
                              // 🆕 496355d: kpi.target_rank 직접 제공 (이전 워크어라운드: ei_ms_matrix에서 추출). fallback 유지
                              const r = kpi?.target_rank ?? eiMatrix?.data.find(b => b.brand === productName)?.rank_overall
                              return r != null ? <>{r}<span>위</span></> : '—'
                            })()}</p>
                          </article>
                          <article className="stat-card stat-card-up" data-brand-cagr-display="exclusive-5y-3y">
                            <p className="stat-card-label">{loading ? <SkelBar w={72} h={12} inline /> : brandCagr.label}</p>
                            <div className="stat-card-flex">
                              <p className="stat-card-value">{loading ? skel : brandCagr.value == null
                                ? '—'
                                : <>{brandCagr.value >= 0 ? '+' : ''}{brandCagr.value.toFixed(2)}<span>%</span></>}
                              </p>
                            </div>
                          </article>
                          {/* <article className="stat-card">
                            <p className="stat-card-label">HHI</p>
                            <p className="stat-card-value">{kpi.hhi_recent.toFixed(0)}</p>
                          </article> */}
                          <article className="stat-card">
                            <p className="stat-card-label">시장 내 경쟁 브랜드 수</p>
                            <p className="stat-card-value">{loading ? skel : noData || kpi?.direct_competition_count == null ? '—' : <>{kpi.direct_competition_count}<span>개</span></>}</p>
                          </article>
                        </>
                      )
                    })()}
                  </div>

                  {/* ===== 차트 1: Market Size & Growth ===== */}
                  <div className="chat-section-title-n">경쟁 시장 전체 Overview</div>
                  <div className="chart-widget chart-widget-full">
                    <div className="chart-widget-header">
                      <div className="chart-widget-title-group">
                        <h2 className="chart-widget-title">Market Size &amp; Growth</h2>
                        <InfoTooltip text={buildMarketSizeGrowthTooltip(
                          w1Measure === 'sales'
                            ? '매출 (UBIST Sales : 처방조제액 / IQVIA NSA : Values LC)'
                            : '처방량 (UBIST Sales : 처방량 / IQVIA NSA : Unit, Dosage Unit, Counting Unit)',
                          growthLabel,
                        )} />
                      </div>
                      <div className="chart-widget-controls">
                        {sourceToggle === 'IQVIA' && w1Measure === 'volume' && (
                          <div className="control-box control-box-select">
                            <ChartSelect options={UNIT_OPTIONS} value={unitMeasure} onChange={v => setUnitMeasure(v as 'unit' | 'dosage_unit' | 'counting_unit')} />
                          </div>
                        )}
                        <div className="control-box control-box-select">
                          <ChartSelect
                            options={periodOptionsForChart}
                            value={w1PeriodInput}
                            onChange={v => onPeriodInputChange(v, setW1PeriodInput, setW1FromInput, setW1ToInput, gFromEff, gToEff)}
                          />
                        </div>
                        <div className="control-box control-box-date">
                          <DateRangePicker
                            from={w1FromInput} to={w1ToInput}
                            mode={w1PeriodInput as 'monthly' | 'quarterly' | 'yearly'}
                            minYM={gFromEff} maxYM={gToEff}
                            onFromChange={setW1FromInput} onToChange={setW1ToInput}
                          />
                        </div>
                        <button type="button" className="btn-date-search" onClick={applyW1}>조회</button>
                        <button type="button" className="btn-date-reset"
                          onClick={() => {
                            setW1PeriodInput(currentDefaultPeriod)
                            setW1FromInput(w1Range?.from ?? ''); setW1ToInput(w1Range?.to ?? '')
                            setW1Period(currentDefaultPeriod)
                            setW1From(w1Range?.from ?? ''); setW1To(w1Range?.to ?? '')
                          }}
                        >새로고침</button>
                        <div className="in-sepa-line" />
                        <MeasureToggle measure={w1Measure} onChange={setW1Measure} />
                        <div className="in-sepa-line" />
                        <button type="button" className="btn-excel-down" onClick={exportChart1Excel} disabled={isMeasureLoading(w1Measure)}>엑셀다운로드</button>
                      </div>
                    </div>
                    <div className="chart-widget-body">
                      <div style={{ height: 429 }}>
                        {isMeasureLoading(w1Measure) ? <ChartSkeleton rightAxis legendItems={2} /> : <Line
                          data={w1ChartData}
                          options={{
                            ...commonOpts,
                            interaction: { mode: 'index', intersect: false },  // hover 시 점 활성화(pointHoverRadius 표시)
                            plugins: {
                              // 범례 클릭으로 dataset toggle 비활성 (기획자 요청)
                              legend: { ...legendBottom, onClick: () => {} },
                              tooltip: {
                                mode: 'index',
                                intersect: false,
                                callbacks: {
                                  title: items => fmtPeriodKor(items[0]?.label ?? ''),
                                  label: ctx => {
                                    const p = w1Series[ctx.dataIndex]
                                    if (!p) return ''
                                    // p.value는 raw 값 (매출=원, 처방량=처방건수) — 측정에 맞춰 단위 분기
                                    if (ctx.datasetIndex === 0) {
                                      const valStr = w1Measure === 'sales' ? fmtBaekman(p.value) : Math.round(p.value).toLocaleString()
                                      return `시장 규모 : ${valStr}`
                                    }
                                    // 라인과 같은 yoyData(mom_growth_pct) 사용
                                    const yoy = yoyData[ctx.dataIndex]
                                    return yoy == null ? '' : `${growthLabel} : ${formatMarketGrowthPct(yoy)}`
                                  },
                                },
                              },
                            },
                            scales: {
                              x: { grid: { display: false }, ticks: { maxTicksLimit: 12 } },
                              y: {
                                position: 'left',
                                title: { display: true, text: yTitleFor(w1Measure) },
                                ticks: { callback: v => Number(v).toLocaleString() },
                              },
                              y1: {
                                position: 'right',
                                grid: { drawOnChartArea: false },
                                title: { display: true, text: growthLabel },
                                // chart.js 자동 ticks의 누적 부동소수점 오차(7.7000000000000002%) 차단
                                // 정수면 그대로, 소수면 1자리 — IEEE 754 / Number.EPSILON 패턴
                                ticks: { callback: v => {
                                  const n = Number(v)
                                  return `${Number.isInteger(n) ? n : n.toFixed(1)}%`
                                } },
                              },
                            },
                          }}
                        />}
                      </div>
                    </div>
                  </div>

                  {/* ===== 차트 2: HHI + 경쟁 순위 Tracker ===== */}
                  {/* 외부 섹션 타이틀 삭제 (수정사항_20260526) */}
                  <div className="chart-widget chart-widget-full">
                    <div className="chart-widget-header">
                      <div className="chart-widget-title-group">
                        <h2 className="chart-widget-title">시장 집중도 추이 및 경쟁 순위 Tracker</h2>
                      </div>
                      <div className="chart-widget-controls">
                        {/* 브랜드/회사 셀렉트 — 년별 왼쪽 (기획자 요청: 우측 토글 → 좌측 셀렉박스) */}
                        <div className="control-box control-box-select">
                          <SelectBox
                            size="sm"
                            weight={400}
                            options={[{ value: '브랜드', label: '브랜드' }, { value: '회사', label: '회사' }]}
                            value={rankingToggleInput}
                            onChange={v => setRankingToggleInput(v as '브랜드' | '회사')}
                          />
                        </div>
                        <div className="in-sepa-line" />
                        <div className="control-box control-box-select">
                          {/* 차트 2: 백엔드 yearly 데이터만 제공 → 셀렉트박스 기능 비활성 ("년별" 텍스트만, 화살표/popup 없음) */}
                          <div className="ds-select ds-select--sm ds-select--w400" style={{ display: 'inline-block' }}>
                            <span
                              className="ui-selectmenu-button ui-button ui-widget ui-selectmenu-button-closed"
                              style={{ cursor: 'default' }}
                            >
                              <span className="ui-selectmenu-text">년별</span>
                              {/* chevron span 의도적 제거 — 화살표 노출 X (클릭 유도 차단) */}
                            </span>
                          </div>
                        </div>
                        <div className="control-box control-box-date">
                          <DateRangePicker
                            from={w2FromInput} to={w2ToInput}
                            mode={w2PeriodInput as 'monthly' | 'quarterly' | 'yearly'}
                            minYM={gFromEff} maxYM={gToEff}
                            onFromChange={setW2FromInput} onToChange={setW2ToInput}
                          />
                        </div>
                        <button type="button" className="btn-date-search" onClick={applyW2}>조회</button>
                        <button type="button" className="btn-date-reset"
                          onClick={() => {
                            // 차트 2는 yearly 고정
                            setW2PeriodInput('yearly')
                            setW2FromInput(w2Range?.from ?? ''); setW2ToInput(w2Range?.to ?? '')
                            setW2Period('yearly')
                            setW2From(w2Range?.from ?? ''); setW2To(w2Range?.to ?? '')
                            setRankingToggleInput('브랜드'); setRankingToggle('브랜드')
                          }}
                        >새로고침</button>
                        <div className="in-sepa-line" />
                        <button type="button" className="btn-excel-down" onClick={exportChart2Excel} disabled={isCauseLoading}>엑셀다운로드</button>
                      </div>
                    </div>
                    <div className="chart-widget-body">
                      <div className="chart-layout-half-split">
                        <div className="chart-split-box">
                          <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', gap: 6, fontSize: 16, fontWeight: 600, marginBottom: 4 }}>
                            <span>HHI (시장 집중도)</span>
                            <InfoTooltip text={tt.hhi} />
                          </div>
                          <div style={{ height: 429 }}>
                          {isCauseLoading ? <ChartSkeleton legendItems={1} /> : <Line
                            data={w2HhiData}
                            options={{
                              ...commonOpts,
                              plugins: {
                                // 범례 클릭 toggle 비활성 (기획자 요청)
                                // HHI 라인 + 구간 밴드 3개(경쟁 시장/부분 집중/과속·독과점)를 범례에 함께 노출
                                legend: {
                                  ...legendBottom,
                                  onClick: () => {},
                                  labels: {
                                    ...legendBottom.labels,
                                    generateLabels: (): LegendItem[] => [
                                      // HHI 라인 — 흰 속 + 라인색 링
                                      { text: 'HHI (시장 집중도)', fillStyle: '#fff', strokeStyle: HHI_LINE_COLOR, lineWidth: 2, hidden: false, index: 0 } as LegendItem,
                                      // 구간 밴드 — 채운 동그라미(0.4, 배경보다 진하게 식별)
                                      ...HHI_BANDS_META.map((b, i) => ({
                                        text: b.label,
                                        fillStyle: `rgba(${b.rgb},0.4)`,
                                        strokeStyle: `rgba(${b.rgb},0.4)`,
                                        lineWidth: 0,
                                        hidden: false,
                                        index: i + 1,
                                      } as LegendItem)),
                                    ],
                                  },
                                },
                                // 구간 배경 밴드 (y축 기준 가로 밴드)
                                annotation: { annotations: hhiBandAnnotations },
                                tooltip: {
                                  callbacks: {
                                    title: items => `${items[0]?.label ?? ''}년`,
                                    label: ctx => {
                                      // 필터된 배열(w2HhiFiltered) 기준으로 조회 — 토글/필터 모두 자동 반영
                                      const h = w2HhiFiltered[ctx.dataIndex]
                                      return h ? `HHI : ${Math.round(h.hhi).toLocaleString()}` : ''
                                    },
                                  },
                                },
                              },
                              scales: {
                                x: { grid: { display: false } },
                                y: {
                                  title: { display: true, text: 'HHI' },
                                  min: hhiAxis.min, max: hhiAxis.max,
                                  ticks: { stepSize: hhiAxis.step },
                                },
                              },
                            }}
                          />}
                          </div>
                        </div>
                        <div className="chart-split-line" />
                        <div className="chart-split-box">
                          <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', gap: 6, fontSize: 16, fontWeight: 600, marginBottom: 4 }}>
                            <span>{rankingToggle === '회사' ? '회사 경쟁 순위' : '브랜드 경쟁 순위'}</span>
                            <InfoTooltip text={tt.rank} />
                          </div>
                          <div style={{ height: 429 }}>
                          {isCauseLoading ? <ChartSkeleton legendItems={6} /> : <Bar
                            data={w2RankData}
                            options={{
                              ...commonOpts,
                              interaction: { mode: 'index', intersect: false },  // hover 시 막대 전체(연도) 강조
                              plugins: {
                                legend: {
                                  ...legendBottom,
                                  ...legendHoverPointer,
                                  // 데이터셋이 "순위 슬롯"이라 기본 범례/토글이 못 씀 → 브랜드 칩 직접 생성 + 브랜드 단위 토글
                                  labels: {
                                    ...legendBottom.labels,
                                    generateLabels: (): LegendItem[] => rankKeyArr.map((key, i) => {
                                      const color = rankColorMap.get(key) ?? '#999'
                                      return {
                                        text: rankLabelOf(key),
                                        fillStyle: color,
                                        strokeStyle: color,
                                        lineWidth: 0,
                                        hidden: hiddenRankKeys.has(key),
                                        index: i,
                                      } as LegendItem
                                    }),
                                  },
                                  onClick: (_e, legendItem) => {
                                    const key = rankKeyArr[legendItem.index ?? -1]
                                    if (!key) return
                                    setHiddenRankKeys(prev => {
                                      const next = new Set(prev)
                                      if (next.has(key)) next.delete(key)
                                      else next.add(key)
                                      return next
                                    })
                                  },
                                },
                                tooltip: {
                                  mode: 'index',
                                  intersect: false,
                                  filter: item => itemIn(rankYears[item.dataIndex], rankKeyArr[item.datasetIndex]) != null,
                                  // 툴팁 줄 순서 = 현재(최신) 기준 고정 = dataset(rankKeyArr) 순서. 기타는 rankKeyArr 끝이라 맨 아래.
                                  //   순위 번호(rd.rank)는 label에서 그 해 실제 순위로 표시 → 순서는 고정, 번호만 해당 연도 기준.
                                  itemSort: (a, b) => a.datasetIndex - b.datasetIndex,
                                  callbacks: {
                                    title: items => `${items[0]?.label ?? ''}년`,
                                    label: ctx => {
                                      const year = rankYears[ctx.dataIndex]
                                      const key = rankKeyArr[ctx.datasetIndex]
                                      if (!key) return ''
                                      const rd = rankYearly.find(y => y.year === year)
                                        ?.rankings?.find(r => (rankingToggle === '브랜드' ? r.brand : r.company) === key)
                                      if (!rd) return ''
                                      const ms = rd.ms_pct.toFixed(1)
                                      const val = rd.value != null ? fmtBaekman(rd.value) : '-'
                                      if (rd.is_others === true) {
                                        const oMs = rd.ms_pct.toFixed(1)
                                        const oVal = fmtBaekman(rd.value ?? 0)
                                        return `기타 (M/S : ${oMs}%, 매출 : ${oVal})`
                                      }
                                      return ((rd.value ?? 0) === 0 || rd.rank == null)
                                        ? `${key} (M/S : ${ms}%, 매출 : ${val})`
                                        : `${rd.rank}위 ${key} (M/S : ${ms}%, 매출 : ${val})`
                                    },
                                  },
                                },
                              },
                              scales: {
                                x: { stacked: true, grid: { display: false } },
                                y: {
                                  stacked: true,
                                  max: hiddenRankKeys.size === 0 ? 100 : undefined,
                                  // 부동소수점 오차 차단 — 정수면 그대로, 소수면 1자리
                                  ticks: { callback: (v: number | string) => {
                                    const n = Number(v)
                                    return `${Number.isInteger(n) ? n : n.toFixed(1)}%`
                                  } },
                                  title: { display: true, text: 'M/S (%)' },
                                },
                              },
                            }}
                          />}
                          </div>
                        </div>
                      </div>
                    </div>
                  </div>

                  {/* ===== 차트 3 + 4: Brand Trajectory Map + Growth Matrix ===== */}
                  <div className="chat-section-title-n">Growth &amp; M/S Matrix</div>
                  <div style={{ width: '100%', display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 24, marginBottom: 16 }}>
                    <div className="chart-widget">
                      <div className="chart-widget-header">
                        <div className="chart-widget-title-group">
                          <h2 className="chart-widget-title">Brand Trajectory Map</h2>
                          <InfoTooltip text={CHART_TOOLTIPS.brandTrajectory} />
                        </div>
                      </div>
                      <div className="chart-widget-body">
                        <div style={{ height: 429 }}>
                          {isCauseLoading ? <ChartSkeleton legendItems={4} /> : <Bubble
                            data={w3BubbleData}
                            options={{
                              ...commonOpts,
                              plugins: {
                                // 범례 클릭 toggle 비활성 (기획자 요청)
                                legend: { ...legendBottom, onClick: () => {} },
                                tooltip: {
                                  callbacks: {
                                    title: () => '',
                                    label: ctx => {
                                      const b = ctx.raw as { brand?: string; share_pct: number | null; ei: number | null; cagr_5y_pct: number | null; momentum_score: number | null } | undefined
                                      if (!b) return ''
                                      const fmt = (v: number | null | undefined, digits = 1) => v == null ? '-' : v.toFixed(digits)
                                      const mkt = kpi?.market_cagr_5y_pct ?? 0
                                      const cagrLine = b.cagr_5y_pct == null
                                        ? `CAGR (5Y) : - (시장 : ${mkt.toFixed(1)}%)`
                                        : `CAGR (5Y) : ${b.cagr_5y_pct.toFixed(1)}% (시장 : ${mkt.toFixed(1)}% / ${(b.cagr_5y_pct - mkt) >= 0 ? '+' : ''}${(b.cagr_5y_pct - mkt).toFixed(1)}%p)`
                                      return [
                                        b.brand ?? '',
                                        `M/S : ${fmt(b.share_pct)}%`,
                                        `EI : ${fmt(b.ei)}`,
                                        cagrLine,
                                        `Momentum Score : ${b.momentum_score ?? '-'}`,
                                      ]
                                    },
                                  },
                                },
                                annotation: {
                                  annotations: {
                                    msLine: {
                                      type: 'line' as const,
                                      xMin: eiMatrix?.ms_avg_pct ?? 0,
                                      xMax: eiMatrix?.ms_avg_pct ?? 0,
                                      borderColor: 'rgba(0,0,0,0.3)',
                                      borderDash: [5, 5],
                                    },
                                  },
                                },
                              },
                              scales: {
                                x: {
                                  title: { display: true, text: 'M/S (현재)' },
                                  ticks: { callback: (v: number | string) => {
                                    const n = Number(v)
                                    return `${Number.isInteger(n) ? n : n.toFixed(1)}%`
                                  } },
                                },
                                y: { title: { display: true, text: 'Evolution Index (5개년 CAGR)' } },
                              },
                            }}
                          />}
                        </div>
                      </div>
                    </div>

                    <div className="chart-widget">
                      <div className="chart-widget-header">
                        <div className="chart-widget-title-group">
                          <h2 className="chart-widget-title">Growth Contribution &amp; M/S Matrix</h2>
                          <InfoTooltip text={CHART_TOOLTIPS.growthMatrix} />
                        </div>
                      </div>
                      <div className="chart-widget-body">
                        <div style={{ height: 429 }}>
                          {isCauseLoading ? <ChartSkeleton legendItems={4} /> : <Bubble
                            data={w4BubbleData}
                            options={{
                              ...commonOpts,
                              plugins: {
                                // 범례 클릭 toggle 비활성 (기획자 요청)
                                legend: { ...legendBottom, onClick: () => {} },
                                tooltip: {
                                  callbacks: {
                                    title: () => '',
                                    label: ctx => {
                                      const raw = ctx.raw as { brand?: string; x: number; y: number; value_recent: number } | undefined
                                      if (!raw) return ''
                                      return [
                                        raw.brand ?? '',
                                        `- M/S : ${raw.x.toFixed(1)}%`,
                                        `- 성장 기여 : ${raw.y.toFixed(1)}%`,
                                        `- 매출 : ${fmtBaekman(raw.value_recent)}`,
                                      ]
                                    },
                                  },
                                },
                                annotation: {
                                  annotations: {
                                    msLine: {
                                      type: 'line' as const,
                                      xMin: gcMsMatrix?.ms_avg_pct ?? 0,
                                      xMax: gcMsMatrix?.ms_avg_pct ?? 0,
                                      borderColor: 'rgba(0,0,0,0.3)',
                                      borderDash: [5, 5],
                                    },
                                    zeroLine: {
                                      type: 'line' as const,
                                      yMin: 0,
                                      yMax: 0,
                                      borderColor: 'rgba(0,0,0,0.3)',
                                      borderDash: [5, 5],
                                    },
                                  },
                                },
                              },
                              scales: {
                                x: {
                                  title: { display: true, text: 'M/S (현재)' },
                                  ticks: { callback: (v: number | string) => {
                                    const n = Number(v)
                                    return `${Number.isInteger(n) ? n : n.toFixed(1)}%`
                                  } },
                                },
                                y: {
                                  title: { display: true, text: 'Growth Contribution (1년 전후)' },
                                  ticks: { callback: (v: number | string) => {
                                    const n = Number(v)
                                    return `${Number.isInteger(n) ? n : n.toFixed(1)}%`
                                  } },
                                },
                              },
                            }}
                          />}
                        </div>
                      </div>
                    </div>
                  </div>

                  {/* ===== 차트 5: 전체 시장 분석 레벨별 매출 추이 및 M/S (AnalysisLevelChart 재사용) ===== */}
                  <AnalysisLevelChart
                    title="전체 시장 분석 레벨별 매출 추이 및 M/S"
                    sectionTitle="분석 레벨별 시장 현황"
                    analysisLv={causeFor(w5Measure)?.analysis_levels}
                    measure={w5Measure}
                    onMeasureChange={setW5Measure}
                    isLoading={isMeasureLoading(w5Measure)}
                    productName={productName}
                    sourceToggle={sourceToggle}
                    unitMeasure={unitMeasure}
                    onUnitChange={setUnitMeasure}
                    unitOptions={UNIT_OPTIONS}
                    periodOptions={periodOptionsForChart}
                    initialPeriod={currentDefaultPeriod}
                    ttTrend={tt.levelSalesTrend}
                    ttMs={tt.levelSalesMs}
                  />

                  {/* ===== 신규 차트: 주요 고객 분석 레벨별 매출 추이 및 M/S (AnalysisLevelChart 재사용) ===== */}
                  <AnalysisLevelChart
                    title="주요 고객 분석 레벨별 매출 추이 및 M/S"
                    showSectionTitle={false}
                    analysisLv={causeFor(wMsMeasure)?.analysis_level_market_status}
                    measure={wMsMeasure}
                    onMeasureChange={setWMsMeasure}
                    isLoading={isMeasureLoading(wMsMeasure)}
                    productName={productName}
                    sourceToggle={sourceToggle}
                    unitMeasure={unitMeasure}
                    onUnitChange={setUnitMeasure}
                    unitOptions={UNIT_OPTIONS}
                    periodOptions={periodOptionsForChart}
                    initialPeriod={currentDefaultPeriod}
                    ttTrend={ttMs.levelSalesTrend}
                    ttMs={ttMs.levelSalesMs}
                  />                  

                  {/* ===== 차트 7: 주요 고객별 Top5 경쟁구도 ===== */}
                  {/* 외부 섹션 타이틀 삭제 (수정사항_20260526) */}
                  <div className="chat-section-title-n">브랜드 경쟁구도</div>
                  <div className="chart-widget chart-widget-full">
                    <div className="chart-widget-header">
                      <div className="chart-widget-title-group">
                        <h2 className="chart-widget-title">주요 고객별 Top5 브랜드 경쟁구도</h2>
                      </div>
                      <div className="chart-widget-controls">
                        {custTargets.length > 0 && (
                          <>
                            <div className="control-box control-box-select">
                              <ChartSelect
                                options={custTargets.map(t => ({ value: t, label: t }))}
                                value={custTargets[custTargetIdxInput] ?? '전체'}
                                onChange={v => {
                                  const idx = custTargets.indexOf(v)
                                  if (idx >= 0) setCustTargetIdxInput(idx)
                                }}
                              />
                            </div>
                            <div className="in-sepa-line" />
                          </>
                        )}
                        <div className="control-box control-box-select">
                          <ChartSelect
                            options={periodOptionsForChart}
                            value={w7PeriodInput}
                            onChange={v => onPeriodInputChange(v, setW7PeriodInput, setW7FromInput, setW7ToInput, w7DataFrom, w7DataTo)}
                          />
                        </div>
                        <div className="control-box control-box-date">
                          <DateRangePicker
                            from={w7FromInput} to={w7ToInput}
                            mode={w7PeriodInput as 'monthly' | 'quarterly' | 'yearly'}
                            minYM={w7DataFrom} maxYM={w7DataTo}
                            onFromChange={setW7FromInput} onToChange={setW7ToInput}
                          />
                        </div>
                        <button type="button" className="btn-date-search" onClick={applyW7}>조회</button>
                        <button type="button" className="btn-date-reset"
                          onClick={() => {
                            setW7PeriodInput(currentDefaultPeriod)
                            setW7FromInput(w7Range?.from ?? ''); setW7ToInput(w7Range?.to ?? '')
                            setW7Period(currentDefaultPeriod)
                            setW7From(w7Range?.from ?? ''); setW7To(w7Range?.to ?? '')
                            setCustTargetIdxInput(0); setCustTargetIdx(0)
                          }}
                        >새로고침</button>
                        <div className="in-sepa-line" />
                        <MeasureToggle measure={w7Measure} onChange={setW7Measure} />
                        <div className="in-sepa-line" />
                        <button type="button" className="btn-excel-down" onClick={exportChart7Excel} disabled={isCauseLoading}>엑셀다운로드</button>
                      </div>
                    </div>
                    <div className="chart-widget-body">
                      <div className="chart-layout-split">
                        <div className="chart-layout-split-main">
                          <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', gap: 6, fontSize: 16, fontWeight: 600, marginBottom: 8 }}>
                            <span>매출 추이</span>
                            <InfoTooltip text={tt.top5CustomerTrend} />
                          </div>
                          <div style={{ height: 429 }}>
                          {isMeasureLoading(w7Measure) ? <ChartSkeleton legendItems={6} /> : <Line
                            data={w5LineData}
                            options={{
                              ...commonOpts,
                              interaction: { mode: 'index', intersect: false },  // hover 시 해당 x의 모든 라인 점만 활성
                              plugins: {
                                legend: { ...legendBottom, ...legendHoverPointer },
                                tooltip: {
                                  mode: 'index',
                                  intersect: false,
                                  // 툴팁 줄 순서 = 현재(최신) 기준 고정 = dataset(custTrendBrands) 순서. 기타는 배열 끝이라 맨 아래.
                                  itemSort: (a, b) => a.datasetIndex - b.datasetIndex,
                                  callbacks: {
                                    title: items => fmtPeriodKor(items[0]?.label ?? ''),
                                    label: ctx => {
                                      // dataset이 custTrendBrands로 정렬돼 있으므로 같은 배열로 인덱싱해야 매칭
                                      const b = custTrendBrands[ctx.datasetIndex]
                                      if (!b) return ''
                                      // 기간 슬라이싱: ctx.dataIndex는 필터된 인덱스 → 원본 인덱스로 변환
                                      const origIdx = w7Idxs[ctx.dataIndex] ?? ctx.dataIndex
                                      const rawVal = b.value_series[origIdx] ?? 0
                                      const measureLabel = w7Measure === 'sales' ? '매출' : '처방량'
                                      const formattedVal = w7Measure === 'sales' ? fmtBaekman(rawVal) : Math.round(rawVal).toLocaleString()
                                      const totalAtT = (custView?.trend_brands ?? []).reduce((sum, br) => sum + (br.value_series[origIdx] ?? 0), 0)
                                      const ms = totalAtT > 0 ? (rawVal / totalAtT) * 100 : 0
                                      const msStr = `${ms.toFixed(1)}%`
                                      // 값이 0이면 순위 번호 없이 브랜드명만 (미출시/무매출)
                                      const periodRank = custTrendBrands.filter(x => x.brand !== '기타' && (x.value_series[origIdx] ?? 0) > rawVal).length + 1
                                      return b.brand === '기타'
                                        ? `${b.brand} (M/S : ${msStr}, ${measureLabel} : ${formattedVal})`
                                        : rawVal === 0
                                          ? `${b.brand} (회사 : ${b.company ?? '-'}, M/S : ${msStr}, ${measureLabel} : ${formattedVal})`
                                          : `${periodRank}위 ${b.brand} (회사 : ${b.company ?? '-'}, M/S : ${msStr}, ${measureLabel} : ${formattedVal})`
                                    },
                                  },
                                },
                              },
                              scales: {
                                x: { grid: { display: false } },
                                y: { title: { display: true, text: yTitleFor(w7Measure) }, ticks: { callback: v => Number(v).toLocaleString() } },
                              },
                            }}
                          />}
                          </div>
                        </div>
                        <div className="chart-layout-split-sub">
                          <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', gap: 6, fontSize: 16, fontWeight: 600, marginBottom: 8 }}>
                            <span>M/S (최근 시점{custPeriodsBase.length > 0 ? ` : ${fmtPeriodKor(custPeriodsBase[custPeriodsBase.length - 1]!)}` : ''} 기준)</span>
                            <InfoTooltip text={tt.top5CustomerMs} />
                          </div>
                          <div style={{ height: 429 }}>
                          {isMeasureLoading(w7Measure) ? <ChartSkeleton leftAxis={false} xAxis={false} legendItems={6} /> : <Doughnut
                            data={w5DoughnutData}
                            options={{
                              ...commonOpts,
                              plugins: {
                                // 범례 클릭 toggle 비활성 (기획자 요청)
                                legend: { ...legendBottom, labels: { ...legendBottom.labels, generateLabels: opaqueLabelsForArc }, onClick: () => {} },
                                tooltip: {
                                  callbacks: {
                                    title: () => '',
                                    label: ctx => {
                                      // 도넛은 custTrendBrands 기반 정렬 (라인과 동일) — 같은 인덱스로 brand/rank 조회
                                      const b = custTrendBrands[ctx.dataIndex]
                                      if (!b) return ''
                                      // '기타'는 extraOthersPct 합산값(데이터 자체)로 표시, 그 외엔 composition pct lookup
                                      const pct = (ctx.raw as number) ?? 0
                                      const prefix = b.rank == null ? b.brand : `${b.rank}위 ${b.brand}`
                                      return [prefix, `- M/S : ${pct.toFixed(1)}%`]
                                    },
                                  },
                                },
                              },
                            }}
                          />}
                          </div>
                        </div>
                      </div>
                    </div>
                  </div>

                  {/* ===== 차트 8: Level Top5 누적막대 + 가로막대 ===== */}
                  {/* 외부 섹션 타이틀 삭제 (수정사항_20260526) */}
                  <div className="chart-widget chart-widget-full">
                    <div className="chart-widget-header">
                      <div className="chart-widget-title-group">
                        <h2 className="chart-widget-title">분석 레벨별 Top5 브랜드 매출 추이 및 M/S</h2>
                      </div>
                      <div className="chart-widget-controls">
                        {lvTop5Levels.length > 0 && (
                          <div className="control-box control-box-select">
                            <ChartSelect
                              options={lvTop5Levels.map(l => ({ value: l.key, label: l.label }))}
                              value={lvTop5KeyInput}
                              onChange={setLvTop5KeyInput}
                            />
                          </div>
                        )}
                        {/* Sub 셀렉박스 — 응답의 all_options 그대로 노출, default_option이 기본값. 옵션 0개면 비활성화 */}
                        <div className="control-box control-box-select">
                          {lvSubOptionsInput.length > 0 ? (
                            <ChartSelect
                              options={lvSubOptionsInput.map(v => ({ value: v, label: v }))}
                              value={lvSubValueInput ?? lvSubOptionsInput[0]!}
                              onChange={setLvSubValueInput}
                            />
                          ) : (
                            <ChartSelect
                              options={[{
                                value: '__none',
                                label: isFilteredMemberScopeUnavailable ? '축별 분해 미제공' : '데이터 없음',
                              }]}
                              value="__none"
                              onChange={() => {}}
                            />
                          )}
                        </div>
                        <div className="in-sepa-line" />
                        <div className="control-box control-box-select">
                          <ChartSelect
                            options={periodOptionsForChart}
                            value={w8PeriodInput}
                            onChange={v => onPeriodInputChange(v, setW8PeriodInput, setW8FromInput, setW8ToInput, w8DataFrom, w8DataTo)}
                          />
                        </div>
                        <div className="control-box control-box-date">
                          <DateRangePicker
                            from={w8FromInput} to={w8ToInput}
                            mode={w8PeriodInput as 'monthly' | 'quarterly' | 'yearly'}
                            minYM={w8DataFrom} maxYM={w8DataTo}
                            onFromChange={setW8FromInput} onToChange={setW8ToInput}
                          />
                        </div>
                        <button type="button" className="btn-date-search" onClick={applyW8}>조회</button>
                        <button type="button" className="btn-date-reset"
                          onClick={() => {
                            setW8PeriodInput(currentDefaultPeriod)
                            setW8FromInput(w8Range?.from ?? ''); setW8ToInput(w8Range?.to ?? '')
                            setW8Period(currentDefaultPeriod)
                            setW8From(w8Range?.from ?? ''); setW8To(w8Range?.to ?? '')
                            setLvTop5KeyInput(''); setLvTop5Key('')
                            setLvSubValueInput(null); setLvSubValue(null)
                          }}
                        >새로고침</button>
                        <div className="in-sepa-line" />
                        {sourceToggle === 'IQVIA' && w8Measure === 'volume' && (
                          <div className="control-box control-box-select">
                            <ChartSelect options={UNIT_OPTIONS} value={unitMeasure} onChange={v => setUnitMeasure(v as 'unit' | 'dosage_unit' | 'counting_unit')} />
                          </div>
                        )}
                        <MeasureToggle measure={w8Measure} onChange={setW8Measure} />
                        <div className="in-sepa-line" />
                        <button type="button" className="btn-excel-down" onClick={exportChart8Excel} disabled={isCauseLoading}>엑셀다운로드</button>
                      </div>
                    </div>
                    <div className="chart-widget-body">
                      {!isMeasureLoading(w8Measure) && lvTop5EmptyMessage ? (
                        <div className="chart-empty-state" role="status">{lvTop5EmptyMessage}</div>
                      ) : (
                      <div className="chart-layout-split">
                        <div className="chart-layout-split-main">
                          <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', gap: 6, fontSize: 16, fontWeight: 600, marginBottom: 8 }}>
                            <span>매출 추이</span>
                            <InfoTooltip text={tt.levelTop5Trend} />
                          </div>
                          <div style={{ height: 429 }}>
                          {isMeasureLoading(w8Measure) ? <ChartSkeleton legendItems={6} /> : <Bar
                            data={w6StackedData}
                            options={{
                              ...commonOpts,
                              // hover 시 막대 전체(스택 전 세그먼트) opacity 1 (수정사항_20260526)
                              // — mode:'index'로 모든 dataset의 같은 column이 active → hoverBackgroundColor 적용
                              interaction: { mode: 'index', intersect: false },
                              plugins: {
                                legend: {
                                  ...legendBottom,
                                  ...legendHoverPointer,
                                  labels: {
                                    ...legendBottom.labels,
                                    generateLabels: (): LegendItem[] => lvBrands.map((b, i) => {
                                      const color = lvColorOf(b)
                                      return {
                                        text: b.brand,
                                        fillStyle: color,
                                        strokeStyle: color,
                                        lineWidth: 0,
                                        hidden: hiddenLvKeys.has(b.brand),
                                        index: i,
                                      } as LegendItem
                                    }),
                                  },
                                  onClick: (_e, legendItem) => {
                                    const b = lvBrands[legendItem.index ?? -1]
                                    if (!b) return
                                    setHiddenLvKeys(prev => {
                                      const next = new Set(prev)
                                      if (next.has(b.brand)) next.delete(b.brand)
                                      else next.add(b.brand)
                                      return next
                                    })
                                  },
                                },
                                tooltip: {
                                  mode: 'index',
                                  intersect: false,
                                  filter: item => lvSlotsByPeriod[item.dataIndex]?.[item.datasetIndex] != null,
                                  callbacks: {
                                    title: items => fmtPeriodKor(items[0]?.label ?? ''),
                                    label: ctx => {
                                      const b = lvSlotsByPeriod[ctx.dataIndex]?.[ctx.datasetIndex]
                                      if (!b) return ''
                                      const origIdx = ctx.dataIndex
                                      const ms = (b.ms_series_10pt[origIdx] ?? 0).toFixed(1)
                                      const rawVal = b.value_series_10pt[origIdx] ?? 0
                                      const measureLabel = w8Measure === 'sales' ? '매출' : '처방량'
                                      const val = w8Measure === 'sales' ? fmtBaekman(rawVal) : Math.round(rawVal).toLocaleString()
                                      const periodRank = lvFixedOrder.filter(x => !lvIsOthers(x) && (x.value_series_10pt[origIdx] ?? 0) > rawVal).length + 1
                                      const brandLabel = levelTop5BrandLabel(b)
                                      if (b.data_quality?.available === false) return brandLabel
                                      return lvIsOthers(b)
                                        ? `기타 (M/S : ${ms}%, ${measureLabel} : ${val})`
                                        : rawVal === 0
                                          ? `${brandLabel} (회사 : ${b.company ?? '-'}, M/S : ${ms}%, ${measureLabel} : ${val})`
                                          : `${periodRank}위 ${brandLabel} (회사 : ${b.company ?? '-'}, M/S : ${ms}%, ${measureLabel} : ${val})`
                                    },
                                  },
                                },
                              },
                              scales: {
                                x: { stacked: true, grid: { display: false } },
                                y: {
                                  stacked: true,
                                  title: { display: true, text: yTitleFor(w8Measure) },
                                  ticks: { callback: v => Number(v).toLocaleString() },
                                },
                              },
                            }}
                          />}
                          </div>
                        </div>
                        <div className="chart-layout-split-sub">
                          <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', gap: 6, fontSize: 16, fontWeight: 600, marginBottom: 8 }}>
                            <span>M/S (최근 시점{w8PeriodsBase.length > 0 ? ` : ${fmtPeriodKor(w8PeriodsBase[w8PeriodsBase.length - 1]!)}` : ''} 기준)</span>
                            <InfoTooltip text={tt.levelTop5Ms} />
                          </div>
                          <div style={{ height: 429 }}>
                          {isMeasureLoading(w8Measure) ? <ChartSkeleton legendItems={0} /> : <Bar
                            data={w6HBarData}
                            options={{
                              ...commonOpts,
                              indexAxis: 'y' as const,
                              plugins: {
                                legend: { display: false },
                                tooltip: {
                                  callbacks: {
                                    title: () => '',
                                    label: ctx => {
                                      const b = lvBrandsRev[ctx.dataIndex]
                                      if (!b) return ''
                                      const brandLabel = levelTop5BrandLabel(b)
                                      if (b.data_quality?.available === false) return [brandLabel]
                                      const prefix = lvIsOthers(b) || b.rank == null ? brandLabel : `${b.rank}위 ${brandLabel}`
                                      const ms = (b.ms_series_10pt[b.ms_series_10pt.length - 1] ?? 0).toFixed(1)
                                      const measureLabel = w8Measure === 'sales' ? '매출' : '처방량'
                                      const val = w8Measure === 'sales' ? fmtBaekman(b.value_recent_100m * 1e8) : Math.round(b.value_recent_100m * 1e8).toLocaleString()
                                      return [prefix, `- M/S : ${ms}%`, `- ${measureLabel} : ${val}`]
                                    },
                                  },
                                },
                              },
                              scales: {
                                x: { title: { display: true, text: 'M/S (%)' } },
                                y: { grid: { display: false } },
                              },
                            }}
                          />}
                          </div>
                        </div>
                      </div>
                      )}
                    </div>
                  </div>

                  {/* ===== 차트 6: 시장 매출변화 기여도 (플로팅 막대) ===== */}
                  <div className="chat-section-title-n">Market Contribution</div>
                  <div className="chart-widget chart-widget-full">
                    <div className="chart-widget-header">
                      <div className="chart-widget-title-group">
                        <h2 className="chart-widget-title">시장 매출변화 기여도</h2>
                      </div>
                      <div className="chart-widget-controls">
                        {/* 1년~5년 셀렉트: growth_contribution.windows[key] 데이터 driver + datepicker 표시 자동 갱신 */}
                        <div className="control-box control-box-select">
                          <ChartSelect
                            options={[
                              { value: '1y', label: '1년' },
                              { value: '2y', label: '2년' },
                              { value: '3y', label: '3년' },
                              { value: '4y', label: '4년' },
                              { value: '5y', label: '5년' },
                            ]}
                            value={w4Window}
                            onChange={v => setW4Window(v as '1y' | '2y' | '3y' | '4y' | '5y')}
                          />
                        </div>
                        {/* DateRangePicker 클릭 비활성 — 표시 전용. pointer-events:none으로 popup 차단 */}
                        <div className="control-box control-box-date" style={{ pointerEvents: 'none' }}>
                          <DateRangePicker
                            from={w4FromInput} to={w4ToInput}
                            mode={w4PeriodInput as 'monthly' | 'quarterly' | 'yearly'}
                            onFromChange={setW4FromInput} onToChange={setW4ToInput}
                          />
                        </div>
                        <button type="button" className="btn-date-reset"
                          onClick={() => {
                            // 차트 6 reset: windows 기본 5y + DateRangePicker는 새 windows의 period로 동기화
                            setW4Window('5y')
                            setW4PeriodInput('yearly')
                            setW4FromInput(w4DataFrom ?? ''); setW4ToInput(w4DataTo ?? '')
                          }}
                        >새로고침</button>
                        <div className="in-sepa-line" />
                        <button type="button" className="btn-excel-down" onClick={exportChart9Excel} disabled={isCauseLoading}>엑셀다운로드</button>
                      </div>
                    </div>
                    <div className="chart-widget-body">
                      <div className="chart-layout-half-split">
                        <div className="chart-split-box">
                          <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', gap: 6, fontSize: 16, fontWeight: 600, marginBottom: 4 }}>
                            <span>브랜드별 성장/하락 기여도</span>
                            <InfoTooltip text={CHART_TOOLTIPS.marketContributionBrand} />
                          </div>
                          <div style={{ height: 429 }}>
                            {isCauseLoading ? <ChartSkeleton legendItems={2} /> : <Bar
                              data={{
                                labels: gcLabels,
                                datasets: [{ label: '기여도', data: gcFloatData, backgroundColor: gcBgColors, hoverBackgroundColor: gcBgColors, borderWidth: 0 }],
                              }}
                              plugins={[contribLabelPlugin]}
                              options={{
                                ...commonOpts,
                                layout: { padding: { top: 22 } },
                                plugins: {
                                  legend: {
                                    ...legendBottom,
                                    labels: {
                                      ...legendBottom.labels,
                                      generateLabels: (): LegendItem[] => [
                                        { text: '성장 기여', fillStyle: CONTRIB_UP, strokeStyle: CONTRIB_UP, lineWidth: 0, hidden: false, datasetIndex: 0 },
                                        { text: '저하 기여', fillStyle: CONTRIB_DOWN, strokeStyle: CONTRIB_DOWN, lineWidth: 0, hidden: false, datasetIndex: 0 },
                                      ],
                                    },
                                    onClick: () => {},
                                  },
                                  tooltip: {
                                    callbacks: fmtContribTooltipFor(gcLabels, gcContribs, gcOthersPct),
                                  },
                                },
                                scales: {
                                  x: { grid: { display: false } },
                                  y: {
                                    // PDF v0.8: 매출만 (처방량 미적용). ticks만 /1e8 — 데이터는 raw 유지(툴팁이 ctx.raw 직접 사용)
                                    title: { display: true, text: '매출 (억원)' },
                                    ticks: { callback: v => Math.round(Number(v) / 1e8).toLocaleString() },
                                  },
                                },
                              }}
                            />}
                          </div>
                        </div>
                        <div className="chart-split-line" />
                        <div className="chart-split-box">
                          <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', gap: 6, fontSize: 16, fontWeight: 600, marginBottom: 4 }}>
                            <span>회사별 성장/하락 기여도</span>
                            <InfoTooltip text={CHART_TOOLTIPS.marketContributionCompany} />
                          </div>
                          <div style={{ height: 429 }}>
                            {isCauseLoading ? <ChartSkeleton legendItems={2} /> : <Bar
                              data={{
                                labels: ccLabels,
                                datasets: [{ label: '기여도', data: ccFloatData, backgroundColor: ccBgColors, hoverBackgroundColor: ccBgColors, borderWidth: 0 }],
                              }}
                              plugins={[contribLabelPlugin]}
                              options={{
                                ...commonOpts,
                                layout: { padding: { top: 22 } },
                                plugins: {
                                  legend: {
                                    ...legendBottom,
                                    labels: {
                                      ...legendBottom.labels,
                                      generateLabels: (): LegendItem[] => [
                                        { text: '성장 기여', fillStyle: CONTRIB_UP, strokeStyle: CONTRIB_UP, lineWidth: 0, hidden: false, datasetIndex: 0 },
                                        { text: '저하 기여', fillStyle: CONTRIB_DOWN, strokeStyle: CONTRIB_DOWN, lineWidth: 0, hidden: false, datasetIndex: 0 },
                                      ],
                                    },
                                    onClick: () => {},
                                  },
                                  tooltip: {
                                    callbacks: fmtContribTooltipFor(ccLabels, ccContribs, ccOthersPct),
                                  },
                                },
                                scales: {
                                  x: { grid: { display: false } },
                                  y: {
                                    // PDF v0.8: 매출만 — 데이터 raw 유지(ctx.raw 사용), ticks만 /1e8 환산
                                    title: { display: true, text: '매출 (억원)' },
                                    ticks: { callback: v => Math.round(Number(v) / 1e8).toLocaleString() },
                                  },
                                },
                              }}
                            />}
                          </div>
                        </div>
                      </div>
                    </div>
                  </div>

                </section>
                )}
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* Agent 챗봇 패널 — 우측 슬라이드 (정적 UI, 백엔드 미연결) */}
      <AgentChatPanel userName={user?.userName} />

      {showScrollTop && (
        <div
          className="scroll-botton-up-n"
          onClick={() => scrollRef.current?.scrollTo({ top: 0, behavior: 'smooth' })}
        />
      )}

      <Modals
        alertMessage={alertMessage}
        onCloseAlert={() => {setAlertMessage('')}}
      />
    </div>
    </AgentChatProvider>
  )
}
