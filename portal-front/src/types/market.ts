// ============ 시장분석 도메인 타입 ============
// /api/v1/market/cause (AnalyzePage), /api/v1/market/analysis (DeepAnalyzePage) 공용
// 페이지 간 공유 가능한 API 응답 + 유틸 타입 정의

// ============ 공용 ============

export interface SelectOption {
  value: string
  label: string
}

// ============ /cause 응답 (AnalyzePage) ============

export interface KpiData {
  direct_competition_count: number
  hhi_recent: number
  brand_cagr_5y_pct?: number | null
  brand_cagr_3y_pct?: number | null
  market_cagr_5y_pct?: number | null
  market_cagr_3y_pct?: number | null
  market_size_recent: number
  target_share_pct: number
  target_brand_sales?: number   // 대상 브랜드 매출(원) — "매출 및 시장 내 M/S" 카드
  target_rank?: number          // 시장 순위 (이전: ei_ms_matrix에서 추출)
  target_brand?: string
  target_company?: string
  // 아래는 KPI 추가 표시용으로 사용 가능 (현재 미사용, 타입만 정의)
  top3_share_pct?: number
  brand_cagr_pct?: number
  market_cagr_pct?: number
  brand_value_recent?: number
  brand_share_pct?: number
  ei?: number
  target_ei?: number
  momentum_score?: number
  target_momentum?: number
}

export interface EiMsItem {
  brand: string
  company: string
  cagr_5y_pct: number
  ei: number
  momentum_score: number
  rank_overall: number
  share_pct: number
  value_recent: number
}

export interface GrowthContributor {
  brand: string
  company: string
  contribution: number
  contribution_pct: number
  is_jw?: boolean
  is_target?: boolean
}

export interface CompanyContributor {
  brands: string[]
  company: string
  contribution: number
  contribution_pct: number
  is_jw?: boolean
}

export interface GcMsItem {
  brand: string
  company: string
  is_jw: boolean | null
  is_target: boolean | null
  growth_contribution: number
  contribution_pct: number
  value_recent: number
  share_pct: number
}

export interface MarketSizePt {
  period: string
  value: number
  yoy_growth_pct: number | null
  mom_growth_pct?: number | null
}

export interface HhiYear {
  year: number
  hhi: number
}

export interface CustBrand {
  brand: string
  company?: string
  rank?: number
  is_target: boolean
  value_series: number[]
}

export interface CustComposition {
  brand: string
  pct: number
}

export interface CustView {
  target_name: string
  periods: string[]
  trend_brands: CustBrand[]
  composition: CustComposition[]
}

export interface RankItem {
  brand?: string
  company?: string
  rank?: number
  ms_pct: number
  value?: number
  is_jw?: boolean | null
  is_target?: boolean | null
  is_others?: boolean | null
}

export interface RankYear {
  year: string
  rankings: RankItem[]
}

export interface Lv5Brand {
  brand: string
  company?: string
  rank?: number
  is_target: boolean
  is_jw?: boolean
  is_others?: boolean  // true면 "기타" 항목 — value_series_10pt 길이가 60 등으로 다르게 올 수 있음
  value_recent_100m: number
  value_series_10pt: number[]
  ms_series_10pt: number[]
  data_quality?: {
    available?: boolean
    reason?: string
  }
}

export interface Lv5Data {
  // 셀렉트박스 노출/옵션은 응답이 직접 결정 (PDF 매핑 테이블 없이 동적 처리)
  empty?: boolean              // true면 데이터 없음 → Level 셀렉트박스에서 숨김
  level_label?: string         // Level 표시 라벨 (예: "Class", "Molecule")
  level_value?: string         // Level 키
  total_market_value?: number  // 해당 Level 총 시장 규모
  all_options?: string[]       // Sub 셀렉트박스 옵션 (응답이 노출 옵션 직접 제공)
  default_option?: string      // Sub 셀렉트박스 기본 선택값
  periods_10pt: string[]
  default_value?: string | null  // (구) sub-class 기본 선택값 — default_option fallback용
  values: {
    value?: string             // sub-class 키 (예: "RSV/EZE", "Statin")
    is_default?: boolean       // (구) default_value/default_option fallback용
    ms_pct?: number            // 해당 sub-class가 전체에서 차지하는 비율
    total_value?: number       // 해당 sub-class 총 시장 규모 (원)
    data_quality?: {
      available?: boolean
      reason?: string
    }
    brands_in_value: Lv5Brand[]
  }[]
}

