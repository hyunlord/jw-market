import { useState, useEffect, useMemo, useRef } from 'react'
import { useLocation, useNavigate, Navigate } from 'react-router-dom'
import {
  Chart as ChartJS,
  CategoryScale, LinearScale, PointElement, LineElement,
  Title, Tooltip, Legend, Filler,
} from 'chart.js'
import { Line } from 'react-chartjs-2'
import annotationPlugin, { type AnnotationOptions } from 'chartjs-plugin-annotation'
import Sidebar from '../components/main/Sidebar'
import SelectBox from '../components/ui/SelectBox'
import MeasureToggle from '../components/main/MeasureToggle'
import SlideToggle from '../components/main/SlideToggle'
import DateRangePicker from '../components/main/DateRangePicker'
import * as deepAnalyzeExcel from '../utils/deepAnalyzeExcel'
import { useAuth } from '../context/AuthContext'
import { assayToDeepView, fetchDeepAnalysis, brandSourcesForAssay, refreshMarketBrands, isJwBrand } from '../utils/dynamicMarket'
import {
  deepAnalysisRequestKey,
  formatDeepAnalysisError,
  readDeepAnalysisCatalog,
  resolveDeepAnalysisMarketId,
  type DeepAnalysisRequestError,
  type DeepAnalysisViewKind,
} from '../utils/deepAnalysisRequest'
import type {
  AnalysisEvent, AnalysisResult, AiBullet,
} from '../types/market'
import { buildSourceNativeBrandProfile } from '../utils/brandProfileSource'
import { commonOpts, legendBottom, legendHoverPointer, fmtPeriodKor, fmtBaekman, TARGET_COLOR, COMPETITOR_PALETTE, UNIT_OPTIONS, solidTooltipLabelColor, legendTextAlignPlugin } from '../utils/chartHelpers'
import Modals from '../components/main/Modals'
import MarketTopNav from '../components/main/MarketTopNav'
import ChartSkeleton from '../components/main/ChartSkeleton'
import SkelBar from '../components/main/SkelBar'
import { AgentChatProvider, AgentChatPanel, AgentChatTrigger } from '../components/main/AgentChat'
import { TooltipBody } from '../utils/chartTooltips'
import { DEEP_ANALYSIS_TOOLTIPS } from '../utils/tooltipCopy'
import { buildForecastModelExplanation, hasReadableNewsBody } from '../utils/newsForecastDisplay'
import {
  brandObservedSourcePeriods,
  isSourceSelectable,
  resolveSourceAvailability,
  sourceAvailabilityTitle,
} from '../utils/sourceAvailability'

ChartJS.register(CategoryScale, LinearScale, PointElement, LineElement, Title, Tooltip, Legend, Filler, annotationPlugin, legendTextAlignPlugin)

// 툴팁 색상 표시를 네모 → 동그라미
ChartJS.defaults.plugins.tooltip.usePointStyle = true
ChartJS.defaults.plugins.tooltip.callbacks.labelColor = solidTooltipLabelColor
// 툴팁 항목 줄 간격 (따닥 붙지 않게)
ChartJS.defaults.plugins.tooltip.bodySpacing = 8
// 축 값 등 기본 폰트 13px (기획자 공통)
ChartJS.defaults.font.size = 13

// ============ 페이지 전용 상수 ============

type AiCardKey = 'phenomenon' | 'cause' | 'prediction' | 'recommendation'
// AI 인사이트 카드: 섹션 키 ↔ 배지(현황/원인/예측/대응) 매핑 (퍼블 status-badge 색상)
const AI_CARDS: { key: AiCardKey; badge: string; badgeClass: 'status' | 'cause' | 'forecast' | 'action' }[] = [
  { key: 'phenomenon', badge: '현황', badgeClass: 'status' },
  { key: 'cause', badge: '원인', badgeClass: 'cause' },
  { key: 'prediction', badge: '예측', badgeClass: 'forecast' },
  { key: 'recommendation', badge: '대응', badgeClass: 'action' },
]

// AI 인사이트 텍스트 — 보통 string이지만 백엔드가 일부 값을 {title, basis, stage} 객체로 섞어 보냄.
// bullets뿐 아니라 section.title/body에도 객체가 섞여 올 수 있어 렌더 전 모두 이 함수로 텍스트화한다.
// string이면 그대로, 객체면 title(+basis)을 한 줄로 합쳐 안전하게 텍스트화 (객체 렌더 시 React #31 방지).
function toText(v: unknown): string {
  if (typeof v === 'string') return v
  if (v && typeof v === 'object') {
    const o = v as Partial<AiBullet>
    return [o.title, o.basis].filter((x): x is string => typeof x === 'string' && x.trim() !== '').join(' · ')
  }
  return ''
}

const bulletToText = (b: string | AiBullet): string => toText(b)

interface ChatItem {
  uid: string
  title: string
  date: string
  pinned?: boolean
}

const MOCK_PINNED_LIST : ChatItem[] = [];
const MOCK_NORMAL_LIST : ChatItem[] = [];

const ASSAY_OPTIONS = [
  { value: 'jw', label: 'JW Strategic' },
  { value: 'market', label: 'Market Standard' },
]

const TURN_OPTIONS = [
  { value: 'latest', label: '최신순' },
  { value: 'importance', label: '중요도순' },
]

const DEEP_ANALYZE_TABS = ['매출 예측', '처방량 예측', 'Simulation', '브랜드 프로파일링'] as const
type DeepTab = typeof DEEP_ANALYZE_TABS[number]

