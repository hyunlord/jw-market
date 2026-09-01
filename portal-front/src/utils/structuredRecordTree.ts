import { canonicalEvidenceSummarySourceKey, recordSummaryForSource } from './evidenceSummaryFields.ts'

export interface StructuredRecordHeader {
  readonly ordinal: string
  readonly identifier: string
  readonly summary: string
}

const IDENTIFIER_KEYS = [
  'nct_id', 'nctid', 'study_id', 'patent_no', 'patent_number',
  'domestic_patent_no', 'kor_pat_no', 'application_number',
  'document_id', 'document_name', 'file_name', 'filename', 'chunk_id', 'evidence_id',
  'sickcd', 'disease_code', 'item_seq', 'id',
]

const SUMMARY_KEYS = [
  'brief_title', 'official_title', 'title', 'document_title', 'name',
  'item_name', 'product_name', 'product', 'overall_status', 'status',
  'phase', 'section_title', 'sheet_name', 'label', 'summary',
]

function scalarText(value: unknown): string | undefined {
  if (typeof value === 'string') return value.trim() || undefined
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  return undefined
}

function valuesByNormalizedKey(record: Record<string, unknown>): Map<string, string> {
  return new Map(Object.entries(record).flatMap(([key, value]) => {
    const text = scalarText(value)
    return text === undefined ? [] : [[key.toLowerCase().replace(/[^a-z0-9]/g, ''), text]]
  }))
}

function firstValue(values: Map<string, string>, keys: readonly string[]): string | undefined {
  for (const key of keys) {
    const value = values.get(key.replace(/[^a-z0-9]/g, ''))
    if (value !== undefined) return value
  }
  return undefined
}

function shorten(value: string, limit = 140): string {
  return value.length <= limit ? value : `${value.slice(0, limit - 1)}…`
}

export function recordHeaderFor(value: unknown, index: number, fallbackLabel: string, source?: string): StructuredRecordHeader {
  const ordinal = `#${index + 1}`
  if (value === null || typeof value !== 'object' || Array.isArray(value)) {
    return { ordinal, identifier: `${fallbackLabel} ${index + 1}`, summary: shorten(scalarText(value) ?? '값 없음') }
  }

  const record = value as Record<string, unknown>
  const values = valuesByNormalizedKey(record)
  const sourceSummary = recordSummaryForSource(canonicalEvidenceSummarySourceKey(source ?? ''), record)
  const identifier = sourceSummary.identifier ?? firstValue(values, IDENTIFIER_KEYS) ?? `${fallbackLabel} ${index + 1}`
  if (sourceSummary.summary !== undefined) {
    return { ordinal, identifier: shorten(identifier, 80), summary: shorten(sourceSummary.summary) }
  }
  const summaries: string[] = []
  for (const key of SUMMARY_KEYS) {
    const candidate = values.get(key.replace(/[^a-z0-9]/g, ''))
    if (candidate && candidate !== identifier && !summaries.includes(candidate)) summaries.push(candidate)
    if (summaries.length === 2) break
  }
  const summary = summaries.length > 0
    ? summaries.join(' · ')
    : `상세 필드 ${Object.keys(record).length}개`
  return { ordinal, identifier: shorten(identifier, 80), summary: shorten(summary) }
}

export function shouldShowRecordIndex(recordCount: number): boolean {
  return recordCount > 20
}
