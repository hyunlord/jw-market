import type { ReasoningStep } from './planSignal'
import {
  parseInspectionDetail,
  parseLaneExecutions,
  type AnswerInspectionDetail,
  type LaneExecutionMap,
} from './answerInspection.ts'
import {
  parseMarketCharts,
  type MarketChart,
} from './marketChartContract.ts'
import { parseMarketTables, type MarketTable } from './marketTables.ts'
import {
  parseSelectionPolicy,
  parseTraceToolResults,
  parseUnnarratedRecords,
  type SelectionPolicy,
  type TraceToolResult,
  type UnnarratedRecord,
} from './traceToolResults.ts'
import {
  applyAnswerSectionDelta,
  answerSectionsHaveContent,
  parseAnswerSectionMetadata,
  type AnswerSectionState,
} from './answerSections.ts'
import { parseMarketDetailContract, type MarketDetailContract } from './marketDetail.ts'

/** SSE event 이름 — 실측 확정본 */
export const MARKET_STREAM_EVENTS = {
  delta: 'delta',
  markdownBlock: 'markdown_block',
  step: 'step',
  trace: 'trace',
  conversation: 'conversation',
  tables: 'tables',
  charts: 'charts',
  answerSections: 'answer_sections',
  answerSectionDelta: 'answer_section_delta',
  error: 'error',
  done: 'done',
} as const

export const MARKET_STREAM_CLIENT_TIMEOUT_MS = 510_000

interface SseFrame {
  event: string
  data: string
}

function parseSseFrame(raw: string): SseFrame | null {
  let event = 'message'
  const dataLines: string[] = []
  for (const line of raw.split('\n')) {
    if (!line || line.startsWith(':')) continue
    const colon = line.indexOf(':')
    const field = colon === -1 ? line : line.slice(0, colon)
    let value = colon === -1 ? '' : line.slice(colon + 1)
    if (value.startsWith(' ')) value = value.slice(1)
    if (field === 'event') event = value
    else if (field === 'data') dataLines.push(value)
  }
  if (dataLines.length === 0) return null
  return { event, data: dataLines.join('\n') }
}

interface SseReadOptions {
  readonly completedSectionIdleMs: number
  readonly canCompleteFromIdle: () => boolean
  readonly onCompletedSectionIdle: () => void
}

async function readWithIdleTimeout(
  reader: ReadableStreamDefaultReader<Uint8Array>,
  idleMs: number | undefined,
): Promise<{ kind: 'read'; value: ReadableStreamReadResult<Uint8Array> } | { kind: 'idle' }> {
  if (idleMs === undefined) return { kind: 'read', value: await reader.read() }
  let timer: ReturnType<typeof setTimeout> | undefined
  try {
    return await Promise.race([
      reader.read().then(value => ({ kind: 'read' as const, value })),
      new Promise<{ kind: 'idle' }>(resolve => {
        timer = setTimeout(() => resolve({ kind: 'idle' }), idleMs)
      }),
    ])
  } finally {
    if (timer !== undefined) clearTimeout(timer)
  }
}

async function* parseSseStream(body: ReadableStream<Uint8Array>, options: SseReadOptions): AsyncGenerator<SseFrame> {
  const reader = body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let streamClosed = false
  try {
    for (;;) {
      const result = await readWithIdleTimeout(
        reader,
        options.canCompleteFromIdle() ? options.completedSectionIdleMs : undefined,
      )
      if (result.kind === 'idle') {
        options.onCompletedSectionIdle()
        break
      }
      const { done, value } = result.value
      if (done) {
        streamClosed = true
        break
      }
      buffer += decoder.decode(value, { stream: true }).replace(/\r\n/g, '\n')
      let idx: number
      while ((idx = buffer.indexOf('\n\n')) !== -1) {
        const frame = parseSseFrame(buffer.slice(0, idx))
        buffer = buffer.slice(idx + 2)
        if (frame) yield frame
      }
    }
    const tail = parseSseFrame(buffer)
    if (tail) yield tail
  } finally {
    if (!streamClosed) await reader.cancel().catch(() => undefined)
    reader.releaseLock()
  }
}

function safeJsonParse<T>(s: string): T | null {
  try { return JSON.parse(s) as T } catch { return null }
}

interface StepData {
  name?: string
  detail?: string
  raw_name?: string
}

export interface MarketStreamResult {
  text: string
  sections?: AnswerSectionState[]
  steps: ReasoningStep[]
  traceId?: string
  conversationId?: string
  inspectionDetail?: AnswerInspectionDetail
  laneExecutions: LaneExecutionMap
  toolResults: TraceToolResult[]
  unnarratedRecords: UnnarratedRecord[]
  selectionPolicy?: SelectionPolicy
  detailContract?: MarketDetailContract
  tables: MarketTable[]
  tableError?: string
  charts: MarketChart[]
  chartError?: string
  errorCode?: string
  hadBodyBeforeTerminalNotice?: boolean
  done: boolean
  completionFallback?: 'section_idle'
}

