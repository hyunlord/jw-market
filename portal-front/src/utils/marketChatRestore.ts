import { answerSectionsToPlainMarkdown, parsePersistedAnswerSections, type AnswerSectionState } from './answerSections.ts'
import { parseMarketCharts, type MarketChart } from './marketChartContract.ts'
import { extractMarketReasoningSteps } from './marketReasoning.ts'
import { parseMarketTables, type MarketTable } from './marketTables.ts'
import type { ReasoningStep } from './planSignal.ts'

export interface MarketChatRestoreData {
  readonly text: string
  readonly agentFlowExecutedData?: unknown
  readonly answer_sections?: unknown
  readonly evidence_catalog?: unknown
  readonly tables?: unknown
  readonly structured_tables?: unknown
  readonly charts?: unknown
  readonly restore_partial?: unknown
}

export interface RestoredMarketAnswerSurface {
  readonly planContent: string
  readonly sections?: AnswerSectionState[]
  readonly reasoningSteps?: ReasoningStep[]
  readonly reasoningInitiallyExpanded: boolean
  readonly tables?: MarketTable[]
  readonly charts?: MarketChart[]
  readonly chartError?: string
  readonly streamNotice?: string
}

export function restoreMarketAnswerSurface(data: MarketChatRestoreData): RestoredMarketAnswerSurface {
  const sections = parsePersistedAnswerSections(data.answer_sections, data.evidence_catalog)
  const reasoningSteps = extractMarketReasoningSteps(data.agentFlowExecutedData)
  const tables = parseMarketTables(data.tables ?? data.structured_tables)
  const parsedCharts = parseMarketCharts(data.charts)
  return {
    planContent: sections ? answerSectionsToPlainMarkdown(sections) : data.text,
    sections,
    reasoningSteps,
    reasoningInitiallyExpanded: Boolean(reasoningSteps?.length),
    tables,
    charts: parsedCharts?.charts,
    chartError: parsedCharts && parsedCharts.rejectedCount > 0
      ? `차트 ${parsedCharts.rejectedCount}건을 복원하지 못했습니다.`
      : undefined,
    streamNotice: data.restore_partial && typeof data.restore_partial === 'object'
      ? '이전 응답의 일부 표시 요소가 저장 용량 제한으로 생략됐습니다.'
      : undefined,
  }
}
