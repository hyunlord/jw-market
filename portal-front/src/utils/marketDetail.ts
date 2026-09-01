import { apiFetch } from './apiFetch.ts'
import type { JsonValue } from './answerInspection.ts'

export const MARKET_DETAIL_SCHEMA = 'jw.detail-on-demand.v1' as const

export interface MarketDetailItem {
  itemKey: string
  kind: string
  source?: string
  identifier?: string
  summary?: JsonValue
}

export interface MarketDetailContract {
  schema: typeof MARKET_DETAIL_SCHEMA
  responseId: string
  items: readonly MarketDetailItem[]
  truncation?: {
    silent?: boolean
    detailFetchRequired?: boolean
    notice?: string
  }
}

export interface MarketDetailLookup {
  conversationId: string
  responseId: string
  itemKeys: ReadonlySet<string>
}

export interface MarketDetailInput {
  query?: string
  requestParameters?: JsonValue
  expansionGrade?: string
}

export interface MarketDetailOutput {
  receivedCount?: number
  directlyRelevantCount?: number
  summary?: JsonValue
  calledAt?: string
  elapsedMs?: number
}

export interface MarketDetailFieldMetadata {
  publicFieldCount?: number
  hiddenFieldCount: number
  hiddenFieldNotice?: string
  missingFields: Readonly<Record<string, string>>
  lengthHints: Readonly<Record<string, number>>
}

export interface MarketDetailResponse {
  schema: typeof MARKET_DETAIL_SCHEMA
  conversationId: string
  responseId: string
  itemKey: string
  kind: string
  detail: JsonValue
  input?: MarketDetailInput
  output?: MarketDetailOutput
  fieldMetadata: MarketDetailFieldMetadata
  partial: boolean
}

type Fetcher = (url: string, options?: RequestInit) => Promise<Response>

function record(value: unknown): Record<string, unknown> | undefined {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : undefined
}

function stringValue(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() ? value.trim() : undefined
}

function finiteNonNegative(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : undefined
}