export type { MarketChart, MarketChartSeries, MarketChartType } from './marketChartContract.ts'

export interface MarketStreamCallbacks {
  onAnswer: (fullText: string) => void
  onSections?: (sections: AnswerSectionState[]) => void
  onSteps?: (steps: ReasoningStep[]) => void
  onTables?: (tables: MarketTable[]) => void
  onTableError?: (error: string) => void
  onCharts?: (charts: MarketChart[]) => void
  onChartError?: (error: string) => void
}

export interface MarketStreamOptions {
  readonly completedSectionIdleMs?: number
}

export const MARKET_STREAM_COMPLETED_SECTION_IDLE_MS = 15_000

function mergeMarketTables(current: readonly MarketTable[], received: readonly MarketTable[]): MarketTable[] {
  const merged = [...current]
  const indexes = new Map(merged.map((table, index) => [table.table_id, index]))
  for (const table of received) {
    const index = indexes.get(table.table_id)
    if (index === undefined) {
      indexes.set(table.table_id, merged.length)
      merged.push(table)
    } else {
      merged[index] = table
    }
  }
  return merged
}

function streamErrorCode(data: string): string {
  const parsed = safeJsonParse<Record<string, unknown>>(data)
  if (typeof parsed?.code === 'string' && parsed.code.trim()) return parsed.code.trim()
  for (const code of ['BFF_SSE_STREAM_TIMEOUT', 'BFF_SSE_FRAME_LIMIT']) {
    if (data.includes(code)) return code
  }
  return 'STREAM_ERROR'
}

export function marketStreamTerminationNotice(result: Pick<MarketStreamResult, 'text' | 'done' | 'errorCode' | 'hadBodyBeforeTerminalNotice'>): string | undefined {
  const hasBody = result.hadBodyBeforeTerminalNotice ?? result.text.trim().length > 0
  const suffix = hasBody ? '지금까지 받은 내용만 표시합니다.' : '결과를 받지 못했습니다.'
  if (result.errorCode === 'BFF_SSE_STREAM_TIMEOUT') return `응답이 시간 제한(300초)을 넘어 중단됐습니다. ${suffix}`
  if (result.errorCode === 'BFF_SSE_FRAME_LIMIT') return `응답 데이터가 표시 한도를 넘어 중단됐습니다. ${suffix}`
  if (result.errorCode) return `응답 처리 중 문제가 발생해 중단됐습니다. ${suffix}`
  if (result.done && !hasBody) return '응답은 완료됐지만 결과를 받지 못했습니다.'
  return undefined
}

export function marketStreamConnectionNotice(hasBody: boolean): string {
  return hasBody
    ? '응답 연결이 중단됐습니다. 지금까지 받은 내용만 표시합니다.'
    : '응답 연결이 중단되어 결과를 받지 못했습니다.'
}

