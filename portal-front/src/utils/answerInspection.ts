export type JsonValue = null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue }

export interface InspectionCounts {
  returned?: number
  parsed?: number
  envelope?: number
  rendered?: number
  narrated?: number
}

export interface InspectionDropReason {
  stage: string
  count: number
  reason: string
  record_ids?: string[]
}

export interface InspectionCall {
  sequence: number
  evidence_id?: string
  trace_sequence?: number
  tool?: string
  lane_id?: string
  state?: string
  source_label: string
  status: string
  elapsed_seconds: number
  request_parameters: Readonly<Record<string, JsonValue>> & { query: string; calls?: JsonValue[] }
  counts: InspectionCounts
  unused_count: number
  dropped_count: number
  output?: JsonValue
  drop_reasons: InspectionDropReason[]
}

export interface LaneExecution {
  source: string
  planned: boolean
  state: string
  reason_code: string | null
}

export type LaneExecutionMap = Readonly<Record<string, LaneExecution>>

export interface InspectionTraceCorrelation {
  key: string
  matched: number
  total: number
  rate: number
}

export interface AnswerInspectionDetail {
  schema: 'r12.5.inspect.v1'
  question: string
  expansion: JsonValue
  calls: InspectionCall[]
  trace_correlation?: InspectionTraceCorrelation
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function isJsonValue(value: unknown): value is JsonValue {
  if (value === null || ['boolean', 'number', 'string'].includes(typeof value)) return true
  if (Array.isArray(value)) return value.every(isJsonValue)
  return isRecord(value) && Object.values(value).every(isJsonValue)
}

function optionalNonNegativeNumber(value: unknown): value is number | undefined {
  return value === undefined || (typeof value === 'number' && Number.isFinite(value) && value >= 0)
}

function parseCounts(value: unknown): InspectionCounts | undefined {
  if (!isRecord(value)) return undefined
  const keys: (keyof InspectionCounts)[] = ['returned', 'parsed', 'envelope', 'rendered', 'narrated']
  if (!keys.every(key => optionalNonNegativeNumber(value[key]))) return undefined
  return Object.fromEntries(keys.filter(key => value[key] !== undefined).map(key => [key, value[key]]))
}

function parseDropReason(value: unknown): InspectionDropReason | undefined {
  if (!isRecord(value)
    || typeof value.stage !== 'string'
    || !optionalNonNegativeNumber(value.count)
    || value.count === undefined
    || typeof value.reason !== 'string'
    || (value.record_ids !== undefined
      && (!Array.isArray(value.record_ids) || !value.record_ids.every(recordId => typeof recordId === 'string')))) {
    return undefined
  }
  return {
    stage: value.stage,
    count: value.count,
    reason: value.reason,
    ...(value.record_ids === undefined ? {} : { record_ids: value.record_ids }),
  }
}

function parseCall(value: unknown): InspectionCall | undefined {
  if (!isRecord(value)
    || !Number.isInteger(value.sequence)
    || (value.evidence_id !== undefined
      && (typeof value.evidence_id !== 'string' || value.evidence_id.trim().length === 0))
    || (value.trace_sequence !== undefined && !Number.isInteger(value.trace_sequence))
    || (value.tool !== undefined && typeof value.tool !== 'string')
    || (value.lane_id !== undefined && typeof value.lane_id !== 'string')
    || (value.state !== undefined && typeof value.state !== 'string')
    || typeof value.source_label !== 'string'
    || typeof value.status !== 'string'
    || typeof value.elapsed_seconds !== 'number'
    || !Number.isFinite(value.elapsed_seconds)
    || value.elapsed_seconds < 0
    || !isRecord(value.request_parameters)
    || typeof value.request_parameters.query !== 'string'
    || !Object.values(value.request_parameters).every(isJsonValue)
    || (value.request_parameters.calls !== undefined
      && (!Array.isArray(value.request_parameters.calls) || !value.request_parameters.calls.every(isJsonValue)))
    || !optionalNonNegativeNumber(value.unused_count)
    || value.unused_count === undefined
    || !optionalNonNegativeNumber(value.dropped_count)
    || value.dropped_count === undefined
    || (value.output !== undefined && !isJsonValue(value.output))
    || !Array.isArray(value.drop_reasons)) return undefined

  const counts = parseCounts(value.counts)
  const dropReasons = value.drop_reasons.map(parseDropReason)
  if (!counts || dropReasons.some(reason => reason === undefined)) return undefined

  return {
    sequence: value.sequence as number,
    ...(value.evidence_id === undefined ? {} : { evidence_id: value.evidence_id }),
    ...(value.trace_sequence === undefined ? {} : { trace_sequence: value.trace_sequence as number }),
    ...(value.tool === undefined ? {} : { tool: value.tool }),
    ...(value.lane_id === undefined ? {} : { lane_id: value.lane_id }),
    ...(value.state === undefined ? {} : { state: value.state }),
    source_label: value.source_label,
    status: value.status,
    elapsed_seconds: value.elapsed_seconds,
    request_parameters: value.request_parameters as Readonly<Record<string, JsonValue>> & { query: string; calls?: JsonValue[] },
    counts,
    unused_count: value.unused_count,
    dropped_count: value.dropped_count,
    ...(value.output === undefined ? {} : { output: value.output }),
    drop_reasons: dropReasons as InspectionDropReason[],
  }
}

function parseLaneExecution(value: unknown): LaneExecution | undefined {
  if (!isRecord(value)
    || typeof value.source !== 'string'
    || typeof value.planned !== 'boolean'
    || typeof value.state !== 'string'
    || (value.reason_code !== null && typeof value.reason_code !== 'string')) return undefined
  return {
    source: value.source,
    planned: value.planned,
    state: value.state,
    reason_code: value.reason_code,
  }
}

export function parseLaneExecutions(value: unknown): LaneExecutionMap {
  if (!isRecord(value)) return {}
  const entries: [string, LaneExecution][] = []
  for (const [key, item] of Object.entries(value)) {
    const execution = parseLaneExecution(item)
    if (execution) entries.push([key, execution])
  }
  return Object.fromEntries(entries)
}

function parseTraceCorrelation(value: unknown): InspectionTraceCorrelation | undefined {
  if (!isRecord(value)
    || typeof value.key !== 'string'
    || !Number.isInteger(value.matched)
    || !Number.isInteger(value.total)
    || typeof value.rate !== 'number'
    || !Number.isFinite(value.rate)
    || (value.matched as number) < 0
    || (value.total as number) < 0
    || value.rate < 0) return undefined
  return {
    key: value.key,
    matched: value.matched as number,
    total: value.total as number,
    rate: value.rate,
  }
}

export function parseInspectionDetail(value: unknown): AnswerInspectionDetail | undefined {
  if (!isRecord(value)
    || value.schema !== 'r12.5.inspect.v1'
    || typeof value.question !== 'string'
    || !isJsonValue(value.expansion)
    || !Array.isArray(value.calls)) return undefined

  const calls = value.calls.map(parseCall)
  if (calls.some(call => call === undefined)) return undefined
  const traceCorrelation = value.trace_correlation === undefined
    ? undefined
    : parseTraceCorrelation(value.trace_correlation)
  if (value.trace_correlation !== undefined && !traceCorrelation) return undefined
  return {
    schema: value.schema,
    question: value.question,
    expansion: value.expansion,
    calls: calls as InspectionCall[],
    ...(traceCorrelation === undefined ? {} : { trace_correlation: traceCorrelation }),
  }
}

export function inspectionDetailFromChatLogData(value: unknown): AnswerInspectionDetail | undefined {
  if (!isRecord(value)) return undefined
  const persisted = value.genos_persist
  if (!isRecord(persisted)) return undefined
  const answer = persisted.chat_agent_answer
  if (!isRecord(answer) || !isRecord(answer.trace)) return undefined
  return parseInspectionDetail(answer.trace.inspection_detail)
}

export function laneExecutionsFromChatLogData(value: unknown): LaneExecutionMap {
  if (!isRecord(value)) return {}
  const persisted = value.genos_persist
  if (!isRecord(persisted)) return {}
  const answer = persisted.chat_agent_answer
  if (!isRecord(answer) || !isRecord(answer.trace)) return {}
  return parseLaneExecutions(answer.trace.lane_execution)
}
