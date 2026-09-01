// ============ 차트 공용 유틸 ============
// AnalyzePage / DeepAnalyzePage 차트 코드에서 공유하는 상수 + 헬퍼 + 플러그인
// (색상/카테고리는 차트 코드와 강결합이라 같은 파일에 둠)

import type { Chart as ChartJS, Plugin, LegendItem } from 'chart.js'
import type { EiMsItem, GcMsItem, RankItem, SelectOption } from '../types/market'

// ============ 색상 팔레트 ============

export const CHART_COLORS = [
  '#4472C4', '#ED7D31', '#A9D18E', '#E15759', '#FFC000',
  '#70AD47', '#5B9BD5', '#C00000', '#4BACC6', '#7030A0',
]

// 브랜드 색상 (PDF v0.75): 자사 / 경쟁1~5 / 기타 — 차트 2B·7·8 공용
export const TARGET_COLOR = '#00A9E5'
export const COMPETITOR_PALETTE = ['#A586DD', '#2168B0', '#7AC7A1', '#FCDF74', '#FB9352']
export const OTHERS_COLOR = '#D1D2D7'

// 시장 매출변화 기여도 색상 (PDF v0.75)
export const CONTRIB_UP = '#658CED'    // 성장 기여
export const CONTRIB_DOWN = '#E98773'  // 저하 기여
export const CONTRIB_BASE = '#D1D2D7'  // 기준(시작/현재)

// ============ 카테고리 정의 ============

// 차트3 E/I Matrix 버블 색상 분류 (PDF v0.75): 가속/감속(CAGR vs 시장) × 강한/완만(momentum 부호)
export const EI_CATEGORIES = [
  { key: 'strongAccel', label: '강한 가속', color: '#1CBC84' },
  { key: 'mildAccel', label: '완만한 가속', color: '#A0E6CD' },
  { key: 'mildDecel', label: '완만한 감속', color: '#FBB6A8' },
  { key: 'strongDecel', label: '강한 감속', color: '#E16349' },
] as const

// Growth Contribution & M/S 4분면 카테고리 (수정사항_20260526)
// X=M/S(share_pct) vs ms_avg_pct / Y=contribution_pct vs 0
export const GC_MS_CATEGORIES = [
  { key: 'dominant',  label: '시장 지배자', color: '#2168B0' }, // 파랑: M/S 평균 이상 + 성장 기여 +
  { key: 'emerging',  label: '신생 성장주', color: '#1CBC84' }, // 초록: M/S 평균 미만 + 성장 기여 +
  { key: 'mature',    label: '성숙·위협',   color: '#FB9352' }, // 주황: M/S 평균 이상 + 성장 기여 -
  { key: 'irrelevant',label: '시장무관',   color: '#E16349' }, // 빨강: M/S 평균 미만 + 성장 기여 -
] as const

// ============ 기간/단위 셀렉트 옵션 ============

export const PERIOD_MONTHLY: SelectOption[] = [
  { value: 'monthly', label: '월별' },
  { value: 'quarterly', label: '분기별' },
  { value: 'yearly', label: '년별' },
]

export const PERIOD_YEARLY: SelectOption[] = [
  { value: 'yearly', label: '년별' },
  { value: 'monthly', label: '월별' },
]

// IQVIA + 처방량 조건일 때 노출되는 단위 선택 (v0.8) — cause measure로 전달
export const UNIT_OPTIONS: SelectOption[] = [
  { value: 'unit', label: 'Unit' },
  { value: 'dosage_unit', label: 'Dosage Unit' },
  { value: 'counting_unit', label: 'Counting Unit' },
]

// ============ 포맷 헬퍼 ============

// IQVIA quarterly raw ("YYYY-Qn") → "YYYY-MM" 정규화 (DateRangePicker/filter 통합 비교용)
// 시작 월: Q1→01, Q2→04, Q3→07, Q4→10 / 끝 월: Q1→03, Q2→06, Q3→09, Q4→12
// 이미 "YYYY-MM" 형식이면 그대로 통과.
const Q_TO_START: Record<string, string> = { Q1: '01', Q2: '04', Q3: '07', Q4: '10' }
const Q_TO_END:   Record<string, string> = { Q1: '03', Q2: '06', Q3: '09', Q4: '12' }
export const ymStartOf = (p: string): string => {
  if (p.length >= 7 && p.charAt(5) === 'Q') {
    return `${p.slice(0, 4)}-${Q_TO_START[p.slice(5, 7)] ?? '01'}`
  }
  return p
}
export const ymEndOf = (p: string): string => {
  if (p.length >= 7 && p.charAt(5) === 'Q') {
    return `${p.slice(0, 4)}-${Q_TO_END[p.slice(5, 7)] ?? '12'}`
  }
  return p
}