export async function consumeMarketStream(
  res: Response,
  cb: MarketStreamCallbacks,
  options: MarketStreamOptions = {},
): Promise<MarketStreamResult> {
  const textChunks: string[] = []
  const steps: ReasoningStep[] = []
  let tables: MarketTable[] = []
  let charts: MarketChart[] = []
  let sections: AnswerSectionState[] | undefined
  const seenStep = new Set<string>()
  let traceId: string | undefined = res.headers.get('x-genos-trace-id') || undefined
  let conversationId: string | undefined
  let inspectionDetail: AnswerInspectionDetail | undefined
  let laneExecutions: LaneExecutionMap = {}
  let toolResults: TraceToolResult[] = []
  let unnarratedRecords: UnnarratedRecord[] = []
  let selectionPolicy: SelectionPolicy | undefined
  let detailContract: MarketDetailContract | undefined
  let tableError: string | undefined
  let chartError: string | undefined
  let errorCode: string | undefined
  let hadBodyBeforeTerminalNotice: boolean | undefined
  let done = false
  let completionFallback: MarketStreamResult['completionFallback']

  // delta는 토큰 조각이므로 원문 그대로 이어 붙인다. 공백을 삽입하면 청크 경계의 URL이 훼손된다.
  const fullText = () => textChunks.join('')

  streamLoop: for await (const frame of parseSseStream(res.body!, {
    completedSectionIdleMs: options.completedSectionIdleMs ?? MARKET_STREAM_COMPLETED_SECTION_IDLE_MS,
    canCompleteFromIdle: () => Boolean(sections?.length && sections.every(section => section.status === 'complete' || section.status === 'failed')),
    onCompletedSectionIdle: () => {
      done = true
      completionFallback = 'section_idle'
      console.warn('[market-stream] completed sections were idle without a done event; finalizing the answer')
    },
  })) {
    switch (frame.event) {
      case MARKET_STREAM_EVENTS.delta: {
        if (frame.data) { textChunks.push(frame.data); cb.onAnswer(fullText()) }
        break
      }
      case MARKET_STREAM_EVENTS.markdownBlock: {
        const b = safeJsonParse<{ markdown?: unknown }>(frame.data)
        const md = typeof b?.markdown === 'string' ? b.markdown : ''
        if (md) {
          if (/응답이 시간 안에 끝나지 않아|답변이 한 번에 전달할 수 있는 크기를 넘어/.test(md)) {
            hadBodyBeforeTerminalNotice = fullText().trim().length > 0 || answerSectionsHaveContent(sections)
          }
          textChunks.push(md)
          cb.onAnswer(fullText())
        }
        break
      }
      case MARKET_STREAM_EVENTS.step: {
        const s = safeJsonParse<StepData>(frame.data)
        const name = typeof s?.name === 'string' ? s.name : ''
        if (!name) break
        const key = typeof s?.raw_name === 'string' && s.raw_name ? s.raw_name : name
        if (seenStep.has(key)) break
        seenStep.add(key)
        const detail = typeof s?.detail === 'string' ? s.detail.trim() : ''
        steps.push({
          nodeId: `market-step-${steps.length}`,
          nodeLabel: 'Market',
          rationale: detail && detail !== name ? `${name}\n\n${detail}` : name,
        })
        cb.onSteps?.([...steps])
        break
      }
      case MARKET_STREAM_EVENTS.trace: {
        const t = safeJsonParse<Record<string, unknown>>(frame.data)
        if (typeof t?.trace_id === 'string') traceId = t.trace_id
        if (typeof t?.conversation_id === 'string') conversationId = t.conversation_id
        inspectionDetail = parseInspectionDetail(t?.inspection_detail) ?? inspectionDetail
        const receivedLaneExecutions = parseLaneExecutions(t?.lane_execution)
        if (Object.keys(receivedLaneExecutions).length > 0) laneExecutions = receivedLaneExecutions
        const receivedToolResults = parseTraceToolResults(t?.tool_results)
        if (receivedToolResults.length > 0) toolResults = receivedToolResults
        const receivedUnnarratedRecords = parseUnnarratedRecords(t?.lossless_spine)
        if (receivedUnnarratedRecords.length > 0) unnarratedRecords = receivedUnnarratedRecords
        selectionPolicy = parseSelectionPolicy(t) ?? selectionPolicy
        detailContract = parseMarketDetailContract(t?.detail_on_demand) ?? detailContract
        break
      }
      case MARKET_STREAM_EVENTS.conversation: {
        if (frame.data) conversationId = frame.data
        break
      }
      case MARKET_STREAM_EVENTS.tables: {
        const received = parseMarketTables(safeJsonParse<unknown>(frame.data))
        if (!received) {
          tableError = '표 데이터를 표시할 수 없습니다.'
          cb.onTableError?.(tableError)
        } else if (received.length > 0) {
          tables = mergeMarketTables(tables, received)
          cb.onTables?.([...tables])
        }
        break
      }
      case MARKET_STREAM_EVENTS.charts: {
        const received = parseMarketCharts(safeJsonParse<unknown>(frame.data))
        if (!received) {
          chartError = '차트를 표시하지 못했습니다(데이터 형식 불일치).'
          cb.onChartError?.(chartError)
        } else {
          if (received.rejectedCount > 0) {
            chartError = received.charts.length > 0
              ? `차트 ${received.rejectedCount}개를 표시하지 못했습니다(데이터 형식 불일치).`
              : '차트를 표시하지 못했습니다(데이터 형식 불일치).'
            cb.onChartError?.(chartError)
          }
          if (received.charts.length > 0) {
            charts = received.charts
            cb.onCharts?.([...charts])
          }
        }
        break
      }
      case MARKET_STREAM_EVENTS.answerSections: {
        const received = parseAnswerSectionMetadata(safeJsonParse<unknown>(frame.data))
        if (received) {
          sections = received
          cb.onSections?.([...sections])
        }
        break
      }
      case MARKET_STREAM_EVENTS.answerSectionDelta: {
        if (!sections) break
        const received = applyAnswerSectionDelta(sections, safeJsonParse<unknown>(frame.data))
        if (received) {
          sections = received
          cb.onSections?.([...sections])
        }
        break
      }
      case MARKET_STREAM_EVENTS.error: {
        errorCode = streamErrorCode(frame.data)
        break
      }
      case MARKET_STREAM_EVENTS.done: {
        done = true
        break streamLoop
      }
    }
  }

  return {
    text: fullText(), sections, steps, traceId, conversationId, inspectionDetail, laneExecutions, toolResults, unnarratedRecords, selectionPolicy, detailContract,
    tables, tableError, charts, chartError, errorCode,
    hadBodyBeforeTerminalNotice: hadBodyBeforeTerminalNotice ?? (answerSectionsHaveContent(sections) || undefined),
    done, completionFallback,
  }
}

export type { AnswerSectionState } from './answerSections.ts'
