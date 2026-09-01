import type { InspectionCall, JsonValue } from './answerInspection'

export interface TraceToolResult {
  readonly sequence?: number
  readonly source: string
  readonly query: string
  readonly status: string
  readonly elapsed_ms?: number
  readonly payload: JsonValue
}

export interface UnnarratedRecord {
  readonly record_id: string
  readonly reason_code: string
}

export interface SelectionPolicy {
  readonly rule?: string
  readonly ranked?: boolean
}

export interface ProjectedTracePayload {
  readonly value: JsonValue
  readonly hiddenFieldCount: number
}

export type TraceMatch = {
  readonly kind: 'matched'
  readonly result: TraceToolResult
} | {
  readonly kind: 'missing' | 'ambiguous'
}

const SOURCE_BY_PUBLIC_LABEL: Readonly<Record<string, string>> = {
  '내부 데이터마트': 'mart',
  '내부데이터마트': 'mart',
  '식품의약품안전처': 'nedrug',
  '식약처': 'nedrug',
  '의약품안전나라': 'nedrug',
  '건강보험심사평가원': 'hira',
  HIRA: 'hira',
  FDA: 'openfda',
  OpenFDA: 'openfda',
  'ClinicalTrials.gov': 'clinicaltrials',
  '웹 뉴스': 'web',
  '웹 검색': 'web',
  '식품의약품안전처 의약품 특허목록': 'patent',
  '특허 자료': 'patent',
  '특허': 'patent',
  '업로드 문서': 'document',
}

const STRUCTURAL_FIELDS = new Set([
  'calls', 'render_data', 'payload', 'items', 'records', 'rows', 'studies', 'series', 'periods',
])

const COMMON_CALL_FIELDS = new Set([
  'status', 'elapsed_ms', 'render_data',
])