// "2025-04" → "2025년 4월", "2025Q1" / "2025-Q1" → "2025년 1분기", "2025" → "2025년"
export const fmtPeriodKor = (p: string): string => {
  const m = /^(\d{4})-(\d{2})$/.exec(p)
  if (m) return `${m[1]}년 ${parseInt(m[2], 10)}월`
  const q = /^(\d{4})-?Q(\d)$/.exec(p)
  if (q) return `${q[1]}년 ${q[2]}분기`
  const y = /^(\d{4})$/.exec(p)
  if (y) return `${y[1]}년`
  return p
}

export function selectRankTrackerKeys(
  competitorKeys: string[],
  targetKey: string | undefined,
  rankOfKey: (key: string) => number,
): string[] {
  if (!targetKey) return [...competitorKeys]
  return [...competitorKeys, targetKey].sort((a, b) => rankOfKey(a) - rankOfKey(b))
}

// ============ monthly → quarterly / yearly 집계 ============
// /cause API는 source별 고정 단위 (UBIST=60개월, IQVIA=20분기). UBIST에서 분기별/년별 토글 시
// 프론트에서 직접 합산 집계 (BACK_MARKET_API.md 577줄 — 백엔드 단위별 응답 미지원)

export type PeriodUnit = 'monthly' | 'quarterly' | 'yearly'

// 입력 series는 period가 "YYYY-MM" 형식인 monthly 데이터일 때만 변환. 그 외(이미 quarterly/yearly)는 그대로 통과
// value는 sum, yoy_growth_pct는 호출자가 별도 계산 (단위 yoy가 아니라 "연간 성장률"을 단위 점에 반복 매핑하는 패턴 사용)
export function aggregateByUnit<T extends { period: string; value: number; yoy_growth_pct?: number | null }>(
  series: T[],
  unit: PeriodUnit,
): T[] {
  if (series.length === 0) return series
  const sample = series[0]!.period
  const isMonthlyRaw = /^\d{4}-\d{2}$/.test(sample)       // "YYYY-MM" (UBIST 등)
  const isQuarterlyRaw = /^\d{4}-Q\d$/.test(sample)        // "YYYY-Qn" (IQVIA 등)
  if (!isMonthlyRaw && !isQuarterlyRaw) return series      // 그 외 형식은 변환 불가, 그대로

  // raw 단위 == 표시 단위면 변환 불필요
  if (unit === 'monthly' && isMonthlyRaw) return series
  if (unit === 'quarterly' && isQuarterlyRaw) return series
  // quarterly raw → monthly로는 분해 불가 (정보 손실 없음 보장 X) — 안전하게 그대로 반환
  if (unit === 'monthly' && isQuarterlyRaw) return series

  const groups = new Map<string, T[]>()
  for (const item of series) {
    const year = item.period.slice(0, 4)
    let key: string
    if (unit === 'yearly') {
      key = year  // monthly/quarterly raw 둘 다 연도로 묶음
    } else if (isMonthlyRaw) {
      // monthly raw → quarterly 변환
      const month = item.period.slice(5, 7)
      key = `${year}-Q${Math.ceil(Number(month) / 3)}`
    } else {
      // 이 분기에 도달 안 함 (위에서 가드)
      key = item.period
    }
    if (!groups.has(key)) groups.set(key, [])
    groups.get(key)!.push(item)
  }

  // value sum + period 라벨 변경. yoy_growth_pct는 호출자 책임이라 null로 비움
  return Array.from(groups.entries()).map(([key, items]) => {
    const last = items[items.length - 1]!
    return {
      ...last,
      period: key,
      value: items.reduce((s, it) => s + (it.value ?? 0), 0),
      yoy_growth_pct: null,
    }
  })
}