export interface AnalysisItem {
  name: string
  rank?: number
  is_others?: boolean | null
  // analysis_level_market_status 는 recent_share_pct 가 null 로 옴 → 폴라는 series_pct 마지막값 fallback
  recent_share_pct: number | null
  series_pct: number[]
  // 매출/처방량 시계열 — measure 파라미터에 따라 단위 다름 (sales: 원, volume: Rx 등)
  value_series: number[]
}

// 차트 5 "분석 Level별 매출 추이 및 M/S" + 신규 차트 "주요 고객별 분석 Level 시장 현황" 공용 구조
export interface AnalysisLevels {
  period_unit: string
  channels: string[]
  levels: string[]
  periods_monthly: string[]
  periods_quarterly: string[]
  // by_channel: 라인(매출 추이) 시계열 — rank=0 '전체' 포함, recent_share_pct는 null로 옴
  // ms_by_channel: ★ 폴라(M/S) 전용 — '전체' 없음, recent_share_pct 정상값 (백엔드가 분리 제공)
  data: Record<string, {
    by_channel: Record<string, AnalysisItem[]>
    ms_by_channel?: Record<string, AnalysisItem[]>
  }>
}

export interface CauseData {
  kpi: KpiData
  ei_ms_matrix: {
    ms_avg_pct: number
    share_avg_pct: number
    data: EiMsItem[]
  }
  growth_contribution: {
    market_start: number
    market_end: number
    market_growth?: number
    period_start: string
    period_end: string
    by_brand: {
      top_contributors: GrowthContributor[]
      others_total: number
    }
    by_company?: {
      top_contributors: CompanyContributor[]
      others_total?: number
    }
    // 기간별 분리 (1y/2y/3y/4y/5y) — 5y는 최상위 키와 동일. 토글로 기간 변경 가능.
    windows?: Record<'1y' | '2y' | '3y' | '4y' | '5y', {
      market_start: number
      market_end: number
      market_growth?: number
      period_start: string
      period_end: string
      by_brand: {
        top_contributors: GrowthContributor[]
        others_total: number
      }
      by_company?: {
        top_contributors: CompanyContributor[]
        others_total?: number
      }
    }>
  }
  growth_contribution_ms_matrix: {
    ms_avg_pct: number
    share_avg_pct: number
    data: GcMsItem[]
  }
  sources_data: {
    market_size_series: MarketSizePt[]
    hhi_series_5y: HhiYear[]
  }
  target_customer_competition: {
    targets: string[]
    views: CustView[]
  }
  level_top5_trend: {
    status?: 'unavailable' | string
    reason?: string
    note?: string
    available_levels: { key: string; label: string }[]
    by_level: Record<string, Lv5Data>
  }
  brand_ranking_stacked: { years: string[]; yearly: RankYear[] }
  company_ranking_stacked: { years: string[]; yearly: RankYear[] }
  // 회사 단위로 재계산된 HHI 시계열 (브랜드 HHI는 sources_data.hhi_series_5y, 회사는 이쪽)
  company_concentration_trend?: { periods: string[]; hhi_values: number[] }
  analysis_levels: AnalysisLevels
  // 신규 차트 "주요 고객별 분석 Level 시장 현황" — analysis_levels 와 동일 구조 (channels 가 진료과별)
  analysis_level_market_status?: AnalysisLevels
}

export interface CauseApiResponse {
  status: string
  result: {
    brand: string
    brand_name?: string 
    brand_key?: string  
    source?: string  // 실제 사용된 source (요청과 다르면 fallback 발생)
    market_meta?: {
      atc_codes?: string[]
      view?: 'strategic_ml' | 'strategic_cd' | 'general'
      view_source_id?: string
      market_name?: string
      is_dual_source?: boolean  // true면 UBIST/IQVIA 둘 다 지원
      source_label?: string  // is_dual_source=false일 때 유일하게 지원하는 source ("UBIST" | "IQVIA")
      source_latest_period?: string | null
      selected_brand_latest_period?: string | null
    } | null
    data: CauseData | null
    reason?: string
    markets?: { market_id: string; is_primary?: boolean }[]
  }
}

