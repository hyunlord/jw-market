export type EvidenceSummarySourceKey =
  | 'patent'
  | 'clinicaltrials'
  | 'hira'
  | 'mart'
  | 'openfda'
  | 'nedrug'
  | 'news'
  | 'document'
  | 'web'

interface EvidenceSummaryField {
  readonly label: string
  readonly aliases: readonly string[]
}

interface EvidenceSummaryDefinition {
  readonly sourceNames: readonly string[]
  readonly evidencePrefixes: readonly string[]
  readonly recordKeyPrefixes?: readonly string[]
  readonly fields: readonly EvidenceSummaryField[]
}

export interface EvidenceSummaryRow {
  readonly key: string
  readonly label: string
  readonly value: unknown
  readonly missing: boolean
}

const MISSING_SOURCE_VALUE = '원천 미제공'

const SUMMARY_DEFINITIONS: Readonly<Record<EvidenceSummarySourceKey, EvidenceSummaryDefinition>> = {
  patent: {
    sourceNames: ['patent', '특허', '식품의약품안전처 의약품 특허목록'],
    evidencePrefixes: ['patent:'],
    recordKeyPrefixes: ['DOMESTIC_PATENT'],
    fields: [
      { label: '특허번호', aliases: ['DOMESTIC_PATENT_NO', 'patent_number', 'patent_no', 'registration_number', 'application_number'] },
      { label: '명칭', aliases: ['DOMESTIC_INVN_NM', 'invention_title', 'title', 'name'] },
      { label: '출원일', aliases: ['application_date', 'filing_date', 'APPLICATION_DATE'] },
      { label: '등록일', aliases: ['registration_date', 'grant_date', 'REGISTRATION_DATE'] },
      { label: '권리자', aliases: ['PATENTEE', 'assignee', 'applicant', 'holder'] },
      { label: '상태', aliases: ['DOMESTIC_PATENT_STATUS', 'patent_status', 'status'] },
    ],
  },
  clinicaltrials: {
    sourceNames: ['clinicaltrials', 'clinicaltrials.gov', 'ct'],
    evidencePrefixes: ['ct:', 'clinicaltrials:'],
    fields: [
      { label: 'NCT 번호', aliases: ['nct_id', 'nctid', 'study_id', 'NCT 번호', 'NCT ID'] },
      { label: '시험명', aliases: ['brief_title', 'official_title', 'title', '시험명'] },
      { label: '상태', aliases: ['overall_status', 'status', '상태'] },
      { label: '단계', aliases: ['phases', 'phase', '단계'] },
      { label: '스폰서', aliases: ['sponsor', 'lead_sponsor', 'sponsor_name', '스폰서'] },
      { label: '시작일', aliases: ['start_date', 'study_start_date', '시작일'] },
      { label: '완료일', aliases: ['completion_date', 'primary_completion_date', 'study_completion_date', '완료일'] },
    ],
  },
  hira: {
    sourceNames: ['hira', '건강보험심사평가원'],
    evidencePrefixes: ['hira:'],
    fields: [
      { label: '상병코드', aliases: ['sickCd', 'sick_cd', 'disease_code'] },
      { label: '상병명', aliases: ['sickNm', 'sick_nm', 'disease_name'] },
      { label: '연도', aliases: ['requested_year', 'year'] },
      { label: '환자수', aliases: ['ptntCnt', 'patient_count'] },
      { label: '요양기관 구분', aliases: ['inpatOpat', 'institution_type', 'care_type'] },
    ],
  },
  mart: {
    sourceNames: ['mart', '내부 데이터마트', 'ubist', 'iqvia', 'csd'],
    evidencePrefixes: ['mart:'],
    fields: [
      { label: '브랜드', aliases: ['brand', '브랜드', 'brand_name'] },
      { label: '제조사', aliases: ['manufacturer', '제조사', 'company', 'company_name'] },
      { label: '채널', aliases: ['channel', '채널', 'source'] },
      { label: '기간', aliases: ['period', '기간', 'year_month', 'month', 'date'] },
      { label: '지표명', aliases: ['metric_name', '지표명', 'metric', 'measure'] },
      { label: '값', aliases: ['value', '값', 'metric_value', '매출'] },
    ],
  },
  openfda: {
    sourceNames: ['fda', 'openfda'],
    evidencePrefixes: ['fda:', 'openfda:'],
    fields: [
      { label: '품목명', aliases: ['product_name', 'openfda.brand_name', 'brand_name', 'item_name'] },
      { label: '성분', aliases: ['active_ingredient', 'substance_name', 'ingredient'] },
      { label: '허가일', aliases: ['approval_date', 'effective_time', 'application_date'] },
      { label: '구분', aliases: ['product_type', 'application_type', 'category'] },
    ],
  },
  nedrug: {
    sourceNames: ['nedrug', '식품의약품안전처', '의약품 허가'],
    evidencePrefixes: ['nedrug:'],
    fields: [
      { label: '품목명', aliases: ['ITEM_NAME', 'item_name', 'product_name'] },
      { label: '업체', aliases: ['ENTP_NAME', 'entp_name', 'company_name'] },
      { label: '성분', aliases: ['INGR_NAME', 'ingredient', 'active_ingredient'] },
      { label: '허가일', aliases: ['ITEM_PERMIT_DATE', 'item_permit_date', 'approval_date'] },
      { label: '효능', aliases: ['EE_DOC_DATA', 'efficacy', 'indication'] },
    ],
  },
  news: {
    sourceNames: ['news', '뉴스', '웹 뉴스', '특허 뉴스', 'aux:web:news', 'aux:patent:news'],
    evidencePrefixes: ['news:', 'aux:web:news:', 'aux:patent:news:'],
    fields: [
      { label: '제목', aliases: ['title', 'headline'] },
      { label: '매체', aliases: ['publisher', 'media', 'source_name'] },
      { label: '발행일', aliases: ['published_at', 'published_date', 'publication_date', 'date'] },
      { label: '링크', aliases: ['url', 'link'] },
    ],
  },
  document: {
    sourceNames: ['document', '업로드 문서', '파일 업로드'],
    evidencePrefixes: ['document:', 'document_rag:', 'document_sql:'],
    fields: [
      { label: '문서명', aliases: ['document_name', 'file_name', 'filename', 'title'] },
      { label: '시트/페이지', aliases: ['sheet_name', 'page_number', 'page', 'section_title'] },
      { label: '기준일', aliases: ['reference_date', 'as_of_date', 'date'] },
      { label: '발췌', aliases: ['content_excerpt', 'excerpt', 'content', 'chunk_text'] },
    ],
  },
  web: {
    sourceNames: ['web', '웹', '웹 검색'],
    evidencePrefixes: ['web:'],
    fields: [
      { label: '제목', aliases: ['title', 'name'] },
      { label: '도메인', aliases: ['domain', 'site_name', 'source'] },
      { label: '게시일', aliases: ['published_at', 'published_date', 'date'] },
      { label: '링크', aliases: ['url', 'link'] },
    ],
  },
}