// 차트 5/7 등용 — periods 배열 + 각 item의 value_series(같은 인덱스) 동시 집계
// periods가 monthly("YYYY-MM")일 때만 변환. items 각각의 value_series + series_pct(있으면)도 같이 group sum
// series_pct는 percentage라 sum이 의미 없지만 길이 정합성 위해 같이 합산 (호출자가 직접 사용 시 주의)
export function aggregatePeriodsAndItems<T extends { value_series: number[]; series_pct?: number[] }>(
  periods: string[],
  items: T[],
  unit: PeriodUnit,
): { periods: string[]; items: T[] } {
  if (unit === 'monthly' || periods.length === 0) return { periods, items }
  if (!/^\d{4}-\d{2}$/.test(periods[0]!)) return { periods, items }  // 이미 monthly 형식이 아니면 통과

  // 각 period의 group key 계산 + 매핑
  const uniqueKeys: string[] = []
  const keyToIndices = new Map<string, number[]>()
  periods.forEach((p, idx) => {
    const [year, month] = p.split('-')
    const key = unit === 'quarterly'
      ? `${year}-Q${Math.ceil(Number(month) / 3)}`
      : year!
    if (!keyToIndices.has(key)) {
      keyToIndices.set(key, [])
      uniqueKeys.push(key)
    }
    keyToIndices.get(key)!.push(idx)
  })

  const aggregatedItems = items.map(item => {
    const newValueSeries = uniqueKeys.map(key => {
      const indices = keyToIndices.get(key)!
      return indices.reduce((s, i) => s + (item.value_series[i] ?? 0), 0)
    })
    const newSeriesPct = item.series_pct
      ? uniqueKeys.map(key => {
          const indices = keyToIndices.get(key)!
          return indices.reduce((s, i) => s + (item.series_pct![i] ?? 0), 0)
        })
      : undefined
    return {
      ...item,
      value_series: newValueSeries,
      ...(newSeriesPct ? { series_pct: newSeriesPct } : {}),
    }
  })

  return { periods: uniqueKeys, items: aggregatedItems }
}

// 차트 8용 — value_series_10pt / ms_series_10pt 필드명 다른 케이스
// value: sum, ms: group 마지막 시점 값 (M/S는 percentage라 sum 의미 X — "최근 시점" 정책 유지)
export function aggregatePeriodsAnd10ptItems<T extends { value_series_10pt: number[]; ms_series_10pt: number[] }>(
  periods: string[],
  items: T[],
  unit: PeriodUnit,
): { periods: string[]; items: T[] } {
  if (unit === 'monthly' || periods.length === 0) return { periods, items }
  if (!/^\d{4}-\d{2}$/.test(periods[0]!)) return { periods, items }

  const uniqueKeys: string[] = []
  const keyToIndices = new Map<string, number[]>()
  periods.forEach((p, idx) => {
    const [year, month] = p.split('-')
    const key = unit === 'quarterly'
      ? `${year}-Q${Math.ceil(Number(month) / 3)}`
      : year!
    if (!keyToIndices.has(key)) {
      keyToIndices.set(key, [])
      uniqueKeys.push(key)
    }
    keyToIndices.get(key)!.push(idx)
  })

  const aggregatedItems = items.map(item => ({
    ...item,
    value_series_10pt: uniqueKeys.map(key => {
      const indices = keyToIndices.get(key)!
      return indices.reduce((s, i) => s + (item.value_series_10pt[i] ?? 0), 0)
    }),
    ms_series_10pt: uniqueKeys.map(key => {
      // M/S는 group 마지막 시점 값 (percentage — sum 의미 없음)
      const indices = keyToIndices.get(key)!
      return item.ms_series_10pt[indices[indices.length - 1]!] ?? 0
    }),
  }))

  return { periods: uniqueKeys, items: aggregatedItems }
}

// 억원 정수 콤마 포맷
export const fmtEok = (v: number): string => `${Math.round(v).toLocaleString()}억원`

// 억원 단위 값 → 원 단위 풀 액수 (예: 960억원 → "96,000,000,000원")
export const fmtFullWon = (eok: number): string => `${Math.round(eok * 1e8).toLocaleString()}원`

// 원 단위 raw 매출 → 백만원 정수 콤마
export const fmtBaekman = (won: number): string =>
  won == null || Number.isNaN(won) ? '—' : `${Math.round(won / 1e6).toLocaleString()}백만원`

// ============ 차트 공통 옵션 ============

// 모든 차트 공통 — 컨테이너 높이 기준 fit
export const commonOpts = { responsive: true, maintainAspectRatio: false } as const

// 범례 공통(기획자): circle 20x20, 차트-범례 공백 30, 폰트 13
export const legendBottom = {
  position: 'bottom' as const,
  labels: {
    usePointStyle: true,
    pointStyle: 'circle' as const,
    boxWidth: 20,
    boxHeight: 20,
    padding: 30,
    font: { size: 13 },
  },
}

