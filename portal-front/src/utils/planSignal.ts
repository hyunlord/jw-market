export function isPlanResponse(resultOrText: unknown): boolean {
  if (!resultOrText || typeof resultOrText !== 'object') return false
  const r = resultOrText as { data?: { agentFlowExecutedData?: unknown } }
  const flow = r.data?.agentFlowExecutedData
  if (!Array.isArray(flow)) return false
  return flow.some(n => {
    if (!n || typeof n !== 'object') return false
    const node = n as { nodeId?: unknown; status?: unknown }
    return typeof node.nodeId === 'string'
      && node.nodeId.startsWith('humanInputAgentflow')
      && node.status === 'STOPPED'
  })
}

export function extractStoppedNodeId(flowData: unknown): string | undefined {
  if (!Array.isArray(flowData)) return undefined
  for (const n of flowData) {
    if (!n || typeof n !== 'object') continue
    const node = n as { nodeId?: unknown; status?: unknown }
    if (
      typeof node.nodeId === 'string'
      && node.nodeId.startsWith('humanInputAgentflow')
      && node.status === 'STOPPED'
    ) {
      return node.nodeId
    }
  }
  return undefined
}

export interface PlanStep {
  step: string
  mcp_server_name: string
  action_description: string
}

function findJsonBlock(text: string): { start: number; end: number } | null {
  const start = text.indexOf('{')
  if (start === -1) return null
  let depth = 0
  let inString = false
  let escape = false
  for (let i = start; i < text.length; i++) {
    const ch = text[i]
    if (escape) { escape = false; continue }
    if (ch === '\\') { escape = true; continue }
    if (ch === '"') { inString = !inString; continue }
    if (inString) continue
    if (ch === '{') depth++
    else if (ch === '}') {
      depth--
      if (depth === 0) return { start, end: i }
    }
  }
  return null
}

export function planContentToMarkdown(text: string): string {
  if (!text) return text
  const block = findJsonBlock(text)
  if (!block) return text
  const jsonStr = text.slice(block.start, block.end + 1)
  const trailingText = text.slice(block.end + 1).trim()
  try {
    const parsed = JSON.parse(jsonStr) as { planning_result?: unknown; plan?: unknown }
    let steps: unknown = parsed.planning_result ?? parsed.plan
    if (typeof steps === 'string') {
      try { steps = JSON.parse(steps) } catch { return text }
    }
    if (!Array.isArray(steps) || steps.length === 0) return text

    const lines = steps
      .map(s => {
        if (!s || typeof s !== 'object') return null
        const o = s as { step?: unknown; mcp_server_name?: unknown; action_description?: unknown }
        const name = typeof o.mcp_server_name === 'string' ? o.mcp_server_name : ''
        const desc = typeof o.action_description === 'string' ? o.action_description : ''
        if (!name && !desc) return null
        const stepPrefix = (typeof o.step === 'string' || typeof o.step === 'number') ? `${o.step}. ` : ''
        return `${stepPrefix}**${name}** — ${desc}`
      })
      .filter((l): l is string => l !== null)
    if (lines.length === 0) return text

    const body = lines.join('\n')
    return trailingText ? `${body}\n\n${trailingText}` : body
  } catch {
    return text
  }
}

export function extractPlanningSteps(flowData: unknown): PlanStep[] | undefined {
  if (!Array.isArray(flowData)) return undefined
  for (const n of flowData) {
    if (!n || typeof n !== 'object') continue
    const node = n as { nodeId?: unknown; data?: { state?: { planning_result?: unknown } } }
    if (typeof node.nodeId !== 'string' || !node.nodeId.startsWith('humanInputAgentflow')) continue
    const planning = node.data?.state?.planning_result

    let parsed: unknown
    if (typeof planning === 'string') {
      const trimmed = planning.trim()
      if (!trimmed) continue
      try { parsed = JSON.parse(trimmed) } catch { continue }
    } else if (Array.isArray(planning) || (planning && typeof planning === 'object')) {
      parsed = planning
    } else {
      continue
    }

    if (!Array.isArray(parsed) && parsed && typeof parsed === 'object') {
      const wrapped = (parsed as { planning_result?: unknown }).planning_result
      if (Array.isArray(wrapped)) parsed = wrapped
    }
    if (!Array.isArray(parsed)) continue

    const steps: PlanStep[] = []
    for (const p of parsed) {
      if (!p || typeof p !== 'object') continue
      const s = p as { step?: unknown; mcp_server_name?: unknown; action_description?: unknown }
      const stepStr = typeof s.step === 'string' ? s.step : typeof s.step === 'number' ? String(s.step) : null
      if (stepStr !== null && typeof s.mcp_server_name === 'string' && typeof s.action_description === 'string') {
        steps.push({ step: stepStr, mcp_server_name: s.mcp_server_name, action_description: s.action_description })
      }
    }
    if (steps.length > 0) return steps
  }
  return undefined
}