function normalized(value: string): string {
  return value.trim().toLowerCase()
}

function directValue(record: Readonly<Record<string, unknown>>, alias: string): unknown {
  if (Object.prototype.hasOwnProperty.call(record, alias)) return record[alias]
  const segments = alias.split('.')
  let current: unknown = record
  for (const segment of segments) {
    if (current === null || typeof current !== 'object' || Array.isArray(current)) return undefined
    current = (current as Readonly<Record<string, unknown>>)[segment]
  }
  return current
}

function present(value: unknown): boolean {
  return value !== undefined && value !== null && !(typeof value === 'string' && value.trim() === '')
}

export function evidenceSummarySourceKey(
  sourceName: string,
  evidenceId: string,
  record: Readonly<Record<string, unknown>>,
): EvidenceSummarySourceKey | undefined {
  const source = normalized(sourceName)
  const id = normalized(evidenceId)
  for (const [key, definition] of Object.entries(SUMMARY_DEFINITIONS) as [EvidenceSummarySourceKey, EvidenceSummaryDefinition][]) {
    if (definition.evidencePrefixes.some(prefix => id.startsWith(prefix))) return key
    if (definition.sourceNames.some(name => source === normalized(name))) return key
    if (definition.recordKeyPrefixes?.some(prefix => Object.keys(record).some(recordKey => recordKey.toUpperCase().startsWith(prefix))) === true) return key
  }
  return undefined
}

export function evidenceSummaryRows(
  sourceKey: EvidenceSummarySourceKey | undefined,
  record: Readonly<Record<string, unknown>>,
): EvidenceSummaryRow[] {
  if (sourceKey === undefined) return []
  return SUMMARY_DEFINITIONS[sourceKey].fields.map(field => {
    const alias = field.aliases.find(candidate => present(directValue(record, candidate)))
    return alias === undefined
      ? { key: field.aliases[0]!, label: field.label, value: MISSING_SOURCE_VALUE, missing: true }
      : { key: alias, label: field.label, value: directValue(record, alias), missing: false }
  })
}

function summaryText(value: unknown): string | undefined {
  if (typeof value === 'string') return value.trim() || undefined
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  if (Array.isArray(value)) {
    const values = value.map(summaryText).filter((item): item is string => item !== undefined)
    return values.length > 0 ? values.join(' · ') : undefined
  }
  return undefined
}

export function recordSummaryForSource(
  sourceKey: EvidenceSummarySourceKey | undefined,
  record: Readonly<Record<string, unknown>>,
): { readonly identifier?: string; readonly summary?: string } {
  const rows = evidenceSummaryRows(sourceKey, record)
  const identifier = rows[0]?.missing === false ? summaryText(rows[0].value) : undefined
  const summary = rows.slice(1).filter(row => !row.missing).map(row => summaryText(row.value)).filter((value): value is string => value !== undefined).slice(0, 2).join(' · ') || undefined
  return { identifier, summary }
}

export function evidenceSummaryValue(value: unknown): string {
  const text = summaryText(value)
  if (text !== undefined) return text
  if (value !== null && typeof value === 'object') return JSON.stringify(value)
  return MISSING_SOURCE_VALUE
}

export function canonicalEvidenceSummarySourceKey(source: string): EvidenceSummarySourceKey | undefined {
  const normalizedSource = normalized(source)
  return (Object.entries(SUMMARY_DEFINITIONS) as [EvidenceSummarySourceKey, EvidenceSummaryDefinition][])
    .find(([key, definition]) => key === normalizedSource || definition.sourceNames.some(name => normalized(name) === normalizedSource))?.[0]
}
