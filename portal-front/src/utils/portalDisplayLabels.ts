import type { InspectionCounts, JsonValue } from './answerInspection'

const SOURCE_LABELS: Readonly<Record<string, string>> = {
  'aux:patent:news': '특허 뉴스',
  'aux:web:news': '웹 뉴스',
  mart: '내부 데이터마트',
  hira: '건강보험심사평가원',
  nedrug: '식품의약품안전처',
  patent: '특허',
  clinicaltrials: 'ClinicalTrials.gov',
  FDA: 'OpenFDA',
  openfda: 'OpenFDA',
  web: '웹 검색',
  document: '업로드 문서',
}

const PARAMETER_LABELS: Readonly<Record<string, string>> = {
  query: '검색어',
  calls: '호출 목록',
  item_name: '품목명',
  limit: '최대 건수',
  sickCd: '상병 코드',
  sick_cd: '상병 코드',
  year: '연도',
  years: '연도 목록',
  ingredient: '성분명',
  brand: '브랜드명',
  keyword: '키워드',
  max_results: '최대 결과 수',
  page: '페이지',
  page_size: '페이지당 건수',
  records: '받은 항목',
  returned: '수신 건수',
  identifiers: '식별자',
  interventions: '중재',
  relevance_status: '관련성 상태',
  sponsor: '의뢰기관',
  title: '제목',
  record_id: '항목 식별자',
  record_ids: '항목 식별자 목록',
  source_label: '소스명',
  slot_id: '항목 위치',
  lane: '소스',
  ptntCnt: '환자 수',
  inpatOpat: '진료 구분',
  notice_number: '고시 번호',
  source_notice_id: '원문 식별자',
  source_date: '고시일',
  source_url: '원문 URL',
  matching_basis: '대응 근거',
  INGR_ENG_NAME: '성분 영문명',
  INGR_NAME: '성분명',
  ITEM_ENG_NAME: '품목 영문명',
  ITEM_NAME: '품목명',
  ENTP_NAME: '업체명',
  SHAPE: '형태',
  CONT_QY: '함량',
  CLASS_NO: '분류 번호',
  PMS_END_DATE: '재심사 종료일',
  DOMESTIC_LWST_YN: '국내 최종 여부',
  ITEM_SEQ: '품목 일련번호',
  PAGE_GB_NM: '특허 목록 구분',
  PATENT_GB_CODE: '특허 구분',
  DOMESTIC_INVN_NM: '발명의 명칭',
  PATENTEE: '권리자',
  DOMESTIC_PATENT_NO: '국내 특허번호',
  DOMESTIC_PATENT_STATUS: '국내 특허 상태',
  DOMESTIC_END_DATE: '국내 특허 종료일',
  status_variants: '상태 변형',
  source_row_count: '원천 행 수',
}

export const PATENT_FIELD_ORDER = [
  'INGR_ENG_NAME', 'INGR_NAME', 'ITEM_ENG_NAME', 'ITEM_NAME', 'ENTP_NAME', 'SHAPE', 'CONT_QY',
  'CLASS_NO', 'PMS_END_DATE', 'DOMESTIC_LWST_YN', 'ITEM_SEQ', 'PAGE_GB_NM', 'PATENT_GB_CODE',
  'DOMESTIC_INVN_NM', 'PATENTEE', 'DOMESTIC_PATENT_NO', 'DOMESTIC_PATENT_STATUS',
  'DOMESTIC_END_DATE',
] as const

const PATENT_FIELD_INDEX = new Map(PATENT_FIELD_ORDER.map((key, index) => [key, index]))

const COUNT_LABELS: readonly { readonly key: keyof InspectionCounts; readonly label: string }[] = [
  { key: 'returned', label: '수신 건수' },
  { key: 'parsed', label: '읽은 건수' },
  { key: 'envelope', label: '답변 구성' },
  { key: 'rendered', label: '표시 건수' },
  { key: 'narrated', label: '본문 반영' },
] as const

export type InspectionStatus = {
  readonly kind: 'success' | 'empty' | 'failure' | 'quota'
  readonly label: string
}

const STATUS_LABELS: Readonly<Record<string, InspectionStatus>> = {
  '완료': { kind: 'success', label: '성공' },
  success: { kind: 'success', label: '성공' },
  '성공': { kind: 'success', label: '성공' },
  '성공+0건': { kind: 'empty', label: '0건' },
  empty: { kind: 'empty', label: '0건' },
  '실패': { kind: 'failure', label: '실패' },
  failure: { kind: 'failure', label: '실패' },
  error: { kind: 'failure', label: '실패' },
  '쿼터 소진': { kind: 'quota', label: '쿼터 소진' },
  quota_exhausted: { kind: 'quota', label: '쿼터 소진' },
}

export function displaySourceLabel(value: string): string {
  return SOURCE_LABELS[value] ?? value
}

export function displayBackendText(value: string): string {
  return value
    .replaceAll('자동 분류 실패 · 공식 원문 표시', '분류 결과 없이 공식 원문을 표시합니다')
    .replaceAll('조회 실패', '자료를 가져오지 못했습니다')
    .replaceAll('aux:patent:news', '특허 뉴스')
    .replaceAll('aux:web:news', '웹 뉴스')
}

export function displayParameterLabel(value: string): string {
  return PARAMETER_LABELS[value] ?? value
}

export function inspectionCountLabels(): typeof COUNT_LABELS {
  return COUNT_LABELS
}

export function displayInspectionStatus(status: string, returned: number | undefined): InspectionStatus {
  const mapped = STATUS_LABELS[status]
  if (mapped) return mapped
  if (returned === 0) return { kind: 'empty', label: '0건' }
  return { kind: 'failure', label: '실패' }
}

export function sortedJsonEntries(
  value: Readonly<Record<string, JsonValue>>,
  preferredOrder?: ReadonlyMap<string, number>,
): readonly (readonly [string, JsonValue])[] {
  return Object.entries(value).sort(([left], [right]) => {
    const leftIndex = preferredOrder?.get(left)
    const rightIndex = preferredOrder?.get(right)
    if (leftIndex !== undefined || rightIndex !== undefined) {
      return (leftIndex ?? Number.MAX_SAFE_INTEGER) - (rightIndex ?? Number.MAX_SAFE_INTEGER)
    }
    return left.localeCompare(right, 'ko')
  })
}

export function patentJsonEntries(
  value: Readonly<Record<string, JsonValue>>,
): readonly (readonly [string, JsonValue])[] {
  return sortedJsonEntries(value, PATENT_FIELD_INDEX)
}