// ============ /analysis 응답 (DeepAnalyzePage) ============

export interface AnalysisEvent {
  id: string
  title: string
  date: string
  category: string
  category_label: string
  summary: string
  body_full: string | null
  impact_score: number
  source: string
  period_map: { UBIST?: string; IQVIA?: string }
  on_list: boolean
  on_chart: boolean
  // 원문보기 링크 (퍼블 2026-06-02 추가). 백엔드는 `url`과 `source_url` 둘 다 동일 값으로 내려줌
  url?: string | null
  source_url?: string | null
}

// bullets 항목은 보통 string이지만, 백엔드가 일부를 객체로 섞어 보내기도 함 (phenomenon에서 관측).
// string·객체 둘 다 안전하게 렌더해야 함 (DeepAnalyzePage의 bulletToText).
export interface AiBullet {
  title: string
  basis?: string
  stage?: string
}

export interface AiSection {
  title: string
  body: string
  bullets: (string | AiBullet)[]
  evidence?: (string | AiBullet)[]
}

export interface AiAnalysis {
  generated_at: string
  phenomenon: AiSection
  cause: AiSection
  prediction: AiSection
  recommendation: AiSection
}

export interface ForecastBrand {
  brand: string
  company: string
  is_target: boolean
  is_jw: boolean
  rank: number
  history_values: (number | null)[]
  forecast_values: number[]
  forecast_method?: string
  forecast_model?: {
    name?: string | null
    variant?: string | null
    selection_reason?: string | null
    selection_policy?: string | null
  } | null
  // M/S 시계열 — history는 history_values와 동일 길이, forecast는 12개월만 옴 (forecast_values는 121개)
  history_ms_pct?: (number | null)[]
  forecast_ms_pct?: number[]
}

export interface ForecastCombo {
  unit_label: string
  period_unit: string
  target_brand: string
  history_periods: string[]
  forecast_periods: string[]
  brands: ForecastBrand[]
}

export interface SimScenario {
  values: number[]
}

export interface SimBrandData {
  history_periods: string[]
  history_values: number[]
  forecast_periods: string[]
  scenarios: {
    base?: SimScenario
    lower?: SimScenario
    upper?: SimScenario
  }
}

export interface SimCombo {
  unit_label: string
  source_granularity?: string
  available_brands: { brand: string; rank: number; is_jw: boolean | null; is_target: boolean | null }[]
  by_brand: Record<string, SimBrandData>
}

export interface BrandStrengthItem {
  candidate_index?: number
  confidence?: string
  metric?: string
  narrative: string
  numbers?: Record<string, number>
  period?: string
  slice?: string
}
export interface BrandProfileDisplay {
  class_recode?: string | null
  molecule_recode?: string | null
  molecule_raw?: string[] | null
  molecule_components?: string[] | null
  strength_pack_recode?: string | null
  strength_pack_raw?: string[] | null
  dosage_form_recode?: string | null
  dosage_form_raw?: string[] | null
  nhi_type_recode?: string | null
  class?: string | null
  molecule?: string | null
  dosage_form?: string | null
  nhi_type?: string | null
}
export interface BrandFactorValues {
  [key: string]: string[] | undefined
  seller?: string[]
  molecule_strength?: string[]
  form?: string[]
  route?: string[]
  reimbursement?: string[]
  mfr_name_kor?: string[]
  molecule_type?: string[]
  molecule_desc?: string[]
  pack_desc?: string[]
  strength?: string[]
  nhi_type?: string[]
}
export interface BrandFactorEntry {
  brand: string
  brand_key?: string
  role?: 'selected' | 'competitor'
  rank?: number | null
  factors?: { available?: boolean; reason?: string | null; values?: BrandFactorValues }
  strength?: {
    profile_display?: BrandProfileDisplay
    strength_items?: BrandStrengthItem[]
    limitations?: unknown[]
  }
}
export interface BrandFactors {
  iqvia?: BrandFactorEntry[]
  ubist?: BrandFactorEntry[]
}