const LANE_FIELDS: Readonly<Record<string, ReadonlySet<string>>> = {
  mart: new Set([
    'market', 'market_id', 'market_name', 'scope', 'scope_label', 'level', 'level_segments', 'view_type',
    'period', 'periods', 'anchor_brand', 'member_brands', 'total_brands_in_market', 'market_size_recent_krw',
    'market_size_억원', 'market_start_krw', 'market_end_krw', 'market_growth_pct', 'market_mom_pct',
    'market_yoy_pct', 'market_cqgr_pct', 'market_cmgr_pct', 'market_value', 'market_value_series',
    'market_prescription_volume', 'hhi_recent', 'hhi_end', 'cr5_end_pct', 'query_result_id', 'source_label',
    'brand', 'company', 'competitors', 'brand_sales_krw', 'brand_sales_억원', 'brand_growth_pct',
    'brand_mom_pct', 'brand_yoy_pct', 'brand_cqgr_pct', 'brand_cmgr_pct', 'brand_value_series_10pt',
    'sales_krw', 'sales_억원', 'sales_start_krw', 'sales_end_krw', 'sales_delta_krw', 'ms_pct',
    'ms_recent_pct', 'from_ms_pct', 'to_ms_pct', 'share_start_pct', 'share_end_pct', 'share_delta_pctp',
    'share_min_pct', 'share_max_pct', 'share_min_period', 'share_max_period', 'rank', 'rank_start', 'rank_end',
    'trend_direction', 'trend_months', 'turning_kind', 'turning_point', 'history_points', 'missing_periods',
    'value', 'value_krw', 'value_억원', 'value_recent', 'value_recent_억원', 'value_delta', 'value_delta_krw',
    'value_label', 'unit_label', 'measure', 'metric', 'metrics', 'source_status', 'name', 'from_period',
    'to_period', 'denominator_end', 'excess_growth_pctp',
  ]),
  nedrug: new Set([
    'request_limit', 'source_limit_reached', 'resultCode', 'totalCount', 'message', 'item_seq', 'brand',
    'ITEM_SEQ', 'ITEM_NAME', 'ITEM_ENG_NAME', 'ENTP_NAME', 'ENTP_ENG_NAME', 'ITEM_PERMIT_DATE',
    'INGR_NAME', 'MAIN_ITEM_INGR', 'MATERIAL_NAME', 'ATC_CODE', 'ETC_OTC_CODE', 'EDI_CODE', 'BAR_CODE',
    'PACK_UNIT', 'STORAGE_METHOD', 'VALID_TERM', 'TOTAL_CONTENT', 'PERMIT_KIND_NAME', 'RARE_DRUG_YN',
    'REEXAM_DATE', 'CANCEL_DATE', 'CANCEL_NAME', 'CHANGE_DATE', 'NEWDRUG_CLASS_NAME',
  ]),
  hira: new Set([
    'message', 'error', 'period_coverage', 'axis_coverage', 'requested_periods', 'provided_periods',
    'requested_axis', 'actual_axis', 'selection_mode', 'period', 'availability_status', 'received_count',
    'sickCd', 'searchText', 'diseaseType', 'patient_count', 'count', 'year', 'month', 'value', 'unit',
    'request_limit', 'source_limit_reached', 'resultCode', 'totalCount', 'inpatOpat', 'sex', 'sickEngNm',
    'sickNm', 'ptntCnt', 'rvdInsupBrdnAmt', 'rvdRpeTamtAmt', 'specCnt', 'vstDdcnt', 'units',
    'sexBreakdown', 'sex_aggregation_applied', 'sex_labels_exposed', 'original_requested_axis',
    'requested_year', 'document_lookup', 'document', 'outcome', 'subject', 'error_code', 'brand', 'brand_name',
    'title', 'raw_text', 'source_date', 'collected_at', 'notice_number', 'source_notice_id',
    'matching_basis', 'match_candidates', 'request', 'lookup_mode', 'source_url',
  ]),
  openfda: new Set([
    'message', 'error', 'results', 'meta', 'total', 'count', 'openfda', 'patient', 'reaction', 'drug',
    'safetyreportid', 'receivedate', 'serious', 'manufacturer_name', 'brand_name', 'generic_name',
  ]),
  clinicaltrials: new Set([
    'totalCount', 'query_manifest', 'coverage', 'query_policy', 'external_claim_policy', 'query_id',
    'query_type', 'compiled_expression', 'parameters', 'source_queries', 'total_reported', 'total_unfiltered',
    'records_after_status_filter', 'records_received', 'records_unique', 'records_relevant',
    'records_direct_relevance_confirmed', 'records_direct_relevance_unconfirmed',
    'records_excluded_by_status', 'records_excluded_by_relevance', 'relevance_exclusions',
    'relevance_assessments', 'page_count', 'pagination_complete', 'partial_reason', 'nct_id', 'brief_title',
    'official_title', 'overall_status', 'study_type', 'phases', 'sponsor', 'collaborators', 'conditions',
    'interventions', 'intervention_details', 'comparators', 'enrollment', 'start_date',
    'primary_completion_date', 'completion_date', 'last_update_date', 'primary_outcomes',
    'secondary_outcomes', 'brief_summary', 'detailed_description', 'eligibility_criteria', 'sex',
    'minimum_age', 'maximum_age', 'facilities', 'countries', 'has_results', 'relevance_status',
    'relevance_matched_tokens', 'matched_query', 'reason_code', 'title', 'description', 'name', 'type',
    'measure', 'time_frame', 'url', 'count', 'other_names',
  ]),
  web: new Set([
    'provider', 'query', 'status', 'message', 'error', 'error_type', 'request_issued', 'response_received',
    'verification_notice', 'parser_outcome', 'title', 'url', 'snippet', 'published_at', 'published_at_label',
    'published_date',
  ]),
  patent: new Set([
    'request_limit', 'source_limit_reached', 'resultCode', 'totalCount', 'message', 'patent_lanes',
    'kr_primary', 'us_secondary', 'news', 'scope', 'authority', 'role', 'records_received', 'records_unique',
    'source_limit', 'identifier_exclusions', 'product_patent_rows', 'non_product_exclusions',
    'product_patent_edges', 'pms_periods', 'relevance_exclusions', 'relevance_decisions', 'lane',
    'jurisdiction', 'product', 'ingredient', 'patent_no', 'invention_title', 'patent_type', 'page_group',
    'listed_status', 'status', 'extinction_reason', 'event_type', 'listed_end_date', 'expiration_date',
    'product_item_seq', 'product_item_name', 'pms_period_start', 'pms_period_end', 'owner', 'url',
    'status_variants', 'source_row_count', 'title', 'snippet', 'event_date', 'published_at', 'decision',
    'reason', 'matched_brand_or_ingredient_tokens', 'matched_company_tokens', 'record_index',
    'content_length', 'content_sha256', 'ITEM_SEQ', 'ITEM_NAME', 'INGR_NAME', 'DOMESTIC_PATENT_NO',
    'KOR_PAT_NO', 'KOR_APPLY_NO', 'KOR_NAME_OF_INVENTION', 'KOR_STATUS', 'KOR_EXP_DATE', 'PATENTEE',
    'CLASS_NO', 'CONT_QY', 'DOMESTIC_END_DATE', 'DOMESTIC_INVN_NM', 'DOMESTIC_LWST_YN',
    'DOMESTIC_PATENT_STATUS', 'ENTP_NAME', 'INGR_ENG_NAME', 'ITEM_ENG_NAME', 'PAGE_GB_NM',
    'PATENT_GB_CODE', 'PMS_END_DATE', 'SHAPE',
  ]),
  document: new Set([
    'document_id', 'file_name', 'page', 'pages', 'chunk_id', 'text', 'title', 'score', 'url',
  ]),
}