export interface ReasoningStep {
  nodeId: string
  nodeLabel: string
  rationale: string
}

export function extractReasoningSteps(flowData: unknown): ReasoningStep[] | undefined {
  if (!Array.isArray(flowData)) return undefined
  const steps: ReasoningStep[] = []
  for (const n of flowData) {
    if (!n || typeof n !== 'object') continue
    const node = n as { nodeId?: unknown; nodeLabel?: unknown; data?: { output?: { content?: unknown } } }
    if (typeof node.nodeLabel !== 'string' || !node.nodeLabel.startsWith('Visible Reasoner')) continue
    const content = node.data?.output?.content
    let rationale: unknown
    if (typeof content === 'string') {
      try { rationale = (JSON.parse(content) as { visible_rationale?: unknown }).visible_rationale } catch { continue }
    } else if (content && typeof content === 'object') {
      rationale = (content as { visible_rationale?: unknown }).visible_rationale
    } else {
      continue
    }
    if (typeof rationale === 'string' && rationale.trim()) {
      const trimmed = rationale.trim()
      if (/^\{\{[\s\S]*\}\}$/.test(trimmed)) continue
      steps.push({
        nodeId: typeof node.nodeId === 'string' ? node.nodeId : '',
        nodeLabel: node.nodeLabel,
        rationale,
      })
    }
  }
  return steps.length > 0 ? steps : undefined
}

export interface ChunkBbox {
  page: number
  l: number; t: number; r: number; b: number
  coordOrigin: string
  chunkIndex: number
}

export interface SourceDoc {
  fileName: string
  pageNos: number[]
  pageContent: string
  filePath?: string
  chunkBboxes?: ChunkBbox[]
}

function extractChunkBboxes(raw: unknown): ChunkBbox[] | undefined {
  if (!Array.isArray(raw)) return undefined
  const out: ChunkBbox[] = []
  raw.forEach((entry, idx) => {
    const items = Array.isArray(entry) ? entry.flat(Infinity) : [entry]
    for (const item of items) {
      if (!item || typeof item !== 'object') continue
      const c = item as { page?: unknown; type?: unknown; bbox?: unknown }
      const type = typeof c.type === 'string' ? c.type : ''
      if (type === 'page_header' || type === 'page_footer') continue  // 헤더/푸터 제외
      if (typeof c.page !== 'number') continue
      const bb = c.bbox
      if (!bb || typeof bb !== 'object') continue
      const { l, t, r, b, coord_origin } = bb as { l?: unknown; t?: unknown; r?: unknown; b?: unknown; coord_origin?: unknown }
      if (typeof l !== 'number' || typeof t !== 'number' || typeof r !== 'number' || typeof b !== 'number') continue
      out.push({ page: c.page, l, t, r, b, coordOrigin: typeof coord_origin === 'string' ? coord_origin : 'BOTTOMLEFT', chunkIndex: idx })
    }
  })
  return out.length > 0 ? out : undefined
}

export function extractSourceDocuments(raw: unknown): SourceDoc[] | undefined {
  if (!Array.isArray(raw)) return undefined
  const docs: SourceDoc[] = []
  for (const r of raw) {
    if (!r || typeof r !== 'object') continue
    const o = r as { metadata?: { file_name?: unknown; i_page?: unknown; page_no?: unknown; file_path?: unknown; chunk_bboxes?: unknown }; pageContent?: unknown }
    const fileName = o.metadata?.file_name
    const pageContent = o.pageContent
    if (typeof fileName !== 'string' || !fileName.trim()) continue
    if (typeof pageContent !== 'string') continue
    const iPage = o.metadata?.i_page
    const pageNo = o.metadata?.page_no
    let pageNos: number[]
    if (Array.isArray(iPage)) pageNos = iPage.filter((n): n is number => typeof n === 'number')
    else if (typeof iPage === 'number') pageNos = [iPage]
    else if (typeof pageNo === 'number') pageNos = [pageNo]
    else pageNos = []
    docs.push({
      fileName,
      pageNos,
      pageContent,
      filePath: typeof o.metadata?.file_path === 'string' ? o.metadata.file_path : undefined,
      chunkBboxes: extractChunkBboxes(o.metadata?.chunk_bboxes),
    })
  }
  return docs.length > 0 ? docs : undefined
}