function jsonValue(value: unknown): JsonValue | undefined {
  if (value === null || typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') return value
  if (Array.isArray(value)) {
    const parsed = value.map(jsonValue)
    return parsed.every(item => item !== undefined) ? parsed as JsonValue[] : undefined
  }
  const source = record(value)
  if (!source) return undefined
  const entries = Object.entries(source).map(([key, item]) => [key, jsonValue(item)] as const)
  if (entries.some(([, item]) => item === undefined)) return undefined
  return Object.fromEntries(entries) as JsonValue
}

function stringMap(value: unknown): Record<string, string> {
  const source = record(value)
  if (!source) return {}
  return Object.fromEntries(Object.entries(source).flatMap(([key, item]) => {
    const parsed = stringValue(item)
    return parsed ? [[key, parsed]] : []
  }))
}

function numberMap(value: unknown): Record<string, number> {
  const source = record(value)
  if (!source) return {}
  return Object.fromEntries(Object.entries(source).flatMap(([key, item]) => {
    const parsed = finiteNonNegative(item)
    return parsed === undefined ? [] : [[key, parsed]]
  }))
}

export function parseMarketDetailContract(value: unknown): MarketDetailContract | undefined {
  const source = record(value)
  if (source?.schema !== MARKET_DETAIL_SCHEMA) return undefined
  const responseId = stringValue(source.response_id)
  if (!responseId || !Array.isArray(source.items)) return undefined
  const items = source.items.flatMap(item => {
    const candidate = record(item)
    const itemKey = stringValue(candidate?.item_key)
    const kind = stringValue(candidate?.kind)
    if (!candidate || !itemKey || !kind) return []
    return [{
      itemKey,
      kind,
      source: stringValue(candidate.source),
      identifier: stringValue(candidate.identifier),
      summary: jsonValue(candidate.summary),
    }]
  })
  const truncation = record(source.truncation)
  return {
    schema: MARKET_DETAIL_SCHEMA,
    responseId,
    items,
    truncation: truncation ? {
      silent: typeof truncation.silent === 'boolean' ? truncation.silent : undefined,
      detailFetchRequired: typeof truncation.detail_fetch_required === 'boolean' ? truncation.detail_fetch_required : undefined,
      notice: stringValue(truncation.notice),
    } : undefined,
  }
}

export function createMarketDetailLookup(
  contract: MarketDetailContract | undefined,
  conversationId: string | undefined,
  responseId?: string,
): MarketDetailLookup | undefined {
  if (!contract || !conversationId) return undefined
  const resolvedResponseId = responseId || contract.responseId
  if (!resolvedResponseId || resolvedResponseId !== contract.responseId) return undefined
  return {
    conversationId,
    responseId: resolvedResponseId,
    itemKeys: new Set(contract.items.map(item => item.itemKey)),
  }
}

export function marketDetailContractFromChatLogData(value: unknown): MarketDetailContract | undefined {
  const data = record(value)
  const direct = parseMarketDetailContract(data?.detail_on_demand)
  if (direct) return direct
  const genosPersist = record(data?.genos_persist)
  const answer = record(genosPersist?.chat_agent_answer)
  const trace = record(answer?.trace)
  return parseMarketDetailContract(trace?.detail_on_demand)
}

function unwrapDetailPayload(value: unknown): unknown {
  const outer = record(value)
  const result = record(outer?.result)
  if (outer?.status === 'SUCCESS' && result?.code === 0) return result.data
  if (outer?.status === 'SUCCESS' && result?.schema === MARKET_DETAIL_SCHEMA) return result
  return value
}

function parseInput(value: unknown): MarketDetailInput | undefined {
  const source = record(value)
  if (!source) return undefined
  const requestParameters = jsonValue(source.request_parameters)
  return {
    query: stringValue(source.query),
    requestParameters,
    expansionGrade: stringValue(source.expansion_grade),
  }
}

function parseOutput(value: unknown): MarketDetailOutput | undefined {
  const source = record(value)
  if (!source) return undefined
  return {
    receivedCount: finiteNonNegative(source.received_count),
    directlyRelevantCount: finiteNonNegative(source.directly_relevant_count),
    summary: jsonValue(source.summary),
    calledAt: stringValue(source.called_at),
    elapsedMs: finiteNonNegative(source.elapsed_ms),
  }
}

function parseFieldMetadata(value: unknown): MarketDetailFieldMetadata {
  const source = record(value)
  return {
    publicFieldCount: finiteNonNegative(source?.public_field_count),
    hiddenFieldCount: finiteNonNegative(source?.hidden_field_count) ?? 0,
    hiddenFieldNotice: stringValue(source?.hidden_field_notice),
    missingFields: stringMap(source?.missing_fields),
    lengthHints: numberMap(source?.length_hints),
  }
}

export async function fetchMarketDetail(
  lookup: MarketDetailLookup,
  itemKey: string,
  fetcher: Fetcher = apiFetch,
): Promise<MarketDetailResponse> {
  if (!lookup.itemKeys.has(itemKey)) throw new Error('이 응답에는 요청한 상세 항목이 없습니다.')
  const url = `/api/v1/market/chat/detail/${encodeURIComponent(lookup.conversationId)}/${encodeURIComponent(lookup.responseId)}?item_key=${encodeURIComponent(itemKey)}`
  let response: Response
  try {
    response = await fetcher(url, { method: 'GET' })
  } catch {
    throw new Error('상세 원문 네트워크 연결에 실패했습니다. 연결 상태를 확인한 뒤 다시 시도해 주세요.')
  }
  if (response.status === 401 || response.status === 403) {
    throw new Error(`상세 원문 조회 권한이 없습니다(HTTP ${response.status}).`)
  }
  if (!response.ok) throw new Error(`상세 원문을 불러오지 못했습니다(HTTP ${response.status}).`)
  const raw = await response.text()
  if (!raw.trim()) throw new Error('상세 원문 응답이 비어 있습니다. 다시 시도해 주세요.')
  let decoded: unknown
  try {
    decoded = JSON.parse(raw)
  } catch {
    throw new Error('상세 원문 응답 형식이 올바르지 않습니다.')
  }
  const payload = record(unwrapDetailPayload(decoded))
  const detail = jsonValue(payload?.detail)
  const responseItemKey = stringValue(payload?.item_key)
  if (payload?.schema !== MARKET_DETAIL_SCHEMA || responseItemKey !== itemKey || detail === undefined) {
    throw new Error('상세 원문 응답 형식이 올바르지 않습니다.')
  }
  return {
    schema: MARKET_DETAIL_SCHEMA,
    conversationId: stringValue(payload.conversation_id) ?? lookup.conversationId,
    responseId: stringValue(payload.response_id) ?? lookup.responseId,
    itemKey: responseItemKey,
    kind: stringValue(payload.kind) ?? 'unknown',
    detail,
    input: parseInput(payload.input),
    output: parseOutput(payload.output),
    fieldMetadata: parseFieldMetadata(payload.field_metadata),
    partial: payload.partial === true,
  }
}