export interface AnalysisResult {
  brand: string
  brand_name?: string
  market_id: string
  market_name?: string
  generated_at: string
  available_combos: string[]
  market_meta: {
    default_source: string
    sources: string[]
    source_latest_period?: string | null
  }
  data: {
    forecast: {
      method?: string
      disclaimer?: string
      is_statistical_model?: boolean
      backtest_available?: boolean
      event_regressor_enabled?: boolean
      phase29_poc?: boolean
      by_combo: Record<string, ForecastCombo>
    }
    simulation: { by_combo: Record<string, SimCombo> }
    events: AnalysisEvent[]
    ai_analysis: AiAnalysis
    ai_analysis_short?: AiAnalysis
    ai_analysis_long?: AiAnalysis
    brand_factors?: BrandFactors
  }
}

export interface AnalysisApiResponse {
  status: string
  result: AnalysisResult
}

export interface BrandActivityFilters {
  atc4: string[]
  molecule: string[]
  channel: string[]
}

export interface SeriesMeasureData {
  source: string                    
  absolute: Record<string, number>  
  ratio: Record<string, number>    
}
export interface SeriesBrand {
  brand_name: string
  brand_key?: string
  product_code?: string
  is_jw: boolean
  is_selected: boolean
  sales_rank?: number | null
  series: {
    activity: SeriesMeasureData
    sales?: SeriesMeasureData
    unit: SeriesMeasureData
    counting_unit: SeriesMeasureData
    dosage_unit: SeriesMeasureData
  }
}
export interface SeriesData {
  scope: {
    view: string
    market_id: string
    market_name: string
    selected_brand: { brand_key: string; product_code: string }
    quarters: string[]             // 분기 키 축 (sales/unit/counting_unit/dosage_unit)
    activity_months: string[]      // 월 키 축 (activity=콜 수 전용)
    measures: string[]
    mode?: string
    csd_market?: string
    csd_markets?: string[]
  }
  brands: SeriesBrand[]
}
export interface SeriesApiResponse {
  status: string
  result: { data: SeriesData | null }
}

// --- /brand/topics (키워드 점유 표재활용) ---
export interface TopicShare {
  topic_id: string
  label: string
  share_pct: number      // 비율 %
  row_count: number      // 행 수 (백엔드 직접 제공 — event_count×share 계산 불필요)
}
export interface BrandSpecificTopic {
  label: string
  share_pct: number
  row_count?: number     // 고유 키워드 — 없으면 event_count×share_pct로 계산 fallback
  definition: string
}
export type TopicDataStatusCode =
  | 'available'
  | 'zero'
  | 'source_absent'
  | 'mapping_failure'
  | 'unknown'
  | 'identity_mismatch'
export interface TopicDataStatus {
  code: TopicDataStatusCode
  label: string | null
  source_row_count?: number
  classified_row_count?: number
  guard_valid_row_count?: number
}
export interface TopicsBrand {
  brand_name: string
  company_name?: string
  is_jw: boolean
  is_selected: boolean
  event_count: number                  // 0이면 topic_shares 빈 배열
  data_status?: TopicDataStatus
  etc_pct: number
  topic_shares: TopicShare[]              // 1~5위 (share_pct 내림차순)
  brand_specific_topics: BrandSpecificTopic[]  // 고유 키워드
}
export interface TopicsData {
  scope: {
    view: string
    market_id: string
    selected_brand: string               // topics scope는 문자열 (series와 다름)
    top_n: number
    period_start?: string
    period_end?: string
  }
  brands: TopicsBrand[]
}
export interface TopicsApiResponse {
  status: string
  result: { data: TopicsData | null }
}

