// 시장분석(Market) 채팅 추론 과정 추출 — BACK_MARKET_CHAT.md §8 (2026-07-08 실측 반영).

import type { ReasoningStep } from './planSignal'

// 멀티쿼리 태그 → 친절한 한글 라벨 (에이전트 output.content의 `### [DB] ...` 헤더 기준)
const TAG_LABEL: Record<string, string> = {
  DB: '자사 시장 데이터 조회',
  HIRA: '질환·환자 통계 조회 (HIRA)',
  Nedrug: '국내 의약품 허가 정보 조회 (Nedrug)',
  FDA: 'FDA 규제 데이터 조회',
  ClinicalTrials: '글로벌 임상시험 조회',
  File: '첨부 파일 참조',
}

// output.content(마크다운)을 `### ` 블록으로 파싱 → 스텝 배열
function parseAgentContent(content: string): ReasoningStep[] {
  const steps: ReasoningStep[] = []
  content.split(/\n(?=###\s)/).forEach((block, i) => {
    const b = block.trim()
    if (!b.startsWith('###')) return
    const tag = (b.match(/^###\s*\[([^\]]+)\]/)?.[1] ?? '').trim()
    const header = (b.match(/^###\s*(.+)/)?.[1] ?? '').trim()
    let query = (b.match(/\*\*Query:\*\*\s*(.+)/)?.[1] ?? '').trim().replace(/^`+|`+$/g, '').trim()
    if (query.length > 200) query = query.slice(0, 200) + '…'   // 과도한 길이 방지
    const title = TAG_LABEL[tag] || header || query
    if (!title) return
    // 첫 줄=제목(친절 라벨), 나머지=본문(조회 목적). ChatMessageAI splitRationale이 tx-tit/tx-answer로 분리
    const rationale = query && query !== title ? `${title}\n\n${query}` : title
    steps.push({ nodeId: `market-step-${i}`, nodeLabel: 'Market MCP', rationale })
  })
  return steps
}

export function extractMarketReasoningSteps(flow: unknown): ReasoningStep[] | undefined {
  if (!Array.isArray(flow)) return undefined

  const directSteps: ReasoningStep[] = []
  const seenDirect = new Set<string>()
  flow.forEach((n, i) => {
    if (!n || typeof n !== 'object') return
    const node = n as { nodeId?: unknown; nodeLabel?: unknown; data?: { name?: unknown } }
    if (typeof node.nodeId !== 'string' || !node.nodeId.startsWith('direct-')) return
    const name = (typeof node.nodeLabel === 'string' && node.nodeLabel.trim())
      ? node.nodeLabel.trim()
      : (typeof node.data?.name === 'string' ? node.data.name.trim() : '')
    if (!name || seenDirect.has(name)) return
    seenDirect.add(name)
    directSteps.push({ nodeId: `market-log-step-${i}`, nodeLabel: 'Market', rationale: name })
  })
  if (directSteps.length > 0) return directSteps

  const mcpNode = flow.find(
    n => n && typeof n === 'object' && (n as { nodeId?: unknown }).nodeId === 'agentAgentflow_6'
  )
  const output = (mcpNode as { data?: { output?: { usedTools?: unknown; content?: unknown } } } | undefined)?.data?.output
  const usedTools = output?.usedTools
  if (!Array.isArray(usedTools) || usedTools.length === 0) return undefined   // ★ 실제 MCP 실행 있을 때만 (환각 방지)

  // 에이전트 정리 마크다운 파싱 (중복·빈결과·영문 없음)
  const content = typeof output?.content === 'string' ? output.content : ''
  const parsed = parseAgentContent(content)
  if (parsed.length > 0) return parsed

  // fallback: content 없을 때만 usedTools 도구명 (중복 제거)
  const seen = new Set<string>()
  const steps: ReasoningStep[] = []
  usedTools.forEach((t, i) => {
    if (!t || typeof t !== 'object') return
    const tool = (t as { tool?: unknown }).tool
    if (typeof tool !== 'string' || !tool || seen.has(tool)) return
    seen.add(tool)
    steps.push({ nodeId: `market-tool-${i}`, nodeLabel: 'Market MCP', rationale: tool })
  })
  return steps.length > 0 ? steps : undefined
}