function ProfileTableSkeleton({ cols = 7, rows = 6 }: { cols?: number; rows?: number }) {
  return (
    <div className="data-table-wrap">
      <table className="data-table">
        <thead>
          <tr>
            <th><SkelBar w={44} h={12} inline /></th>
            {Array.from({ length: cols }).map((_, i) => <th key={i}><SkelBar w={56} h={12} inline /></th>)}
          </tr>
        </thead>
        <tbody>
          {Array.from({ length: rows }).map((_, r) => (
            <tr key={r}>
              <th><SkelBar w={52} h={12} inline /></th>
              {Array.from({ length: cols }).map((_, c) => <td key={c}><SkelBar w="80%" h={12} inline /></td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function DeepAnalysisErrorState({ error }: { error: DeepAnalysisRequestError }) {
  return (
    <div className="deep-analysis-error" role="alert">
      <strong>분석 데이터를 표시할 수 없습니다.</strong>
      <p>{formatDeepAnalysisError(error)}</p>
    </div>
  )
}

// 처방량 combo: UBIST→UBIST.volume(Rx) / IQVIA→IQVIA.{unit|dosage_unit|counting_unit} (단위 셀렉트)
// 모든 combo는 단일 /analysis 응답의 by_combo에 포함 → 단위 전환은 재호출 없이 즉시
const volumeCombo = (source: 'UBIST' | 'IQVIA', unit: 'unit' | 'dosage_unit' | 'counting_unit'): string =>
  source === 'UBIST' ? 'UBIST.volume' : `IQVIA.${unit}`

const Q_TO_MM: Record<string, string> = { '1': '01', '2': '04', '3': '07', '4': '10' }
const labelToYM = (lbl: string): string => {
  const m = lbl.match(/^(\d{4})-Q([1-4])$/)
  return m ? `${m[1]}-${Q_TO_MM[m[2]]}` : lbl
}
const rangeFromLabels = (labels: string[]): { from: string; to: string; mode: 'monthly' | 'quarterly' } | null => {
  if (labels.length === 0) return null
  const isQuarterly = /^\d{4}-Q[1-4]$/.test(labels[0])
  return { from: labelToYM(labels[0]), to: labelToYM(labels[labels.length - 1]), mode: isQuarterly ? 'quarterly' : 'monthly' }
}

// ============ DeepAnalyzePage ============

// productName 변경 시 key로 통째 재마운트 (AnalyzePage와 동일 패턴)
export default function DeepAnalyzePage() {
  const location = useLocation()
  const navState = location.state as { productName?: string } | null
  const productName = navState?.productName ?? ''
  if (!productName) return <Navigate to="/market" replace />
  return <DeepAnalyzePageInner key={productName} />
}

function DeepAnalyzePageInner() {
  const [alertMessage, setAlertMessage] = useState('')
  // 상단 스크롤 버튼 — scrollTop > 300px일 때 표시
  const scrollRef = useRef<HTMLDivElement>(null)
  const [showScrollTop, setShowScrollTop] = useState(false)
  const { user } = useAuth()
  const location = useLocation()
  const navigate = useNavigate()

  const navState = location.state as {
    productName?: string; sources?: string[]
    generalSources?: string[]; strategicSources?: string[]; assay?: 'jw' | 'market'
  } | null
  const productName: string = navState?.productName ?? '리바로'
  const navInitialAssay: 'jw' | 'market' = (isJwBrand(productName) === false || navState?.assay === 'market') ? 'market' : 'jw'
  const assayLockedToMarket =
    isJwBrand(productName) === false
    && !(navState?.strategicSources ?? []).some(s => s === 'UBIST' || s === 'IQVIA')

  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [activeChatId, setActiveChatId] = useState<string | null>(null)
  const [assayValue, setAssayValue] = useState<'jw' | 'market'>(navInitialAssay)
  const [activeIssueId, setActiveIssueId] = useState<string | null>(null)
  const [deepTab, setDeepTab] = useState<DeepTab>('매출 예측')
  const [chartToggle, setChartToggle] = useState('매출')
  // IQVIA 처방량 단위 (Unit/Dosage Unit/Counting Unit) — 처방량 예측 탭에서 IQVIA일 때 노출
  const [unitMeasure, setUnitMeasure] = useState<'unit' | 'dosage_unit' | 'counting_unit'>('unit')
  // brand가 IQVIA만 지원하면 IQVIA로 시작 (UBIST 디폴트로 시작해서 빈 화면 방지)
  const [sourceToggle, setSourceToggle] = useState<'UBIST' | 'IQVIA'>(() => {
    const srcs = navState?.sources ?? []
    if (srcs.includes('UBIST')) return 'UBIST'
    if (srcs.includes('IQVIA')) return 'IQVIA'
    return 'UBIST'
  })
  const [issueCategory, setIssueCategory] = useState('all')
  const [turnValue, setTurnValue] = useState('latest')
  const [issueModalOpen, setIssueModalOpen] = useState(false)
  const [selectedIssue, setSelectedIssue] = useState<AnalysisEvent | null>(null)
  const [simBrand, setSimBrand] = useState<string>('')
  // 2차 작업 — 토글 UI 주석처리 상태. setter는 토글 복구 시 destructuring에 다시 추가
  const [simMeasure, setSimMeasure] = useState<'매출' | '처방량'>('매출')
  // 과거 5년 / 미래 5년 (UBIST=월×60 / IQVIA=분기×20 — perYear가 단위 분기 처리, test.md 5y 옵션)
  const [simPastYears] = useState(5)
  const [simFutureYears] = useState(5)

  const [analysisData, setAnalysisData] = useState<AnalysisResult | null>(null)
  const [analysisError, setAnalysisError] = useState<DeepAnalysisRequestError | null>(null)
  // AI 인사이트 status 카드 펼침/접힘 (기본 전체 펼침)
  const [openCards, setOpenCards] = useState<Record<AiCardKey, boolean>>({
    phenomenon: true, cause: true, prediction: true, recommendation: true,
  })
  const [insightPeriod, setInsightPeriod] = useState<'1년' | '5년'>('1년')
  // 🆕 PDF v0.8 Page 14 Description 1-3: 차트 하이라이트 — 이슈의 period_map[source] 위치에 vertical line
  const [highlightedEventId, setHighlightedEventId] = useState<string | null>(null)

  const [brandsRefreshed, setBrandsRefreshed] = useState(false)
  useEffect(() => { refreshMarketBrands(productName).then(() => setBrandsRefreshed(true)).catch(() => {}) }, [productName])

  const formalViewKind: DeepAnalysisViewKind = assayValue === 'market' ? 'general' : 'strategic_ml'
  const analysisMarketId = resolveDeepAnalysisMarketId(
    readDeepAnalysisCatalog(),
    productName,
    formalViewKind,
    sourceToggle,
  )

  // 정식 분석 컨텍스트가 바뀌면 이전 응답을 지우고 새 요청을 기다린다.
  const analysisFetchKey = deepAnalysisRequestKey(
    productName,
    formalViewKind,
    sourceToggle,
    analysisMarketId,
  )
  const [lastAnalysisFetchKey, setLastAnalysisFetchKey] = useState(analysisFetchKey)
  if (analysisFetchKey !== lastAnalysisFetchKey) {
    setLastAnalysisFetchKey(analysisFetchKey)
    setAnalysisData(null)
    setAnalysisError(null)
  }

  // 포탈 POST /api/v1/market/analysis — BFF가 정식 컨텍스트를 backend에 그대로 전달한다.
  useEffect(() => {
    if (!brandsRefreshed) return
    const activeSources = brandSourcesForAssay(productName, assayValue === 'jw' ? 'jw' : 'market')
    if (activeSources && !activeSources.has(sourceToggle)) return
    let cancelled = false
    fetchDeepAnalysis(productName, assayToDeepView(assayValue), {
      viewKind: formalViewKind,
      marketId: analysisMarketId,
      source: sourceToggle,
    })
      .then(result => {
        if (cancelled) return
        if (result.ok) {
          setAnalysisData(result.data)
          setAnalysisError(null)
        } else {
          setAnalysisData(null)
          setAnalysisError(result.error)
        }
      })
      .catch(() => {
        if (cancelled) return
        setAnalysisData(null)
        setAnalysisError({
          status: 0,
          code: 'network_error',
          message: '분석 데이터를 불러오지 못했습니다. 잠시 후 다시 시도해 주세요.',
          availableContexts: [],
        })
      })
    return () => { cancelled = true }
  }, [productName, assayValue, formalViewKind, analysisMarketId, sourceToggle, brandsRefreshed])

  const openIssueModal = (e: React.MouseEvent, issue: AnalysisEvent) => {
    e.preventDefault()
    if (!hasReadableNewsBody(issue.body_full)) return
    setSelectedIssue(issue)
    setIssueModalOpen(true)
  }

  // 이슈 카테고리 동적 생성
  const issueCategoryOptions = useMemo(() => {
    const cats = new Map((analysisData?.data.events ?? []).map(e => [e.category, e.category_label]))
    return [
      { value: 'all', label: '전체' },
      ...[...cats.entries()].map(([value, label]) => ({ value, label })),
    ]
  }, [analysisData])

  const navSourcesForAssay = (assay: string): Set<'UBIST' | 'IQVIA'> | null => {
    const raw = assay === 'jw' ? navState?.strategicSources : navState?.generalSources
    const eff = (raw ?? []).filter((s): s is 'UBIST' | 'IQVIA' => s === 'UBIST' || s === 'IQVIA')
    return eff.length ? new Set(eff) : null
  }
  const sourceSupport = useMemo(() => {
    const combos = analysisData?.available_combos ?? []
    // 응답의 과거 조합은 stale 구분에만 쓰고, 활성 여부는 브랜드 카탈로그가 결정한다.
    const catalogSources = brandSourcesForAssay(productName, assayValue === 'jw' ? 'jw' : 'market')
      ?? navSourcesForAssay(assayValue)
    return resolveSourceAvailability(catalogSources, combos)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [analysisData, productName, assayValue, brandsRefreshed])
  const observedSourcePeriods = brandObservedSourcePeriods(productName)

  // IQVIA 처방량 단위 셀렉트 — available_combos에 실제 존재하는 단위(IQVIA.unit/dosage_unit/counting_unit)만 노출
  const availableUnitOptions = useMemo(() => {
    const combos = analysisData?.available_combos ?? []
    return UNIT_OPTIONS.filter(o => combos.includes(`IQVIA.${o.value}`))
  }, [analysisData])
  if (availableUnitOptions.length > 0 && !availableUnitOptions.some(o => o.value === unitMeasure)) {
    setUnitMeasure(availableUnitOptions[0]!.value as 'unit' | 'dosage_unit' | 'counting_unit')
  }

  // 미지원 source 토글 시 자동으로 가용 source로 보정 (Adjust state during render 패턴)
  const [lastSourceForGuard, setLastSourceForGuard] = useState(sourceToggle)
  const sourceSelectable = {
    UBIST: isSourceSelectable(sourceSupport.UBIST),
    IQVIA: isSourceSelectable(sourceSupport.IQVIA),
  }
  if (sourceToggle !== lastSourceForGuard || (!sourceSelectable[sourceToggle] && (sourceSelectable.UBIST || sourceSelectable.IQVIA))) {
    setLastSourceForGuard(sourceToggle)
    if (!sourceSelectable[sourceToggle]) {
      if (sourceSelectable.UBIST) setSourceToggle('UBIST')
      else if (sourceSelectable.IQVIA) setSourceToggle('IQVIA')
    }
  }

  // 탭 변경 시 chartToggle 디폴트로 강제 초기화 (PDF 수정사항_20260602: "탭 이동 시 이전 탭 상태값 초기화")
  // 매출 예측 → '매출', 처방량 예측 → '처방량'. 이전 탭에서 'M/S' 선택 상태였어도 무조건 측정값 디폴트로
  const [lastDeepTab, setLastDeepTab] = useState(deepTab)
  if (deepTab !== lastDeepTab) {
    setLastDeepTab(deepTab)
    if (deepTab === '매출 예측' && chartToggle !== '매출') setChartToggle('매출')
    else if (deepTab === '처방량 예측' && chartToggle !== '처방량') setChartToggle('처방량')
  }

  // 이슈 필터링 + 정렬
  const displayedEvents = useMemo(() => {
    // on_list === true만 이슈 목록 표시 (백엔드 안내: 사실상 다 true지만 false 케이스 대비 방어적 체크)
    const events = (analysisData?.data.events ?? []).filter(e => e.on_list === true)
    const filtered = issueCategory === 'all' ? events : events.filter(e => e.category === issueCategory)
    return [...filtered].sort((a, b) =>
      turnValue === 'importance'
        ? b.impact_score - a.impact_score
        : b.date.localeCompare(a.date)
    )
  }, [analysisData, issueCategory, turnValue])

  // 매출/처방량 예측 차트 데이터 (deepTab + sourceToggle + chartToggle 기반)
  // 매출 예측: 원 단위 → 억원 환산(/1e8) / 처방량 예측: 응답 단위 그대로
  // M/S 토글: history_ms_pct(%) + forecast_ms_pct(%) — 고객사 명세: forecast_values와 동일 length
  // 색상 (사용자 지정): 자사 TARGET_COLOR(#00A9E5), 경쟁사 COMPETITOR_PALETTE 순서 (#A586DD/#2168B0/#7AC7A1/#FCDF74/#FB9352)
  const isMsToggle = chartToggle === 'M/S'
  const forecastComboKey = deepTab === '처방량 예측'
    ? volumeCombo(sourceToggle, unitMeasure)
    : `${sourceToggle}.sales`
  const activeForecastCombo = analysisData?.data.forecast.by_combo[forecastComboKey]
  const forecastModelExplanation = useMemo(
    () => buildForecastModelExplanation({
      source: sourceToggle,
      historyPeriodCount: activeForecastCombo?.history_periods.length ?? 0,
      brands: activeForecastCombo?.brands ?? [],
    }),
    [activeForecastCombo, sourceToggle],
  )
  const forecastChartData = useMemo(() => {
    const isRevenue = deepTab !== '처방량 예측'
    const comboKey = isRevenue ? `${sourceToggle}.sales` : volumeCombo(sourceToggle, unitMeasure)
    const fc = analysisData?.data.forecast.by_combo[comboKey]
    if (!fc) return { labels: [], datasets: [] }
    // 백엔드가 forecast_periods[0]를 history_periods 마지막과 동일 시점으로 줄 때가 있음 → 중복 라벨(예: 2026-04 두 번) 방지
    // Simulation과 동일 dedup. 모든 brand가 같은 fc.forecast_periods를 공유하므로 한 번만 계산해서 일괄 적용
    const histLastP = fc.history_periods[fc.history_periods.length - 1]
    const fcStart = fc.forecast_periods[0] === histLastP ? 1 : 0
    // 과거 5년 / 미래 5년 고정 (Simulation과 동일). UBIST(월)=×60 / IQVIA(분기)=×20. 데이터가 부족하면 가용분만.
    const perYear = fc.period_unit === '분기' ? 4 : 12
    const pastN = 5 * perYear
    const futureN = 5 * perYear
    const histStart = Math.max(0, fc.history_periods.length - pastN)
    const histPeriods = fc.history_periods.slice(histStart)
    const fcPeriods = fc.forecast_periods.slice(fcStart, fcStart + futureN)
    const allPeriods = [...histPeriods, ...fcPeriods]
    const histLen = histPeriods.length
    // 매출: 원 → 억원(/1e8). 처방량: raw → 만(/1e4, 기획자 요청). M/S: 그대로(%).
    const toUnit = (v: number | null) => {
      if (v == null) return null
      if (isMsToggle) return v
      return isRevenue ? v / 1e8 : v / 1e4
    }
    let compIdx = 0
    return {
      labels: allPeriods,
      datasets: fc.brands.map(b => {
        const color = b.is_target ? TARGET_COLOR : COMPETITOR_PALETTE[compIdx++ % COMPETITOR_PALETTE.length]!
        const data = isMsToggle
          ? [...(b.history_ms_pct ?? []).slice(histStart), ...((b.forecast_ms_pct ?? []).slice(fcStart, fcStart + futureN))]
          : [...b.history_values.slice(histStart).map(toUnit), ...b.forecast_values.slice(fcStart, fcStart + futureN).map(toUnit)]
        return {
          label: b.brand,
          data: data as (number | null)[],
          borderColor: color,
          backgroundColor: color,  // 범례/툴팁 동그라미 채움
          borderWidth: 1.5,  // 자사/경쟁 동일 두께 (기획자: 자사만 두껍게 강조 X)
          tension: 0.3,
          pointRadius: 0,
          pointHoverRadius: 4,  // PDF 수정사항_20260602: 차트 라인 hover 시 마커 표시
          segment: {
            borderDash: (ctx: { p0DataIndex: number }) =>
              ctx.p0DataIndex >= histLen - 1 ? [5, 5] : [],
          },
        }
      }),
    }
  }, [analysisData, sourceToggle, deepTab, isMsToggle, unitMeasure])

  // 매출/처방량 예측 기간 표시 박스 범위 — 차트 첫/마지막 라벨(과거~예측 끝). 클릭 비활성(표시 전용)
  const forecastRange = useMemo(() => rangeFromLabels(forecastChartData.labels as string[]), [forecastChartData])

  // 매출/처방량 예측 차트 vertical line annotation의 yMax — 좌표 명시로 대각선 fallback 차단
  const forecastYMax = useMemo(() => {
    const all = forecastChartData.datasets.flatMap(d => (d.data as (number | null)[]) ?? [])
    const nums = all.filter((v): v is number => typeof v === 'number' && Number.isFinite(v))
    if (nums.length === 0) return 1
    return Math.max(...nums) * 1.1
  }, [forecastChartData])

  const simComboKey = simMeasure === '처방량' ? volumeCombo(sourceToggle, unitMeasure) : `${sourceToggle}.sales`

  const simAvailableBrands = useMemo(() => {
    return analysisData?.data.simulation.by_combo[simComboKey]?.available_brands ?? []
  }, [analysisData, simComboKey])

  const effectiveSimBrand = simBrand || simAvailableBrands.find(b => b.is_target === true)?.brand || simAvailableBrands[0]?.brand || ''

  const simulationChartData = useMemo(() => {
    const simCombo = analysisData?.data.simulation.by_combo[simComboKey]
    if (!simCombo) return { labels: [], datasets: [] }
    const brandData = simCombo.by_brand[effectiveSimBrand]
    if (!brandData) return { labels: [], datasets: [] }
    // 측정값별 환산 — 매출 /1e8(억), 처방량 /1e4(만). 처방량 예측 탭과 동일 (이 값에 맞춰 툴팁·Y축도 분기)
    const scaleDiv = simMeasure === '처방량' ? 1e4 : 1e8
    // granularity로 연→포인트 환산 (분기=4, 그 외 월=12). API는 항상 전체(과거 60·미래 120)를 주므로 프론트 slice
    const perYear = simCombo.source_granularity === 'quarterly' ? 4 : 12
    const pastN = simPastYears * perYear
    const futureN = simFutureYears * perYear
    const histStart = Math.max(0, brandData.history_periods.length - pastN)
    const histPeriods = brandData.history_periods.slice(histStart)
    const histValues = brandData.history_values.slice(histStart)
    // 백엔드 forecast_periods[0]가 history_periods 마지막과 동일 시점인 경우가 있음 (예측 모델 시작점 = 과거 마지막).
    // 그대로 concat하면 같은 시점이 두 인덱스에 존재 → x축 라벨 중복 + (현재) 위치 어긋남. 중복이면 forecast 첫 점 skip.
    const histLastP = histPeriods[histPeriods.length - 1]
    const fcStart = brandData.forecast_periods[0] === histLastP ? 1 : 0
    const fcPeriods = brandData.forecast_periods.slice(fcStart, fcStart + futureN)
    const sliceFc = (vals: number[] | undefined): number[] => (vals ?? []).slice(fcStart, fcStart + futureN).map(v => v / scaleDiv)
    const histLen = histPeriods.length
    const allLabels = [...histPeriods, ...fcPeriods]
    const nullArr = (n: number): null[] => Array(n).fill(null)
    // 예측 dataset이 과거 마지막 점과 시각적으로 한 점에서 분기되도록 bridge 추가
    // (없으면 과거 끝 인덱스 histLen-1과 예측 시작 인덱스 histLen이 한 칸 떨어져서 끊겨 보임)
    const lastHistScaled = histValues.length > 0 ? histValues[histValues.length - 1] / scaleDiv : null
    const fcWithBridge = (vals: number[] | undefined): (number | null)[] =>
      histLen > 0
        ? [...nullArr(histLen - 1), lastHistScaled, ...sliceFc(vals)]
        : sliceFc(vals)
    return {
      labels: allLabels,
      // Simulation 색상 (사용자 지정): 과거 #D1D2D7 / 기본 #82828D / 최저 #E98773 / 최고 #658CED
      datasets: [
        {
          label: '과거',
          data: [...histValues.map(v => v / scaleDiv), ...nullArr(fcPeriods.length)] as (number | null)[],
          borderColor: '#D1D2D7',
          backgroundColor: '#D1D2D7',
          borderWidth: 1.5,
          pointRadius: 2,
          pointHoverRadius: 4,
          tension: 0.3,
        },
        {
          label: '기본 예측',
          data: fcWithBridge(brandData.scenarios.base?.values),
          borderColor: '#82828D',
          backgroundColor: '#82828D',
          borderWidth: 2,
          pointRadius: 0,
          pointHoverRadius: 4,
          tension: 0.3,
          segment: { borderDash: () => [5, 5] as number[] },
        },
        {
          label: '최고 예측',
          data: fcWithBridge(brandData.scenarios.upper?.values),
          borderColor: '#658CED',
          backgroundColor: '#658CED',
          borderWidth: 1.5,
          pointRadius: 0,
          pointHoverRadius: 4,
          tension: 0.3,
          segment: { borderDash: () => [5, 5] as number[] },
        },
        {
          label: '최저 예측',
          data: fcWithBridge(brandData.scenarios.lower?.values),
          borderColor: '#E98773',
          backgroundColor: '#E98773',
          borderWidth: 1.5,
          pointRadius: 0,
          pointHoverRadius: 4,
          tension: 0.3,
          segment: { borderDash: () => [5, 5] as number[] },
        },
      ],
    }
  }, [analysisData, simComboKey, effectiveSimBrand, simPastYears, simFutureYears, simMeasure])

  // Simulation 현재 시점 — simulationChartData에서 직접 추출 (data와 idx 동일 소스 → 절대 mismatch 없음)
  // 과거 dataset의 마지막 비-null 인덱스 = 현재 시점
  const simCurrentIdx = useMemo(() => {
    const histData = simulationChartData.datasets[0]?.data as (number | null)[] | undefined
    if (!Array.isArray(histData)) return -1
    for (let i = histData.length - 1; i >= 0; i--) {
      if (typeof histData[i] === 'number' && Number.isFinite(histData[i])) return i
    }
    return -1
  }, [simulationChartData])

  // annotation vertical line 좌표 강제용 yMax (좌표 미지정 시 일부 케이스에서 대각선 fallback 발생)
  const simYMax = useMemo(() => {
    const all = simulationChartData.datasets.flatMap(d => (d.data as (number | null)[]) ?? [])
    const nums = all.filter((v): v is number => typeof v === 'number' && Number.isFinite(v))
    if (nums.length === 0) return 1
    return Math.max(...nums) * 1.1
  }, [simulationChartData])

  // 단위별 1년 칸 수 — UBIST(월)=12 / IQVIA(분기)=4. x축 라벨 간격(연 1회) + 날짜 박스 모드 결정
  const simPerYear = analysisData?.data.simulation.by_combo[simComboKey]?.source_granularity === 'quarterly' ? 4 : 12

  // 날짜 표시 박스 범위 — 차트 첫/마지막 라벨(과거 5년 시작 ~ 미래 5년 끝). 클릭 비활성(표시 전용)
  const simRange = useMemo(() => rangeFromLabels(simulationChartData.labels as string[]), [simulationChartData])

  // commonOpts/legendBottom은 utils/chartHelpers로 이동
  // 1년/5년 토글 → 단기/장기 인사이트 선택. 해당 필드 없으면 기존 ai_analysis로 fallback
  const aiAnalysis = insightPeriod === '5년'
    ? (analysisData?.data.ai_analysis_long ?? analysisData?.data.ai_analysis)
    : (analysisData?.data.ai_analysis_short ?? analysisData?.data.ai_analysis)
  const generatedAt = analysisData?.generated_at?.slice(0, 10) ?? ''

  // 시장 기준일은 선택 브랜드가 아니라 소스 전체의 최신 시점이다.
  // UBIST → "YYYY년 M월" / IQVIA → "YYYY년 N분기" (fmtPeriodKor 자동 분기)
  const referenceLabel = useMemo(() => {
    if (!analysisData) return ''
    const last = analysisData.market_meta.source_latest_period
    return last ? fmtPeriodKor(last) : ''
  }, [analysisData])

  // 시장 정의 툴팁 — 아직 미사용 (2026-07-15 주석 처리). 재사용 시 아래 useMemo + 헤더 JSX 주석 해제
  // const marketDef = useMemo(() => {
  //   try {
  //     const raw = sessionStorage.getItem('marketStatusResult')
  //     if (!raw) return ''
  //     const parsed = JSON.parse(raw) as { brand_cards?: { brand: string; atc_codes?: string[] }[] }
  //     const card = parsed.brand_cards?.find(c => c.brand === productName)
  //     return (card?.atc_codes ?? []).join(', ')
  //   } catch { return '' }
  // }, [productName])

  // 엑셀 핸들러
  const exportForecastExcel = () => {
    const isRevenue = deepTab !== '처방량 예측'
    const comboKey = isRevenue ? `${sourceToggle}.sales` : volumeCombo(sourceToggle, unitMeasure)
    const fc = analysisData?.data.forecast.by_combo[comboKey]
    if (!fc) return
    deepAnalyzeExcel.exportForecast({ productName, sourceToggle, isRevenue, referenceLabel, unitMeasure, fc })
  }
  const exportSimulationExcel = () => {
    const brandData = analysisData?.data.simulation.by_combo[simComboKey]?.by_brand[effectiveSimBrand]
    if (!brandData) return
    deepAnalyzeExcel.exportSimulation({ productName, referenceLabel, simComboKey, brand: effectiveSimBrand, isVolume: simMeasure === '처방량', brandData })
  }

  // 브랜드 프로파일링은 선택한 원천의 factors.values만 표로 투영한다.
  const bf = analysisData?.data.brand_factors
  const profile = buildSourceNativeBrandProfile(sourceToggle, bf)

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
        {/* top-navigation — 원인분석·심층분석 공용 */}
        <MarketTopNav onAlertMessage={setAlertMessage} />

        {/* content */}
        <div
          className="content-wrap scroll-container analyze"
          ref={scrollRef}
          onScroll={e => setShowScrollTop((e.currentTarget as HTMLDivElement).scrollTop > 300)}
        >
          <div className="content">
            <div className="content-inner">
              <div className="dashboard-inner">

                {/* 상단 섹션 (sticky) */}
                <section className="status-section">
                  <div className="section-title">
                    <div className="left-wrap">
                      {productName}
                      <div className="inner-tab">
                        <ul>
                          <li>
                            <a href="#" onClick={e => { e.preventDefault(); navigate('/market/analyze', { state: { productName, sources: navState?.sources, generalSources: navState?.generalSources, strategicSources: navState?.strategicSources, assay: assayValue } }) }}>원인분석</a>
                          </li>
                          <li>
                            <a href="#" className="on" onClick={e => e.preventDefault()}>심층분석</a>
                          </li>
                        </ul>
                      </div>
                      <SelectBox
                        wrapperClassName="assay-select"
                        options={ASSAY_OPTIONS}
                        value={assayValue}
                        disabled={assayLockedToMarket}
                        onChange={v => setAssayValue(v === 'market' ? 'market' : 'jw')}
                      />
                    </div>
                    <AgentChatTrigger />
                  </div>
                </section>

                {/* 메인 컨텐츠 */}
                {/* profiling 클래스로 좌측 이슈 패널 접힘/우측 확장 전환을 CSS로 애니메이션 (탭 그룹 이동 시 자연스러운 전환) */}
                <section className={`app-content${deepTab === '브랜드 프로파일링' ? ' profiling' : ''}`}>
                  {/* 브랜드 프로파일링에선 언마운트 대신 접어서(grid column 0 + fade) 부드럽게 사라지게 함 */}
                  <aside
                    className="panel panel-left"
                    aria-hidden={deepTab === '브랜드 프로파일링'}
                  >
                    <div className="panel-box">
                      <div className="box-top">
                        <div className="panel-box-title deep-tooltip-title">
                          이슈 목록
                          <i className="deep-tooltip-icon" aria-label="이슈 목록 설명">ⓘ
                            <div className="chart-tooltip"><TooltipBody text={DEEP_ANALYSIS_TOOLTIPS.issues} /></div>
                          </i>
                        </div>
                        <div className="panel-box-select">
                          <SelectBox
                            wrapperClassName="issue-select"
                            size="sm"
                            weight={400}
                            options={issueCategoryOptions}
                            value={issueCategory}
                            onChange={setIssueCategory}
                          />
                          <SelectBox
                            wrapperClassName="turn-select"
                            size="sm"
                            weight={400}
                            options={TURN_OPTIONS}
                            value={turnValue}
                            onChange={setTurnValue}
                          />
                        </div>
                      </div>
                      <ul className="issue-list scroll-container">
                        {!analysisData && !analysisError && Array.from({ length: 5 }).map((_, i) => (
                          <li key={`issue-skel-${i}`}>
                            <div className="issue-item" style={{ pointerEvents: 'none' }}>
                              <div className="issue-item-header">
                                <SkelBar w={72} h={14} />
                              </div>
                              <div className="issue-item-meta" style={{ display: 'flex', gap: 8 }}>
                                <SkelBar w={44} h={12} inline />
                                <SkelBar w={56} h={12} inline />
                                <SkelBar w={60} h={12} inline />
                              </div>
                              <SkelBar w="92%" h={16} mb={8} />
                              <SkelBar w="100%" h={12} mb={5} />
                              <SkelBar w="66%" h={12} />
                              <div className="btm-btns" style={{ marginTop: 10 }}>
                                <SkelBar w={44} h={12} inline />
                                <span className="line" />
                                <SkelBar w={56} h={12} inline />
                              </div>
                            </div>
                            {i < 3 && <div className="item-list-line" />}
                          </li>
                        ))}
                        {analysisError && (
                          <li className="issue-list-error">
                            분석 결과를 불러오지 못했습니다.
                          </li>
                        )}
                        {analysisData && displayedEvents.length === 0 && (
                          <li style={{ padding: '40px 16px', textAlign: 'center', color: '#aaa', fontSize: 13 }}>
                            이슈가 없습니다.
                          </li>
                        )}
                        {displayedEvents.map((issue, idx) => (
                          <li key={issue.id}>
                            <div
                              className={`issue-item${activeIssueId === issue.id ? ' is-active' : ''}`}
                              onClick={() => setActiveIssueId(issue.id)}
                            >
                              <div className="issue-item-header">
                                <span className="issue-item-date">{issue.date}</span>
                                {/* 🆕 PDF v0.8 Description 1-3: 클릭 시 차트에 period_map[source] 위치 하이라이트 (같은 이슈 재클릭 시 해제) */}
                                {/* 활성(링크 클릭) 시에만 하늘색 — 카드 선택과는 별개 (카드 active CSS 규칙은 common.css에서 주석 처리됨) */}
                                {/* on_chart === true 이벤트만 차트 마커 가능 — 그 외엔 링크 자체 숨김 (40개 중 15개) */}
                                {issue.on_chart && (
                                  <a
                                    href="#"
                                    className={`issue-item-link${highlightedEventId === issue.id ? ' is-highlighted' : ''}`}
                                    onClick={e => {
                                      e.preventDefault()
                                      setHighlightedEventId(prev => prev === issue.id ? null : issue.id)
                                    }}
                                  >차트 하이라이트</a>
                                )}
                              </div>
                              <div className="issue-item-meta">
                                <span className="category">{issue.category_label}</span>
                                <span>중요도 {issue.impact_score}</span>
                                <span className="last-category-text">{issue.source}</span>
                              </div>
                              <h3 className="issue-item-title">{issue.title}</h3>
                              <p className="issue-item-desc">
                                {issue.summary?.length > 80 ? issue.summary.slice(0, 80) + '...' : issue.summary}
                              </p>
                              <div className="btm-btns">
                                {hasReadableNewsBody(issue.body_full) && (
                                  <a href="#" className="issue-item-more" onClick={e => openIssueModal(e, issue)}>더보기</a>
                                )}
                                {(issue.url || issue.source_url) && (
                                  <>
                                    {hasReadableNewsBody(issue.body_full) && <span className="line" />}
                                    <a
                                      href={issue.url || issue.source_url || '#'}
                                      target="_blank"
                                      rel="noopener noreferrer"
                                      className="issue-item-view"
                                      onClick={e => e.stopPropagation()}
                                    >원문보기</a>
                                  </>
                                )}
                              </div>
                            </div>
                            {idx < displayedEvents.length - 1 && <div className="item-list-line" />}
                          </li>
                        ))}
                      </ul>
                    </div>
                  </aside>

                  <div className="panel-group-right">
                    {/* 차트 패널 */}
                    <section className="panel panel-center">
                      <div className="analyze-top-wrap">
                        <div className="analyze-tab">
                          <ul>
                            {DEEP_ANALYZE_TABS.map(tab => (
                              <li key={tab}>
                                <a
                                  href="#"
                                  className={deepTab === tab ? 'on' : ''}
                                  onClick={e => { e.preventDefault(); setDeepTab(tab); scrollRef.current?.scrollTo({ top: 0 }) }}
                                >
                                  {tab}
                                </a>
                              </li>
                            ))}
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
                                disabled: !isSourceSelectable(sourceSupport[s]),
                                title: sourceAvailabilityTitle(
                                  s,
                                  sourceSupport[s],
                                  observedSourcePeriods[s.toLowerCase()],
                                ),
                              }))}
                              value={sourceToggle}
                              onChange={setSourceToggle}
                            />
                          </div>
                          <div className="bx-line" />
                          <div className="bx-info">
                            <span>기준</span>
                            {analysisData
                              ? (referenceLabel || '—')
                              : analysisError ? '—' : <SkelBar w={72} h={15} inline />}
                            <i className="icon-ex" style={{ position: 'relative', cursor: 'pointer' }}>
                              <div className="chart-tooltip"><TooltipBody text={DEEP_ANALYSIS_TOOLTIPS.reference} /></div>
                            </i>
                          </div>
                          {/* 시장 정의 (아직 미사용 — 2026-07-15 주석 처리, marketDef useMemo도 함께 주석)
                          <div className="bx-line" />
                          <div className="bx-info">
                            시장 정의
                            <i className="icon-ex" style={{ position: 'relative', cursor: 'pointer' }}>
                              {marketDef && <div className="chart-tooltip">{marketDef}</div>}
                            </i>
                          </div>
                          */}
                        </div>
                      </div>
                      <div className="panel-box chart-box">
                        <header className="chart-box-header">
                          <h2 className="chart-box-title">
                            {deepTab}
                            {(deepTab === '매출 예측' || deepTab === '처방량 예측' || deepTab === 'Simulation') && (
                              <i className="icon-ex deep-tooltip-icon">
                                <div className="chart-tooltip"><TooltipBody text={
                                  deepTab === 'Simulation'
                                    ? DEEP_ANALYSIS_TOOLTIPS.simulation
                                    : `${DEEP_ANALYSIS_TOOLTIPS.forecast}\n\n${forecastModelExplanation}`
                                } /></div>
                              </i>
                            )}
                          </h2>
                          {(deepTab === '매출 예측' || deepTab === '처방량 예측') && (
                            <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                              {deepTab === '처방량 예측' && sourceToggle === 'IQVIA' && availableUnitOptions.length > 0 && (
                                <div className="control-box control-box-select">
                                  <SelectBox
                                    size="sm"
                                    weight={400}
                                    options={availableUnitOptions}
                                    value={unitMeasure}
                                    onChange={v => setUnitMeasure(v as 'unit' | 'dosage_unit' | 'counting_unit')}
                                  />
                                </div>
                              )}
                              {/* 기간 표시 박스 — 클릭 비활성(pointerEvents:none), 차트 범위(과거~예측)만 표시 + 토글과의 사이 구분선 */}
                              {forecastRange && (
                                <>
                                  <div className="control-box control-box-date" style={{ pointerEvents: 'none' }}>
                                    <DateRangePicker
                                      from={forecastRange.from}
                                      to={forecastRange.to}
                                      mode={forecastRange.mode}
                                      onFromChange={() => {}}
                                      onToChange={() => {}}
                                    />
                                  </div>
                                  <div className="bx-line" />
                                </>
                              )}
                              {/* 매출/처방량 예측 모두 [측정값, M/S] 토글 */}
                              <SlideToggle
                                options={deepTab === '매출 예측'
                                  ? [{ value: '매출', label: '매출' }, { value: 'M/S', label: 'M/S' }]
                                  : [{ value: '처방량', label: '처방량' }, { value: 'M/S', label: 'M/S' }]}
                                value={chartToggle}
                                onChange={setChartToggle}
                              />
                              <div className="bx-line" />
                              <button type="button" className="btn-excel-down" onClick={exportForecastExcel} disabled={!analysisData}>엑셀다운로드</button>
                            </div>
                          )}
                          {deepTab === 'Simulation' && (
                            <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap' }}>
                              {simAvailableBrands.length > 0 && (
                                <div className="control-box control-box-select">
                                  <SelectBox
                                    size="sm"
                                    weight={400}
                                    options={simAvailableBrands.map(b => ({ value: b.brand, label: b.brand }))}
                                    value={effectiveSimBrand}
                                    onChange={setSimBrand}
                                  />
                                </div>
                              )}
                              {/* IQVIA + 처방량일 때 단위 선택 (처방량 예측 탭과 동일 패턴) */}
                              {simMeasure === '처방량' && sourceToggle === 'IQVIA' && availableUnitOptions.length > 0 && (
                                <div className="control-box control-box-select">
                                  <SelectBox
                                    size="sm"
                                    weight={400}
                                    options={availableUnitOptions}
                                    value={unitMeasure}
                                    onChange={v => setUnitMeasure(v as 'unit' | 'dosage_unit' | 'counting_unit')}
                                  />
                                </div>
                              )}
                              {/* 기간 표시 박스 — 클릭 비활성(pointerEvents:none), 차트 범위(과거 5년~미래 5년)만 표시. Market Contribution과 동일 패턴 */}
                              {/* 좌우 구분선: 브랜드↔기간 / 기간↔측정값 토글 사이 (bx-line) */}
                              {simRange && (
                                <>
                                  <div className="bx-line" />
                                  <div className="control-box control-box-date" style={{ pointerEvents: 'none' }}>
                                    <DateRangePicker
                                      from={simRange.from}
                                      to={simRange.to}
                                      mode={simRange.mode}
                                      onFromChange={() => {}}
                                      onToChange={() => {}}
                                    />
                                  </div>
                                  <div className="bx-line" />
                                </>
                              )}
                              {/* 매출/처방량 토글 — simMeasure('매출'|'처방량') ↔ MeasureToggle('sales'|'volume') 변환 */}
                              <MeasureToggle
                                measure={simMeasure === '처방량' ? 'volume' : 'sales'}
                                onChange={m => setSimMeasure(m === 'volume' ? '처방량' : '매출')}
                              />
                              <div className="bx-line" />
                              <button type="button" className="btn-excel-down" onClick={exportSimulationExcel} disabled={!analysisData}>엑셀다운로드</button>
                              {/* 2차 작업 — 과거/미래 기간 토글 (현재는 과거 5년 / 미래 10년 고정)
                              <div style={{ display: 'flex', gap: 4, alignItems: 'center' }}>
                                <span style={{ fontSize: 11, color: '#888' }}>과거</span>
                                <div className="toggle-group">
                                  {[1, 3, 5].map(y => (
                                    <button key={y} type="button"
                                      className={`toggle-btn${simPastYears === y ? ' is-active' : ''}`}
                                      onClick={() => setSimPastYears(y)}
                                    >{y}년</button>
                                  ))}
                                </div>
                                <span style={{ fontSize: 11, color: '#888' }}>미래</span>
                                <div className="toggle-group">
                                  {[1, 3, 5, 10].map(y => (
                                    <button key={y} type="button"
                                      className={`toggle-btn${simFutureYears === y ? ' is-active' : ''}`}
                                      onClick={() => setSimFutureYears(y)}
                                    >{y}년</button>
                                  ))}
                                </div>
                              </div>
                              */}
                            </div>
                          )}
                        </header>
                        {/* key={deepTab} — 탭 전환마다 remount되어 CSS 등장 애니메이션(deep-tab-content-in) 재생 */}
                        <div className="chart-box-body" key={deepTab}>
                          {analysisError
                            ? <DeepAnalysisErrorState error={analysisError} />
                            : !analysisData
                              ? (deepTab === '브랜드 프로파일링' ? <ProfileTableSkeleton /> : <ChartSkeleton legendItems={6} />)
                              : <>
                          {(deepTab === '매출 예측' || deepTab === '처방량 예측') && (
                            <>
                              <div style={{ height: 429 }}>
                                <Line
                                  // 탭 전환 시 차트 인스턴스 새로 만들어 범례 hidden 상태 강제 리셋
                                  // (PDF 수정사항_20260602: "탭 이동 시 이전 탭 상태값 초기화")
                                  key={`forecast_${deepTab}`}
                                  data={forecastChartData}
                                  options={{
                                    ...commonOpts,
                                    // PDF 수정사항_20260602: 차트 라인 hover 시 같은 x의 모든 dataset 점 표시
                                    // (pointHoverRadius만 줘서는 nearest+intersect=true 디폴트라 점 위 정확히 호버해야만 트리거됨)
                                    interaction: { mode: 'index', intersect: false },
                                    plugins: {
                                      // 범례 hover 시 cursor pointer (PDF 수정사항_20260602)
                                      legend: { ...legendBottom, ...legendHoverPointer },
                                      tooltip: {
                                        mode: 'index',
                                        intersect: false,
                                        // 값 큰 순서로 정렬 (기획자: 자사 강제 맨 위 X, 순위 기반)
                                        itemSort: (a, b) => (b.parsed.y ?? 0) - (a.parsed.y ?? 0),
                                        callbacks: {
                                          // 날짜 한글 변환 ("2026-05" → "2026년 5월", "2026-Q2" → "2026년 2분기")
                                          title: items => {
                                            const lbl = items[0]?.label
                                            return lbl ? fmtPeriodKor(lbl) : ''
                                          },
                                          label: ctx => {
                                            const v = ctx.parsed.y
                                            const name = ctx.dataset.label ?? ''
                                            if (v === null || !Number.isFinite(v)) return `${name}: 0`
                                            if (isMsToggle) return `${name}: ${v.toFixed(1)}%`
                                            if (deepTab === '처방량 예측') {
                                              return `${name}: ${Math.round(v * 1e4).toLocaleString()}`
                                            }
                                            return `${name}: ${fmtBaekman(v * 1e8)}`
                                          },
                                        },
                                      },
                                      annotation: (() => {
                                        const ev = analysisData?.data.events.find(e => e.id === highlightedEventId)
                                        const period = ev?.period_map?.[sourceToggle]
                                        if (!period) return {}
                                        const labels = forecastChartData.labels as string[]
                                        const idx = labels.indexOf(period)
                                        if (idx < 0) return {}
                                        return {
                                          annotations: {
                                            eventHighlight: {
                                              type: 'line' as const,
                                              xMin: idx,
                                              xMax: idx,
                                              yMin: 0,
                                              yMax: forecastYMax,
                                              // annotation이 Y축 자동 max를 늘려 데이터 범위가 300→350으로 확장되는 부작용 차단
                                              adjustScaleRange: false,
                                              borderColor: '#000000',
                                              borderWidth: 2,
                                              borderDash: [5, 5],
                                            },
                                          },
                                        }
                                      })(),
                                    },
                                    scales: {
                                      x: { grid: { display: false }, ticks: { maxTicksLimit: 10 } },
                                      y: {
                                        title: {
                                          display: true,
                                          // M/S 토글이면 'M/S(%)' (PDF 수정사항_20260602), 그 외엔 매출(억원)/처방량(만) 분기
                                          text: isMsToggle ? 'M/S(%)' : (deepTab === '처방량 예측' ? '처방량 (만)' : '매출 (억원)'),
                                        },
                                        ticks: {
                                          // chart.js 자동 ticks의 부동소수점 누적 오차 차단 + 큰 수 가독성 콤마
                                          // M/S: 정수면 그대로 / 소수면 1자리 + "%". 매출·처방량: toLocaleString
                                          callback: (v: number | string) => {
                                            const n = Number(v)
                                            if (isMsToggle) return `${Number.isInteger(n) ? n : n.toFixed(1)}%`
                                            return n.toLocaleString()
                                          },
                                        },
                                      },
                                    },
                                  }}
                                />
                              </div>
                              {/* disclaimer는 백엔드가 내부 개발 정보(Phase N, 모델명 등)를 보내서 end user 노출 부적절 → 미표시 */}
                            </>
                          )}
                          {deepTab === 'Simulation' && (
                            <div style={{ height: 429 }}>
                              <Line
                                data={simulationChartData}
                                options={{
                                  ...commonOpts,
                                  // 같은 x의 모든 dataset 점이 동시 활성화 (hover 시 pointHoverRadius 표시)
                                  interaction: { mode: 'index', intersect: false },
                                  plugins: {
                                    // 범례 hover 시 cursor pointer (PDF 수정사항_20260602)
                                    legend: { ...legendBottom, ...legendHoverPointer },
                                    tooltip: {
                                      mode: 'index',
                                      intersect: false,
                                      callbacks: {
                                        // 날짜 한글 변환 (예: "2026-04" → "2026년 4월")
                                        title: items => {
                                          const lbl = items[0]?.label
                                          return lbl ? fmtPeriodKor(lbl) : ''
                                        },
                                        // 매출: 억환산값(/1e8) → ×1e8 원 복원 후 백만원 환산(fmtBaekman, 예: 2,567백만원)
                                        // 처방량: 만환산값(/1e4) → ×1e4 처방량 풀 표시, 단위 없음(숫자만). 처방량 예측 탭과 동일
                                        label: ctx => {
                                          const v = ctx.parsed.y
                                          const name = ctx.dataset.label ?? ''
                                          if (v === null || !Number.isFinite(v)) return `${name}: 0`
                                          if (simMeasure === '처방량') return `${name}: ${Math.round(v * 1e4).toLocaleString()}`
                                          return `${name}: ${fmtBaekman(v * 1e8)}`
                                        },
                                      },
                                    },
                                    // currentLine(현재 시점) + eventHighlight(차트 하이라이트) 동시 표시
                                    annotation: (() => {
                                      // chart.js annotationPlugin이 기대하는 정확한 타입 — Record<string, unknown>은 호환 안 됨
                                      const annotations: Record<string, AnnotationOptions> = {}
                                      if (simCurrentIdx >= 0) {
                                        annotations.currentLine = {
                                          // xMin/xMax 동일(카테고리 idx) + yMin/yMax 명시 →
                                          // chart.js v4 + annotationPlugin v3 일부 데이터 형태에서 좌표 미지정 시 대각선으로 그려지는 fallback 완전 차단
                                          type: 'line' as const,
                                          xMin: simCurrentIdx,
                                          xMax: simCurrentIdx,
                                          yMin: 0,
                                          yMax: simYMax,
                                          // annotation의 yMax(데이터*1.1)가 Y축 자동 max를 늘리는 부작용 차단
                                          adjustScaleRange: false,
                                          borderColor: '#D1D2D7',
                                          borderWidth: 1.5,
                                          // (현재) 텍스트는 x축 라벨로 통합 → annotation label 제거
                                        }
                                      }
                                      // 🆕 차트 하이라이트 — 매출/처방량 예측 차트와 동일한 vertical line
                                      const ev = analysisData?.data.events.find(e => e.id === highlightedEventId)
                                      const period = ev?.period_map?.[sourceToggle]
                                      if (period) {
                                        const labels = simulationChartData.labels as string[]
                                        const idx = labels.indexOf(period)
                                        if (idx >= 0) {
                                          annotations.eventHighlight = {
                                            type: 'line' as const,
                                            xMin: idx,
                                            xMax: idx,
                                            yMin: 0,
                                            yMax: simYMax,
                                            // annotation이 Y축 자동 max를 늘리는 부작용 차단
                                            adjustScaleRange: false,
                                            borderColor: '#000000',
                                            borderWidth: 2,
                                            borderDash: [5, 5],
                                          }
                                        }
                                      }
                                      return Object.keys(annotations).length > 0 ? { annotations } : {}
                                    })(),
                                  },
                                  scales: {
                                    x: {
                                      grid: { display: false },
                                      ticks: {
                                        // simCurrentIdx 라벨 강제 표시 + "(현재)" 부착 + 좌우 연 1회 간격 표시 (겹침 방지)
                                        // 과거 5년 + 미래 5년 = 최대 120칸(월)이라 simCurrentIdx 기준 |diff| % simPerYear === 0 위치만 표시
                                        // (UBIST 월=12개월마다 / IQVIA 분기=4분기마다 → 연 1회 라벨)
                                        autoSkip: false,
                                        maxRotation: 0,
                                        callback: function(value, index) {
                                          const lbl = this.getLabelForValue(value as number)
                                          if (index === simCurrentIdx) return `${lbl}(현재)`
                                          if (simCurrentIdx < 0) return index % simPerYear === 0 ? lbl : ''
                                          return Math.abs(index - simCurrentIdx) % simPerYear === 0 ? lbl : ''
                                        },
                                      },
                                    },
                                    y: {
                                      title: {
                                        display: true,
                                        text: simMeasure === '처방량' ? '처방량 (만)' : '매출 (억원)',
                                      },
                                      // Y축 누적 부동소수점 오차 차단 + 큰 수 가독성 콤마
                                      ticks: { callback: (v: number | string) => Number(v).toLocaleString() },
                                    },
                                  },
                                }}
                              />
                            </div>
                          )}
                          {deepTab === '브랜드 프로파일링' && (
                            profile.brands.length === 0 ? (
                              <div style={{ padding: '60px 0', textAlign: 'center', color: '#aaa' }}>브랜드 프로파일링 데이터가 없습니다.</div>
                            ) : (
                              <div className="data-table-wrap">
                                <table className="data-table" data-contract="brand-profile-source-native">
                                  <thead>
                                    <tr>
                                      <th>브랜드</th>
                                      {profile.brands.map((brand, index) => <th key={`${brand.brand}-${index}`}>{brand.brand}</th>)}
                                    </tr>
                                  </thead>
                                  <tbody>
                                    {profile.rows.map(row => (
                                      <tr key={row.key} className={row.highlight ? 'highlight-row' : undefined}>
                                        <th>{row.label}</th>
                                        {row.values.map((value, index) => (
                                          <td key={`${row.key}-${index}`} style={{ whiteSpace: 'pre-line' }}>{value}</td>
                                        ))}
                                      </tr>
                                    ))}
                                  </tbody>
                                </table>
                              </div>
                            )
                          )}
                          </>}
                        </div>
                        {/* 예측 신뢰도 — 백엔드 데이터 미정(언제 줄지 미확정)이라 주석 처리
                        {deepTab === 'Simulation' && (
                          <div className="bx-trust-wrap">
                            <div className="tx-l">예측 신뢰도</div>
                          </div>
                        )} */}
                      </div>
                    </section>

                    {/* AI 인사이트 패널 (퍼블 status 카드) */}
                    <section className="panel-box ai-analysis">
                      <div className="ai-analysis-header">
                        <div className="tx-l">
                          <div className="ai-analysis-title">AI 인사이트</div>
                          <span className="ai-analysis-date">
                            {analysisData
                              ? (generatedAt ? generatedAt.replace(/-/g, '.') + ' 업데이트' : '')
                              : analysisError ? '—' : <SkelBar w={120} h={12} inline />}
                          </span>
                        </div>
                        <SlideToggle<'1년' | '5년'>
                          options={[{ value: '1년', label: '1년' }, { value: '5년', label: '5년' }]}
                          value={insightPeriod}
                          onChange={setInsightPeriod}
                        />
                      </div>
                      {analysisError ? (
                        <DeepAnalysisErrorState error={analysisError} />
                      ) : aiAnalysis ? (
                        <div>
                          {AI_CARDS.map(card => {
                            const section = aiAnalysis[card.key]
                            const isOpen = openCards[card.key]
                            const oddTail = section.bullets.length % 2 === 1
                            return (
                              <div className="status-card-wrap" key={card.key}>
                                <div className="status-card-inner">
                                  <span className={`status-badge ${card.badgeClass}`}>{card.badge}</span>
                                  <div className="status-card-text-row">
                                    <div className="status-card-title-wrap">
                                      <div className="status-card-title">{toText(section.title)}</div>
                                      <button
                                        type="button"
                                        className={`status-card-toggle-btn ${isOpen ? 'open' : 'close'}`}
                                        aria-label="상세 내용 토글"
                                        onClick={() => setOpenCards(p => ({ ...p, [card.key]: !p[card.key] }))}
                                      />
                                    </div>
                                    <div className={`status-card-body ${isOpen ? 'open' : 'close'}`}>
                                      <div className="status-card-body-inner">
                                        {toText(section.body) && <p className="status-card-description">{toText(section.body)}</p>}
                                        {section.evidence && section.evidence.length > 0 && (
                                          <div className="status-card-evidence">
                                            {section.evidence.map((item, i) => (
                                              <p className="status-card-evidence-item" key={`${card.key}-evidence-${i}`}>
                                                {toText(item)}
                                              </p>
                                            ))}
                                          </div>
                                        )}
                                        {section.bullets.length > 0 && (
                                          <div className="grid-container">
                                            {section.bullets.map((b, i) => (
                                              <div
                                                key={i}
                                                className={`metric-box${oddTail && i === section.bullets.length - 1 ? ' metric-box--full' : ''}`}
                                              >
                                                {bulletToText(b)}
                                              </div>
                                            ))}
                                          </div>
                                        )}
                                      </div>
                                    </div>
                                  </div>
                                </div>
                              </div>
                            )
                          })}
                        </div>
                      ) : (
                        <div>
                          {AI_CARDS.map((card, ci) => (
                            <div className="status-card-wrap" key={`ai-skel-${card.key}`}>
                              <div className="status-card-inner">
                                <SkelBar w={48} h={26} r={13} />
                                <div className="status-card-text-row" style={{ flex: 1 }}>
                                  <SkelBar w={ci % 2 === 0 ? 260 : 200} h={18} mb={14} />
                                  <SkelBar w="100%" h={13} mb={6} />
                                  <SkelBar w="100%" h={13} mb={6} />
                                  <SkelBar w="82%" h={13} mb={14} />
                                  <div className="grid-container">
                                    {Array.from({ length: 4 }).map((_, j) => (
                                      <div className="metric-box" key={j} style={{ background: 'transparent', padding: 0 }}>
                                        <SkelBar w="100%" h={48} r={12} />
                                      </div>
                                    ))}
                                  </div>
                                </div>
                              </div>
                            </div>
                          ))}
                        </div>
                      )}
                    </section>
                  </div>
                </section>

              </div>
            </div>
          </div>
        </div>
      </div>

      {/* 이슈 상세 모달 */}
      {hasReadableNewsBody(selectedIssue?.body_full) && <div
        id="modal-issue-detail"
        className={`modal-overlay${issueModalOpen ? ' open' : ''}`}
        onClick={() => setIssueModalOpen(false)}
      >
        <div className="modal-window" onClick={e => e.stopPropagation()}>
          <div className="modal-header">
            <div>{selectedIssue?.title ?? ''}</div>
            <button type="button" className="btn-modal-close" onClick={() => setIssueModalOpen(false)}>닫기</button>
          </div>
          <div className="modal-content">
            <div>
              <div className="bx-date">
                <span className="tx-date">{selectedIssue?.date ?? ''}</span>
                <span className="sepa-line" />
                <span className="tx-date">{selectedIssue?.source ?? ''}</span>
              </div>
              <div className="bx-title">{selectedIssue?.summary ?? ''}</div>
              <div className="bx-content">{selectedIssue?.body_full ?? ''}</div>
            </div>
          </div>
        </div>
      </div>}

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