// --- /brand/matrix (19p INTEREST 폴라·21p 버블) — 🚨 백엔드 DTO 수정 대기 ---
export interface MatrixBrand {
  brand_name: string
  is_jw: boolean
  is_selected: boolean
  event_count: number                       // 버블 면적 (0이면 score 3종 null)
  // ⚠️ distribution은 비율(0~1) 아니라 정수 counts (합 = event_count) → %는 /event_count
  interest_distribution: Record<string, number>       // {VERY USEFUL, SOMEWHAT USEFUL, NOT AT ALL}
  rx_frequency_distribution: Record<string, number>
  prescription_evolution_distribution: Record<string, number>  // {increase..., remain unchanged, decrease}
  interest_score: number
  rx_frequency_score: number
  prescription_evolution_score: number      // 21p 버블 Y축 (2026-07-08 실측 확정)
}
export interface MatrixData {
  scope: {
    view: string
    market_id: string
    selected_brand: string
  }
  brands: MatrixBrand[]
  levels: {
    interest: string[]
    rx_frequency: string[]
    prescription_evolution: string[]
  }
  market_average: {
    interest_distribution: Record<string, number>
    rx_frequency_distribution: Record<string, number>
    prescription_evolution_distribution: Record<string, number>
    interest_score: number
    rx_frequency_score: number
    prescription_evolution_score: number    // 21p 기준선 Y축
  }
}
export interface MatrixApiResponse {
  status: string
  result: { data: MatrixData | null }
}

// ============ /dynamic + 필터 옵션 (AnalyzePage ATC 필터) ============

export type AssayMode = 'jw' | 'market'

export interface FilterOptionValue {
  key: string
  value: string
  row_count?: number
  default?: boolean
  selected?: boolean
  flag?: boolean
}

export interface FilterDimension {
  dimension_type: string
  label: string
  values: FilterOptionValue[]
}

export interface DimensionHierarchyNode {
  key: string
  value: string
}

export interface DimensionHierarchyChild extends DimensionHierarchyNode {
  parent_keys: string[]
}

export interface DimensionHierarchy {
  parent_dimension: string
  child_dimension: string
  relation: 'one_to_many' | string
  parents: DimensionHierarchyNode[]
  children: DimensionHierarchyChild[]
}

export interface AtcOptionItem {
  key: string
  value?: string
  label?: string
  level: string | number
  parent?: string | null
  default?: boolean
  selected?: boolean
  flag?: boolean
}

export interface AtcOptionsTree {
  atc1?: AtcOptionItem[]
  atc2?: AtcOptionItem[]
  atc3?: AtcOptionItem[]
  atc4?: AtcOptionItem[]
  selectable_levels?: string[]
}

export interface FilterOptionsResponse {
  view?: string
  source?: string
  market_id?: string
  brand?: string
  dimensions?: FilterDimension[]
  dimension_hierarchies?: DimensionHierarchy[]
  atc?: AtcOptionsTree
  channel_axis?: {
    ubist?: {
      facility?: FilterOptionValue[]
      specialty?: FilterOptionValue[]
      pairs?: FilterOptionValue[]
    }
    iqvia?: {
      audit_code?: FilterOptionValue[]
    }
  }
  default_selections?: Record<string, string[]>
  applied_selections?: Record<string, string[]>
  brand_matched?: Record<string, string[]>
}

export interface DynamicFilterContext {
  assayMode: AssayMode
  atc4: string[]
  analysisLevel: Record<string, string[]>
}

export interface DynamicRequestBody {
  source: string
  measure: string
  filters: {
    atc4?: string[]
    view_kind?: string
    focus_brand_key?: string
    analysis_level?: {
      ubist?: Record<string, string[]>
      iqvia?: Record<string, string[]>
    }
  }
  options?: {
    top_n?: number
    period_range?: { start?: string; end?: string } | null
  }
}

export interface DynamicCauseResult {
  brand?: string
  brand_name?: string
  brand_key?: string
  source?: string
  measure?: string
  view?: string
  market_id?: string
  unit_label?: string
  market_meta?: CauseApiResponse['result']['market_meta']
  data: CauseData | null
  reason?: string
  markets?: { market_id: string; is_primary?: boolean }[]
}

export interface DynamicApiResponse {
  status: string
  result: {
    status?: string
    result?: DynamicCauseResult
  }
}