// 범례 아이콘(원)–텍스트 수직 정렬 보정 (기획자: 아이콘 크기는 유지, 텍스트만 위치 변경)
// chart.js는 usePointStyle 범례에서 원과 텍스트를 같은 중심(y + fontSize/2)에 그림.
const LEGEND_TEXT_NUDGE_Y = 1
export const legendTextAlignPlugin: Plugin = {
  id: 'legendTextAlign',
  afterUpdate(chart) {
    const legend = chart.legend as unknown as {
      ctx: CanvasRenderingContext2D
      draw: () => void
      options?: { labels?: { usePointStyle?: boolean } }
      $textAlignPatched?: boolean
    } | undefined
    if (!legend || legend.$textAlignPatched) return
    // usePointStyle(원 아이콘) 범례에만 적용 — 그 외 범례는 손대지 않음
    if (!legend.options?.labels?.usePointStyle) return
    legend.$textAlignPatched = true
    const originalDraw = legend.draw.bind(legend)
    legend.draw = () => {
      const ctx = legend.ctx
      const orig = ctx.fillText
      ctx.fillText = function (this: CanvasRenderingContext2D, text: string, x: number, y: number, maxWidth?: number) {
        return orig.call(this, text, x, y + LEGEND_TEXT_NUDGE_Y, maxWidth)
      } as typeof ctx.fillText
      try {
        originalDraw()
      } finally {
        ctx.fillText = orig // 범례 외 다른 fillText(축 라벨 등)에 영향 없도록 즉시 복구
      }
    }
  },
}

// 범례 항목 hover 시 마우스 커서 pointer 표시 — chart.js 범례는 canvas 위라 CSS로 못 지정. JS로 canvas style 변경.
// onClick toggle이 살아있는 차트(데이터 사라짐 가능)에 적용해 클릭 가능함을 시각적으로 알림
export const legendHoverPointer = {
  onHover: (e: { native?: { target?: EventTarget | null } | null }) => {
    const t = e.native?.target as HTMLElement | null
    if (t) t.style.cursor = 'pointer'
  },
  onLeave: (e: { native?: { target?: EventTarget | null } | null }) => {
    const t = e.native?.target as HTMLElement | null
    if (t) t.style.cursor = 'default'
  },
}

// ============ 차트 8 (brand_ranking_stacked) 라벨 결정 ============
// 미출시 target — `is_target === true && rank == null && (value === 0 || value == null)` → "브랜드명(미출시)"
// 진짜 기타 — `is_others === true` → "기타"
// 그 외 — brand 그대로 (회사 모드면 company 또는 keyOverride fallback)
// (백엔드는 target 브랜드는 모든 연도 rankings[]에 포함시키되 미출시 연도는 rank=null, value=0으로 채워서 보냄.
//  rank==null만으로 "기타" fallback하면 미출시 target이 "기타"로 표시되어 진짜 기타와 합쳐서 "기타"가 2개로 보임)
export function getBrandDisplayLabel(item: RankItem | undefined, keyOverride?: string, preferCompany = false): string {
  if (!item) return keyOverride ?? ''
  if (item.is_others === true) return '기타'
  const name = preferCompany
    ? (item.company ?? item.brand ?? keyOverride ?? '')
    : (item.brand ?? item.company ?? keyOverride ?? '')
  if (item.is_target === true && item.rank == null && (item.value === 0 || item.value == null)) {
    return `${name}(미출시)`
  }
  return name
}

// ============ 카테고리 분류 ============

// 차트 3 E/I Matrix — 가속/감속(CAGR vs 시장) × 강한/완만(momentum 부호)
export const eiCategoryKey = (b: EiMsItem, marketCagr: number): string => {
  const accel = b.cagr_5y_pct >= marketCagr
  const strong = (b.momentum_score ?? 0) >= 0
  if (accel) return strong ? 'strongAccel' : 'mildAccel'
  return strong ? 'mildDecel' : 'strongDecel'
}

// 차트 4 Growth Contribution & M/S 4분면
export const gcMsCategoryKey = (b: GcMsItem, msAvg: number): typeof GC_MS_CATEGORIES[number]['key'] => {
  const highMs = b.share_pct >= msAvg
  const positive = b.contribution_pct >= 0
  if (highMs && positive) return 'dominant'
  if (!highMs && positive) return 'emerging'
  if (highMs && !positive) return 'mature'
  return 'irrelevant'
}

// ============ 단위 변경 시 from/to 초기화 (수정사항_20260526) ============
// defaultFrom/defaultTo는 차트별 데이터 timeline 첫/마지막 — 하드코딩 제거
// (차트 1·2·7·8: market_size_series / 차트 5: periods_10pt / 차트 6: growth_contribution period)