const SENSITIVE_KEY = /(?:api[_-]?key|authorization|credential|function|internal|password|query_sql|safe_url|secret|sql|token|tool)/i
const INTERNAL_VALUE = /(?:\.svc(?:\.cluster\.local)?|localhost|127\.0\.0\.1|10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+)/i
const SQL_VALUE = /\b(?:select|insert|update|delete)\b[\s\S]{0,80}\b(?:from|into|set)\b/i

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function isJsonValue(value: unknown): value is JsonValue {
  if (value === null || ['boolean', 'number', 'string'].includes(typeof value)) return true
  if (Array.isArray(value)) return value.every(isJsonValue)
  return isRecord(value) && Object.values(value).every(isJsonValue)
}

export function parseTraceToolResults(value: unknown): TraceToolResult[] {
  if (!Array.isArray(value)) return []
  return value.flatMap(item => {
    if (!isRecord(item)
      || typeof item.source !== 'string'
      || typeof item.query !== 'string'
      || typeof item.status !== 'string'
      || (item.sequence !== undefined && !Number.isInteger(item.sequence))
      || (item.elapsed_ms !== undefined && (typeof item.elapsed_ms !== 'number' || !Number.isFinite(item.elapsed_ms)))
      || !isJsonValue(item.payload)) return []
    return [{
      ...(item.sequence === undefined ? {} : { sequence: item.sequence as number }),
      source: item.source,
      query: item.query,
      status: item.status,
      ...(typeof item.elapsed_ms === 'number' ? { elapsed_ms: item.elapsed_ms } : {}),
      payload: item.payload,
    }]
  })
}

export function parseUnnarratedRecords(value: unknown): UnnarratedRecord[] {
  if (!isRecord(value) || !Array.isArray(value.unnarrated_records)) return []
  return value.unnarrated_records.flatMap(item => (
    isRecord(item) && typeof item.record_id === 'string' && typeof item.reason_code === 'string'
      ? [{ record_id: item.record_id, reason_code: item.reason_code }]
      : []
  ))
}

export function traceSourceForInspectionLabel(label: string): string | undefined {
  return SOURCE_BY_PUBLIC_LABEL[label]
}

function persistedTrace(value: unknown): Record<string, unknown> | undefined {
  if (!isRecord(value) || !isRecord(value.genos_persist)) return undefined
  const answer = value.genos_persist.chat_agent_answer
  if (!isRecord(answer) || !isRecord(answer.trace)) return undefined
  return answer.trace
}

export function traceToolResultsFromChatLogData(value: unknown): TraceToolResult[] {
  return parseTraceToolResults(persistedTrace(value)?.tool_results)
}

export function unnarratedRecordsFromChatLogData(value: unknown): UnnarratedRecord[] {
  return parseUnnarratedRecords(persistedTrace(value)?.lossless_spine)
}

export function selectionPolicyFromChatLogData(value: unknown): SelectionPolicy | undefined {
  return parseSelectionPolicy(persistedTrace(value))
}

function matchKey(source: string, query: string): string {
  return `${source}\u0000${query}`
}