export const onPeriodInputChange = (
  next: string,
  setPeriod: (v: string) => void,
  setFrom: (v: string) => void,
  setTo: (v: string) => void,
  defaultFrom: string,
  defaultTo: string,
) => {
  setPeriod(next)
  setFrom(defaultFrom)
  setTo(defaultTo)
}

// ============ 범례 opaque 강제 (수정사항_20260526: 범례 컬러 opacity 1) ============
// 차트 dataset의 backgroundColor가 +'66'(40% alpha)일 때 범례만 opaque로 표시

const stripHexAlpha = (c: string): string =>
  /^#[0-9a-fA-F]{8}$/.test(c) ? c.slice(0, -2) : c

// 폴라/도넛용 (1 dataset, backgroundColor=배열, labels=배열)
export const opaqueLabelsForArc = (chart: ChartJS): LegendItem[] => {
  const ds = chart.data.datasets[0]
  const labels = (chart.data.labels as string[]) ?? []
  const bgs = (ds?.backgroundColor as string[]) ?? []
  return labels.map((lbl, i) => {
    const color = stripHexAlpha(bgs[i] ?? '#999')
    return {
      text: lbl,
      fillStyle: color,
      strokeStyle: color,
      lineWidth: 0,
      hidden: false,
      index: i,
    } as LegendItem
  })
}

// ============ 툴팁 색 동그라미 = 아래 범례와 동일(불투명 + 테두리 없음) ============
export const solidTooltipLabelColor = (
  ctx: { chart: ChartJS; datasetIndex: number; dataIndex: number },
): { backgroundColor: string; borderColor: string; borderWidth: number } => {
  const bg = ctx.chart.data.datasets[ctx.datasetIndex]?.backgroundColor
  const raw = Array.isArray(bg) ? bg[ctx.dataIndex] : bg
  const color = typeof raw === 'string' ? stripHexAlpha(raw) : '#999'
  return { backgroundColor: color, borderColor: color, borderWidth: 0 }
}

// 다중 dataset용 (각 dataset = 한 색상 — chart 8 stacked 등)
export const opaqueLabelsForDatasets = (chart: ChartJS): LegendItem[] =>
  chart.data.datasets.map((ds, i) => {
    const bg = ds.backgroundColor
    const color = typeof bg === 'string' ? stripHexAlpha(bg) : '#999'
    return {
      text: typeof ds.label === 'string' ? ds.label : '',
      fillStyle: color,
      strokeStyle: color,
      lineWidth: 0,
      hidden: !chart.isDatasetVisible(i),
      datasetIndex: i,
    } as LegendItem
  })

// ============ 차트 6 contribLabelPlugin: 막대 위/아래에 +/-억 라벨 표시 ============

export const contribLabelPlugin: Plugin<'bar'> = {
  id: 'contribLabel',
  afterDatasetsDraw(chart) {
    const ds = chart.data.datasets[0]
    if (!ds) return
    const raws = ds.data as (number | [number, number] | null)[]
    const first = raws[0]
    const startVal = Array.isArray(first) ? first[1] : 0
    const { ctx } = chart
    ctx.save()
    chart.getDatasetMeta(0).data.forEach((bar, i) => {
      const raw = raws[i]
      if (i === 0 || !Array.isArray(raw)) return  // 시작 막대는 라벨 없음
      const isLast = i === raws.length - 1
      const delta = isLast ? raw[1] - startVal : raw[1] - raw[0]
      // 데이터는 원 단위 — 라벨은 가독성 위해 억으로 환산 표시 (/1e8)
      const label = `${delta >= 0 ? '+' : ''}${Math.round(delta / 1e8).toLocaleString()}억`
      const { x, y, base } = bar.getProps(['x', 'y', 'base'], true)
      ctx.fillStyle = delta >= 0 ? CONTRIB_UP : CONTRIB_DOWN
      ctx.font = 'bold 12px sans-serif'
      ctx.textAlign = 'center'
      if (delta >= 0 || isLast) {
        // 성장 또는 마지막(현재) 막대: 부호 무관 항상 막대 위
        ctx.textBaseline = 'bottom'
        ctx.fillText(label, x, Math.min(y, base) - 4)
      } else {
        // 저하: 막대 아래 (마이너스 느낌)
        ctx.textBaseline = 'top'
        ctx.fillText(label, x, Math.max(y, base) + 4)
      }
    })
    ctx.restore()
  },
}