export function matchInspectionCallsToTrace(
  calls: readonly InspectionCall[],
  results: readonly TraceToolResult[],
): ReadonlyMap<number, TraceMatch> {
  const inspectionGroups = new Map<string, InspectionCall[]>()
  const traceGroups = new Map<string, TraceToolResult[]>()
  for (const call of calls) {
    const source = SOURCE_BY_PUBLIC_LABEL[call.source_label]
    if (!source) continue
    const key = matchKey(source, call.request_parameters.query)
    inspectionGroups.set(key, [...(inspectionGroups.get(key) ?? []), call])
  }
  for (const result of results) {
    const key = matchKey(result.source, result.query)
    traceGroups.set(key, [...(traceGroups.get(key) ?? []), result])
  }
  const matches: Array<readonly [number, TraceMatch]> = calls.map(call => {
    if (call.trace_sequence !== undefined) {
      const sequenceMatches = results.filter(result => result.sequence === call.trace_sequence)
      if (sequenceMatches.length === 1) {
        return [call.sequence, { kind: 'matched', result: sequenceMatches[0]! } as const]
      }
      if (sequenceMatches.length > 1) {
        return [call.sequence, { kind: 'ambiguous' } as const]
      }
    }
    const source = SOURCE_BY_PUBLIC_LABEL[call.source_label]
    if (!source) return [call.sequence, { kind: 'missing' } as const]
    const key = matchKey(source, call.request_parameters.query)
    const callGroup = inspectionGroups.get(key) ?? []
    const resultGroup = traceGroups.get(key) ?? []
    if (callGroup.length === 1 && resultGroup.length === 1) {
      return [call.sequence, { kind: 'matched', result: resultGroup[0]! } as const]
    }
    return [call.sequence, { kind: callGroup.length > 1 || resultGroup.length > 1 ? 'ambiguous' : 'missing' } as const]
  })
  return new Map(matches)
}

function sanitizeString(value: string): string {
  if (INTERNAL_VALUE.test(value) || SQL_VALUE.test(value)) return '내부 값 비공개'
  return value
}

function projectValue(source: string, value: JsonValue): ProjectedTracePayload {
  if (typeof value === 'string') return { value: sanitizeString(value), hiddenFieldCount: 0 }
  if (value === null || typeof value !== 'object') return { value, hiddenFieldCount: 0 }
  if (Array.isArray(value)) {
    const projected = value.map(item => projectValue(source, item))
    return {
      value: projected.map(item => item.value),
      hiddenFieldCount: projected.reduce((sum, item) => sum + item.hiddenFieldCount, 0),
    }
  }
  const allowed = LANE_FIELDS[source] ?? new Set<string>()
  const output: Record<string, JsonValue> = {}
  let hiddenFieldCount = 0
  for (const [key, item] of Object.entries(value)) {
    if (SENSITIVE_KEY.test(key)
      || (!STRUCTURAL_FIELDS.has(key) && !COMMON_CALL_FIELDS.has(key) && !allowed.has(key))) {
      hiddenFieldCount += 1
      continue
    }
    const projected = projectValue(source, item)
    output[key] = projected.value
    hiddenFieldCount += projected.hiddenFieldCount
  }
  return { value: output, hiddenFieldCount }
}

export function projectTracePayload(source: string, payload: JsonValue): ProjectedTracePayload {
  return projectValue(source, payload)
}

export function parseSelectionPolicy(value: unknown): SelectionPolicy | undefined {
  if (!isRecord(value)) return undefined
  const candidate = isRecord(value.selection) ? value.selection : value
  const rule = typeof candidate.selection_rule === 'string' ? candidate.selection_rule : undefined
  const ranked = typeof candidate.selection_is_ranked === 'boolean' ? candidate.selection_is_ranked : undefined
  if (rule === undefined && ranked === undefined) return undefined
  return { ...(rule === undefined ? {} : { rule }), ...(ranked === undefined ? {} : { ranked }) }
}

export function selectionNotice(policy: SelectionPolicy | undefined, rowCount: number): string | undefined {
  if (rowCount !== 40) return undefined
  if (policy?.ranked === false) return '관련도 정렬 없이 상류 반환 순서에서 선택된 임의 40건입니다.'
  if (policy?.ranked === true) return `정렬 기준 ${policy.rule ?? '백엔드 지정'}으로 선택된 40건입니다.`
  return '정렬 플래그가 제공되지 않아 백엔드가 전달한 40건을 그대로 표시합니다.'
}

export function tracePreservationNotice(source: string): string | undefined {
  if (source === 'patent') return '상류 응답 일부만 보존됐습니다. 특허 MCP 원문은 길이와 해시만 남습니다.'
  if (source === 'web') return '상류 원문은 보존되지 않았습니다. 파싱된 웹 검색 결과만 표시합니다.'
  if (['nedrug', 'hira', 'openfda'].includes(source)) return '상류 응답 일부만 보존됐습니다. MCP 결과 배열에는 수집 상한이 적용됩니다.'
  return undefined
}

export const TRACE_OUTPUT_WHITELISTS = LANE_FIELDS
