import { useState, useEffect, useRef, useMemo } from 'react'
import { useSearchParams } from 'react-router-dom'
import { apiFetch } from '../utils/apiFetch'
import { useAuth } from '../context/AuthContext'
import Sidebar from '../components/main/Sidebar'
import TopNavigation from '../components/main/TopNavigation'
import ChatMessageUser from '../components/main/ChatMessageUser'
import ChatMessageAI from '../components/main/ChatMessageAI'
import { isPlanResponse, extractPlanningSteps, extractStoppedNodeId, extractSourceDocuments, extractReasoningSteps, planContentToMarkdown, type PlanStep, type SourceDoc, type ChunkBbox, type ReasoningStep } from '../utils/planSignal'
import { isRndOk, RND_ALERT } from '../utils/rndApi'
import InputArea from '../components/main/InputArea'
import Panel from '../components/main/Panel'
import PdfViewerPanel from '../components/main/PdfViewerPanel'
import Modals, { type ToolCallDetail } from '../components/main/Modals'
import type { ReportSection, ReportToolServer, ReportToolCall, ReportReference } from '../components/main/ReportPreviewModal'
import { useToast } from '../context/ToastContext'
import { useChatSessions } from '../utils/useChatSessions'


function generateId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID()
  }
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
    const r = Math.random() * 16 | 0
    return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16)
  })
}

interface Message {
  id: string
  role: 'user' | 'ai'
  content?: string
  planContent?: string
  isGenerating?: boolean
  mcpDetails?: McpExecutedDetail[]
  // BACK_API.md 3-X: 응답 text가 planning_result JSON 형식이면 true → ChatMessageAI에 취소/수정/실행 버튼 활성
  isPlan?: boolean
  isPlanMsg?: boolean
  // ChatMessageAI 헤더 텍스트 — 실행(/proceed) 후 응답은 '추론 생성 계획', 그 외 (첫 질문/취소/수정)는 디폴트 '답변 생성 계획'
  headerLabel?: string
  // BACK_API.md 3-4 A안: state.planning_result에서 추출한 구조화 plan. text(자연어) 대신 우선 노출
  planSteps?: PlanStep[]
  // 추론 과정(Visible Reasoner) 스텝 — 실행(/proceed) 완료 답변에만. 답변 본문 위 타임라인 (PDF 12p)
  reasoningSteps?: ReasoningStep[]
  // 추론 과정 순차 완료 애니메이션 여부 — 라이브 proceed만 true. /log 복원 시 undefined(즉시 완료 상태로 표시)
  reasoningAnimate?: boolean
  // 출처 — 답변 영역 아래 + MCP 실행 정보 버튼 위에 펼침/접힘 형태로 노출
  sourceDocuments?: SourceDoc[]
  traceId?: string
}

interface TaskCard {
  id: string
  title: string
  description: string
  // ★ 백엔드 진단(2026-05-28): 기존 parseToolUsages 경로 미스매치 → input/output 정보 누락이었음.
  //   PDF 24p 정렬해서 toolCalls(tool/input/output)로 통일. 카운트도 toolCalls.length 기반.
  toolCalls: ToolCall[]
  // 카드 안에 "출처 ▶ / ▼" 토글 + file_name 리스트로 노출 (node.data.output.sourceDocuments 기반)
  sourceDocuments: SourceDoc[]
}

interface AgentFlowState {
  mcp_list: string
  planning_result: string | Array<{ step: string; mcp_server_name: string; action_description: string }>
  history: string
  step: string
  Excute_mcp?: string
  Excute_mcp_description?: string
  Excute_mcp_result?: unknown
  fix_mcp?: string
}

interface AgentFlowNode {
  nodeId: string
  nodeLabel: string
  data: {
    id: string
    name: string
    state: AgentFlowState
    // ★ visible_used_tools 노드의 MCP 실행 정보 (test.md 확정). content는 객체 또는 JSON string
    //   (proceed 응답=string / log 응답=객체 — ensureParsed로 양쪽 처리).
    // sourceDocuments는 노드별 출처 (있으면 Panel 카드 안 file_name 리스트).
    output?: {
      content?: unknown
      sourceDocuments?: unknown[]
    }
  }
  status: string
}

// MCP 도구 한 번의 호출 정보 — visible_used_tools.content.used_tools[] 정렬 (step/tool/args/result)
interface ToolCall {
  step: number
  tool: string
  input: unknown   // = used_tools[].args (모달 Input)
  output: unknown  // = used_tools[].result (모달 Output)
}

interface McpExecutedDetail {
  nodeId: string
  name: string          // = content.mcp_server (카드 제목)
  description: string   // = content.description (카드 설명)
  // 해당 노드의 도구 호출 내역 (step/tool/args/result)
  toolCalls: ToolCall[]
  // 노드 출처 — 있으면 Panel 카드 안에 file_name 리스트로 표시
  sourceDocuments: SourceDoc[]
}

interface ChatLogItem {
  trace_id?: string
  duration?: number
  data: {
    // ⚠️ 보통 string이지만, 보고서 생성 턴은 객체 {session_id, trace_ids}로 옴 → unknown으로 받고 사용처에서 좁힘
    question: unknown
    text: string
    chatMessageId?: string
    sourceDocuments?: unknown[]
    agentFlowExecutedData?: AgentFlowNode[]
  }
  feedback?: {
    thumbs?: 'up' | 'down' | null
    messages?: unknown[]
  } | null
}

interface ChatLogResponse {
  result: {
    code: number
    errMsg: string
    data: {
      chatlog: ChatLogItem[]
      chat_name?: string
      title?: string
      feedback_type?: string
      ui_template_id?: number
      icon?: string | null
      session?: { id: number; uid: string; title: string }
      reg_user?: { id: number; user_id: string; name: string }
    }
  }
  status: 'SUCCESS' | 'FAIL' | 'UNAUTHORIZED' | 'ERROR'
  statusName?: string
  statusCode: number
  message?: string
}

interface ChatQueryResponse {
  result: {
    code: number
    errMsg: string
    // 🚨 2026-05-28 백엔드 실측 확정: chatId는 `result.data.chatId`로 통일 — 최상위는 항상 null.
    //    구버전 호환 fallback 유지 (`data.result.chatId ?? data.result.data?.chatId`).
    chatId?: string | null
    data: {
      text: string
      chatId?: string                          // ★ 현재 chatId 위치 (2026-05-28~)
      chatMessageId?: string
      chat_session_title?: string
      sourceDocuments?: unknown[]
      // 7f7ff7e 추가. R&D 계획 식별: nodeId.startsWith('humanInputAgentflow') && status === 'STOPPED' (BACK_API 3-X)
      agentFlowExecutedData?: AgentFlowNode[]
      json?: null
    }
  }
  // d8a76d4 신규: `/chat/query` 응답에 jwai response headers 포함 (`x-genos-trace-id` 등).
  // `/chat/query/proceed`·`/reject`는 null (비대칭). abort 호출 시 trace_id 추출 용도.
  headers?: Record<string, string> | null
  status: 'SUCCESS' | 'FAIL' | 'UNAUTHORIZED' | 'ERROR'
  statusName?: string
  statusCode: number
  message?: string
}


// ChatMessageAI 헤더 라벨 — proceed로 만들어진 메시지(실제 답변 생성 중/완료)에 사용.
// 디폴트("답변 생성 계획" — plan 단계)와 구분. chat/log 복원 시에도 동일 라벨 사용.
const PROCEED_HEADER = 'AI 분석 결과'
const REASONING_HEADER = '추론 과정'

// proceed/reject/cancel 모두 startNodeId 항상 전달 — 추출된 STOPPED nodeId 없으면 백엔드 default 값으로 폴백.
const DEFAULT_START_NODE_ID = 'humanInputAgentflow_0'

// 사용자가 직접 입력한 modify(수정) 텍스트는 여기 걸리지 않으므로 그대로 노출됨.
// ('[REJECT]'=cancel entry는 user/AI 통째로 스킵하므로 여기 아닌 CANCEL_QUESTION 처리)
const SILENT_QUESTIONS = new Set(['진행', '취소'])

// cancel(/chat/query/cancel)이 백엔드에서 question="[REJECT]"로 저장하는 entry 식별값 (BACK_API d812d7b).
const CANCEL_QUESTION = '[REJECT]'

// 보고서 생성 산출물 식별
function isReportArtifact(v: unknown): boolean {
  if (v && typeof v === 'object') {
    return 'session_id' in (v as object) && 'trace_ids' in (v as object)
  }
  if (typeof v === 'string') {
    const t = v.trim()
    if (!t.startsWith('{')) return false
    try {
      const o = JSON.parse(t) as Record<string, unknown>
      return !!o && typeof o === 'object' && 'session_id' in o && 'trace_ids' in o
    } catch { return false }
  }
  return false
}

// 보고서 생성 시 "보고서 적용" 체크 가능한 답변 최대 개수 (기획서 확인 모달 — 초과 시 alert).
const REPORT_MAX = 10

const FAILED_ANSWER_TEXT = '답변 생성에 실패했습니다.'
function isFailedAnswer(text?: string): boolean {
  return (text ?? '').trim() === FAILED_ANSWER_TEXT
}

interface ReportData {
  text?: string
  // 공식 가이드
  overrideConfig?: { vars?: { report_md?: unknown; status?: unknown; message?: unknown } }
}
interface ReportResponse {
  result?: { data?: ReportData | null }
}

type ReportExtract =
  | { ok: true; markdown: string; title: string; sections: ReportSection[] }
  | { ok: false; message: string }

// 응답의 sections[] (unknown) → 타입 안전 ReportSection[]로 정규화
function normalizeToolServers(raw: unknown): ReportToolServer[] {
  if (!Array.isArray(raw)) return []
  const out: ReportToolServer[] = []
  for (const s of raw) {
    if (!s || typeof s !== 'object') continue
    const o = s as Record<string, unknown>
    const server = typeof o.server === 'string' ? o.server : ''
    if (!server) continue
    const calls: ReportToolCall[] = Array.isArray(o.calls)
      ? o.calls
          .filter((c): c is Record<string, unknown> => !!c && typeof c === 'object')
          .map(c => ({
            plan_step: typeof c.plan_step === 'number' ? c.plan_step : undefined,
            tool: typeof c.tool === 'string' ? c.tool : '',
            args: c.args,
            result: typeof c.result === 'string' ? c.result : undefined,
            result_truncated: typeof c.result_truncated === 'string' ? c.result_truncated : undefined,
          }))
      : []
    out.push({ server, calls })
  }
  return out
}
// 응답 references[] (unknown) → ReportReference[] (본문 [N] 인용 매칭용)
function normalizeReferences(raw: unknown): ReportReference[] {
  if (!Array.isArray(raw)) return []
  const out: ReportReference[] = []
  for (const r of raw) {
    if (!r || typeof r !== 'object') continue
    const o = r as Record<string, unknown>
    const marker = typeof o.marker === 'string' ? o.marker : (typeof o.marker === 'number' ? String(o.marker) : '')
    if (!marker) continue
    out.push({
      marker,
      type: o.type === 'web' ? 'web' : 'doc',
      title: typeof o.title === 'string' ? o.title : '',
      file_name: typeof o.file_name === 'string' ? o.file_name : undefined,
      url: typeof o.url === 'string' ? o.url : undefined,
    })
  }
  return out
}
function normalizeSections(raw: unknown): ReportSection[] {
  if (!Array.isArray(raw)) return []
  const out: ReportSection[] = []
  raw.forEach((s, i) => {
    if (!s || typeof s !== 'object') return
    const o = s as Record<string, unknown>
    out.push({
      id: typeof o.id === 'string' ? o.id : `sec_${i}`,
      title: typeof o.title === 'string' ? o.title : '',
      section_type: typeof o.section_type === 'string' ? o.section_type : undefined,
      text: typeof o.text === 'string' ? o.text : '',
      references: normalizeReferences(o.references),
      tool_servers: normalizeToolServers(o.tool_servers),
    })
  })
  return out
}

// 보고서 추출
function extractReport(data: ReportData | null | undefined): ReportExtract {
  const vars = data?.overrideConfig?.vars
  const status = typeof vars?.status === 'string' ? vars.status : undefined
  if (status !== undefined && status !== 'SUCCESS') {
    return { ok: false, message: typeof vars?.message === 'string' ? vars.message : '' }
  }
  const fromVars = vars?.report_md
  if (typeof fromVars === 'string' && fromVars.trim()) {
    return { ok: true, markdown: fromVars, title: '', sections: [] }
  }
  const text = data?.text ?? ''
  const t = text.trim()
  if (t.startsWith('{')) {
    try {
      const parsed = JSON.parse(t) as Record<string, unknown>
      const title = typeof parsed.title === 'string' ? parsed.title.trim() : ''
      const sections = normalizeSections(parsed.sections)
      // ⚠️ 백엔드 markdown 필드는 잘리거나(예: Abstract 중간 절단) 섹션 번호가 어긋나는 등 불안정 →
      //   sections가 있으면 신뢰 가능한 sections로 본문을 합성 (보기/수정/PDF 단일 소스). 없을 때만 markdown 필드.
      const markdown = sections.length
        ? composeMarkdown(title, sections)
        : (typeof parsed.markdown === 'string' ? parsed.markdown
          : typeof parsed.report_md === 'string' ? parsed.report_md : '')
      if (markdown.trim() || sections.length) return { ok: true, markdown, title, sections }
    } catch { /* JSON 파싱 실패 → 순수 markdown 폴백 */ }
  }
  if (t) return { ok: true, markdown: text, title: '', sections: [] }
  return { ok: false, message: '' }
}

// sections → 단일 markdown 본문 합성: # title + 섹션마다 ## 제목 + 본문 (섹션 제목은 깨끗한 원본 그대로, 번호 X)
function composeMarkdown(title: string, sections: ReportSection[]): string {
  const parts: string[] = []
  if (title) parts.push(`# ${title}`)
  for (const s of sections) {
    if (s.title) parts.push(`## ${s.title}`)
    if (s.text.trim()) parts.push(s.text)
  }
  return parts.join('\n\n')
}

// 파일명 기본값용 YYMMDD (오늘) — 예: 2026-06-23 → 260623
function todayYYMMDD(): string {
  const d = new Date()
  const yy = String(d.getFullYear()).slice(2)
  const mm = String(d.getMonth() + 1).padStart(2, '0')
  const dd = String(d.getDate()).padStart(2, '0')
  return `${yy}${mm}${dd}`
}

// ============ visible_used_tools 추출 (test.md 확정판) ============
// 백엔드가 MCP 실행 정보를 nodeLabel이 'visible_used_tools'로 시작하는 노드의
// data.output.content로 내려줌. content는 proceed 응답에선 JSON string, /log 응답에선 객체로 와서
// ensureParsed로 양쪽 호환. content = { mcp_server, description, used_tools: [{step, tool, args, result}] }
// (구버전 agentAgentflow_* + usedTools 경로는 백엔드가 더는 안 채우므로 제거)

interface UsedToolStep {
  step: number
  tool: string
  args: unknown
  result: unknown
}
interface VisibleUsedToolsContent {
  mcp_server: string
  description: string
  used_tools: UsedToolStep[]
}

// string이면 JSON.parse, 이미 객체면 그대로 — proceed(string)/log(객체) 비대칭 호환
function ensureParsed<T = unknown>(v: unknown): T | null {
  if (v == null) return null
  if (typeof v === 'string') {
    try { return JSON.parse(v) as T } catch { return null }
  }
  return v as T
}

// nodeLabel이 visible_used_tools로 시작하는 노드 1개 = MCP 카드 1개 (MCP별로 노드가 따로 옴)
function extractExecutedMcpDetails(flowData: AgentFlowNode[]): McpExecutedDetail[] {
  const out: McpExecutedDetail[] = []
  for (const n of flowData) {
    if (typeof n.nodeLabel !== 'string' || !n.nodeLabel.startsWith('visible_used_tools')) continue
    const content = ensureParsed<VisibleUsedToolsContent>(n.data?.output?.content)
    if (!content || typeof content.mcp_server !== 'string' || !Array.isArray(content.used_tools)) continue
    const toolCalls: ToolCall[] = content.used_tools
      .filter((t): t is UsedToolStep => !!t && typeof t === 'object')
      .slice()
      .sort((a, b) => (typeof a.step === 'number' ? a.step : 0) - (typeof b.step === 'number' ? b.step : 0))
      .map(t => ({
        step: typeof t.step === 'number' ? t.step : 0,
        tool: typeof t.tool === 'string' ? t.tool : '',
        input: t.args,     // 모달 Input
        output: t.result,  // 모달 Output
      }))
    out.push({
      nodeId: n.nodeId,
      name: content.mcp_server,
      description: typeof content.description === 'string' ? content.description : '',
      toolCalls,
      // 노드별 출처 — 있으면 카드 안 "출처" 섹션에 file_name 나열 (없으면 미노출)
      sourceDocuments: extractSourceDocuments(n.data?.output?.sourceDocuments) ?? [],
    })
  }
  return out
}

function mcpDetailsToTaskCards(details: McpExecutedDetail[]): TaskCard[] {
  // visible_used_tools 노드 1개 = 카드 1개 (test.md §5-2). MCP별로 노드가 따로 와서 dedup 불필요.
  // id에 index를 붙여 유니크 보장 — 카드끼리 같이 열리고/닫히던 버그 방지.
  return details.map((d, i) => ({
    id: `${d.nodeId}-${i}`,
    title: d.name,
    description: typeof d.description === 'string' ? d.description : '',
    toolCalls: d.toolCalls,
    sourceDocuments: d.sourceDocuments,
  }))
}

// MCP 전체보기 (PDF 13p) — 세션 내 MCP 실행 정보가 있는 프롬프트 목록 빌드.
// mcpDetails 있는 AI 메시지마다 그룹 1개, 프롬프트 텍스트 = 직전 user 메시지(진행/취소는 silent라 원 질문이 잡힘).
// 데이터는 messages에 이미 있음(라이브·/log 복원 공통) — 별도 API 불필요 (rdtest.md §PDF 13p).
interface McpPromptGroup { id: string; prompt: string; cards: TaskCard[] }
function buildMcpPromptGroups(messages: Message[]): McpPromptGroup[] {
  const groups: McpPromptGroup[] = []
  for (let i = 0; i < messages.length; i++) {
    const m = messages[i]
    if (m.role !== 'ai' || !m.mcpDetails || m.mcpDetails.length === 0) continue
    let prompt = ''
    for (let j = i - 1; j >= 0; j--) {
      if (messages[j].role === 'user') { prompt = messages[j].content ?? ''; break }
    }
    groups.push({ id: m.id, prompt, cards: mcpDetailsToTaskCards(m.mcpDetails) })
  }
  return groups
}

// proceed/reject 응답의 본문 텍스트 fallback 추출 — text 필드가 응답에 없을 때 사용
// agentFlowExecutedData 노드들을 역순 순회하면서 state의 결과 필드에서 의미 있는 값 추출
function extractTextFromAgentFlow(flowData: AgentFlowNode[] | null | undefined): string | undefined {
  if (!flowData || flowData.length === 0) return undefined
  for (let i = flowData.length - 1; i >= 0; i--) {
    const state = flowData[i]!.data?.state
    if (!state) continue
    // 우선순위: Excute_mcp_result(MCP 실행 결과) > planning_result(계획) > Excute_mcp_description
    const result = state.Excute_mcp_result
    if (typeof result === 'string' && result.trim()) return result
    const planning = state.planning_result
    if (typeof planning === 'string' && planning.trim()) return planning
    const desc = state.Excute_mcp_description
    if (typeof desc === 'string' && desc.trim()) return desc
  }
  return undefined
}

export default function MainPage() {
  const [messages, setMessages] = useState<Message[]>([])
  const [alertMessage, setAlertMessage] = useState<string | null>(null)
  const [chatId, setChatId] = useState<string | null>(() =>
    new URLSearchParams(window.location.search).get('session')
  )
  // BACK_API 3-6 (d8a76d4): /chat/query 응답 headers의 `x-genos-trace-id` — /chat/abort 호출 시 trace_id로 사용.
  // /chat/query/proceed·reject 응답엔 headers null이라 query 응답 직후 받은 값 유지.
  const [traceId, setTraceId] = useState<string | null>(null)
  // BACK_API 3-1 (d8a76d4): /chat/query 응답의 agentFlowExecutedData에서 STOPPED 노드의 nodeId.
  // proceed/reject 호출 시 body.startNodeId에 포함. 실측은 항상 humanInputAgentflow_0이지만
  // PDF 28-29p 명세상 "action 이벤트의 nodeId" 사용이라 jwai 변경 대비 명시 전달.
  const [startNodeId, setStartNodeId] = useState<string | null>(null)
  // ★ 세션 목록/관리는 공용 훅 단일 소스 (McpServerPage와 공유). 별칭으로 기존 변수명 유지 →
  //   표시/조회 사용처 무수정. 세터는 전부 훅 내부 (새 세션 추가는 prependSession, active-세션 정리는 아래 핸들러가 담당)
  const {
    pinnedList: pinnedChatList,
    normalList: normalChatList,
    prependSession,
    normalHasNext, loadingMore: loadingMoreSessions, loadMore: loadMoreSessions,
    pinChat, unpinChat, renameChat, deleteChat, bulkDelete,
  } = useChatSessions({ onError: setAlertMessage })
  // MCP 패널 모드 — id 있으면 그 프롬프트 카드만 바로(단일, 메시지별 버튼/proceed) / undefined면 전체 프롬프트 목록(헤더 전체보기)
  const [mcpSingleId, setMcpSingleId] = useState<string | undefined>(undefined)
  const [targetChatUid, setTargetChatUid] = useState<string | null>(null)
  const abortControllerRef = useRef<AbortController | null>(null)
  // fake streaming 핸들 — complete(중단 시 전체 노출+마무리) / discard(화면 전환 시 타이머만 정리)
  const streamRef = useRef<{ complete: () => void; discard: () => void } | null>(null)
  const scrollContainerRef = useRef<HTMLDivElement>(null)
  const isScrolledUpRef = useRef(false)
  const scrollInstantRef = useRef(false)
  // 현재 보고 있는(또는 로딩 중인) 세션 uid를 동기적으로 추적 — 같은 세션 연타 시 중복 /chat/log 호출 차단.
  //   activeChatId(state)는 비동기라 연타 직후 stale → 비교로 못 막음. ref로 즉시 판정.
  const currentSessionRef = useRef<string | null>(
    new URLSearchParams(window.location.search).get('session')
  )
  // 진행 중인 /chat/log 요청 — 다른 세션으로 전환하면 이전 요청을 abort (stale 응답이 화면 덮어쓰기 방지).
  const logAbortRef = useRef<AbortController | null>(null)
  const { user } = useAuth()

  const [isLoadingLog, setIsLoadingLog] = useState<boolean>(() =>
    !!new URLSearchParams(window.location.search).get('session')
  )

  const [, setSearchParams] = useSearchParams()
  const [activeChatId, setActiveChatId] = useState<string | null>(() =>
    new URLSearchParams(window.location.search).get('session')
  )

  // 새로고침 시 URL ?session=uid 복원: activeChatId/chatId는 useState 초기값으로 이미 설정됨
  // 여기서는 chat/log API 호출만 비동기로 수행 (effect 내 동기 setState 금지 우회)
  useEffect(() => {
    const sessionId = new URLSearchParams(window.location.search).get('session')
    if (!sessionId) return
    const controller = new AbortController()
    logAbortRef.current = controller
    apiFetch('/api/v1/rnd/chat/log', {
      method: 'POST',
      body: JSON.stringify({ chat_session_id: sessionId }),
      signal: controller.signal,
    })
      .then(res => res.json())
      .then((data: ChatLogResponse) => {
        if (data.status === 'SUCCESS' && data.result?.code === 0) {
          const chatlog = data.result.data?.chatlog ?? []
          const msgs: Message[] = []
          // BACK_API 4-X 수석님 가이드: 마지막 메시지만 isPlan 검출 적용.
          // 중간 메시지의 STOPPED는 이미 다음 메시지에서 처리된 흐름이므로 버튼 노출 X.
          const lastIdx = chatlog.length - 1
          chatlog.forEach((item, idx) => {
            const question = item.data.question
            if (isReportArtifact(question)) return
            const qStr = typeof question === 'string' ? question : ''
            if (qStr === CANCEL_QUESTION) return
            const isSilentQ = SILENT_QUESTIONS.has(qStr)
            // '진행'/'취소' 같은 시스템 키워드 + 빈/공백 question은 user 메시지로 노출 X
            if (!isSilentQ && qStr.trim()) {
              msgs.push({ id: generateId(), role: 'user', content: qStr })
            }
            const msgIsPlan = isPlanResponse({ data: { agentFlowExecutedData: item.data.agentFlowExecutedData } })
            const isLastPlan = idx === lastIdx && msgIsPlan
            msgs.push({
              id: item.data.chatMessageId ?? generateId(),
              role: 'ai',
              planContent: item.data.text,
              isGenerating: false,
              mcpDetails: item.data.agentFlowExecutedData
                ? extractExecutedMcpDetails(item.data.agentFlowExecutedData)
                : undefined,
              isPlan: isLastPlan,
              isPlanMsg: msgIsPlan,
              // BACK_API 3-4 A안: plan 메시지면 state.planning_result 우선 노출 (text 자연어와 불일치 가능)
              planSteps: isLastPlan ? extractPlanningSteps(item.data.agentFlowExecutedData) : undefined,
              // 추론 과정 — proceed(최종 답변) turn만. plan turn은 planning 노드 1개뿐이라 msgIsPlan으로 스킵 (rdtest.md §4-X-3 /log)
              reasoningSteps: msgIsPlan ? undefined : extractReasoningSteps(item.data.agentFlowExecutedData),
              headerLabel: msgIsPlan ? undefined : PROCEED_HEADER,
              sourceDocuments: extractSourceDocuments(item.data.sourceDocuments),
              traceId: item.trace_id,
            })
          })
          if (msgs.length > 0) {
            scrollInstantRef.current = true
            setMessages(msgs)
            return
          }
        } else {
          setAlertMessage(RND_ALERT.log)
        }
        setSearchParams({})
        setActiveChatId(null)
        setChatId(null)
      })
      .catch((err: unknown) => {
        // 다른 세션 클릭으로 취소된 경우 — 무시 (그쪽이 화면/스피너 담당)
        if (err instanceof Error && err.name === 'AbortError') return
        console.error('[chat/log restore] 처리 중 오류:', err)
        setAlertMessage(RND_ALERT.log)
        setSearchParams({})
        setActiveChatId(null)
        setChatId(null)
      })
      .finally(() => {
        if (logAbortRef.current === controller) {
          logAbortRef.current = null
          setIsLoadingLog(false)
        }
      })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const [sidebarOpen, setSidebarOpen] = useState(() =>
    localStorage.getItem('sidebarOpen') === 'true'
  )
  const [panelOpen, setPanelOpen] = useState(false)
  const mcpPromptGroups = useMemo(() => buildMcpPromptGroups(messages), [messages])
  const panelHasData = mcpPromptGroups.length > 0
  // 출처 PDF 뷰어 — openSourcePdf가 만든 Blob URL을 오른쪽 패널 iframe에 표시.
  // 새 PDF 열기/닫기 시 이전 Blob URL은 revokeObjectURL로 정리 (메모리 누수 방지).
  const [pdfViewer, setPdfViewer] = useState<{ url: string; fileName: string; bboxes?: ChunkBbox[]; initialPage?: number } | null>(null)
  const openPdfViewer = (url: string, fileName: string, bboxes?: ChunkBbox[], initialPage?: number) => {
    setPdfViewer(prev => {
      if (prev) URL.revokeObjectURL(prev.url)
      return { url, fileName, bboxes, initialPage }
    })
  }
  const closePdfViewer = () => {
    setPdfViewer(prev => {
      if (prev) URL.revokeObjectURL(prev.url)
      return null
    })
  }

  const leaveSession = () => {
    logAbortRef.current?.abort()
    logAbortRef.current = null
    currentSessionRef.current = null
    setReportCheckedIds(new Set())   // 보고서 적용 선택 초기화
  }
  const [profileMenuOpen, setProfileMenuOpen] = useState(false)
  const [utilMenuOpen, setUtilMenuOpen] = useState(false)
  const [deleteModal, setDeleteModal] = useState(false)
  const [changeNameModal, setChangeNameModal] = useState(false)
  const [viewReportModal, setViewReportModal] = useState(false)
  // MCP 패널에서 도구 li 클릭 시 모달에 보여줄 선택된 도구 호출 (mcpName + tool/input/output)
  const [selectedToolCall, setSelectedToolCall] = useState<ToolCallDetail | null>(null)
  const [reportCancelModal, setReportCancelModal] = useState(false)
  const [reportCheckedIds, setReportCheckedIds] = useState<Set<string>>(new Set())
  const [reportPreviewOpen, setReportPreviewOpen] = useState(false)
  const [reportLoading, setReportLoading] = useState(false)
  const [reportMarkdown, setReportMarkdown] = useState('')
  const [reportTitle, setReportTitle] = useState('')
  const [reportSections, setReportSections] = useState<ReportSection[]>([])
  const reportSubmittingRef = useRef(false)
  const reportAbortRef = useRef<AbortController | null>(null)
  const [reportAllConfirm, setReportAllConfirm] = useState(false)
  const [bulkDeleteModal, setBulkDeleteModal] = useState(false)
  const [pendingBulkUids, setPendingBulkUids] = useState<string[]>([])
  const [bulkDeleteResetSignal, setBulkDeleteResetSignal] = useState(0)
  const [showScrollBtn, setShowScrollBtn] = useState(false)
  const [isCancelling, setIsCancelling] = useState(false)

  // App-level Toast 사용 (페이지 이동해도 사라지지 않음)
  const { showToast } = useToast()

  const isDetail = activeChatId !== null
  const isGenerating = messages.some(m => m.role === 'ai' && m.isGenerating)

  const handleScroll = () => {
    const el = scrollContainerRef.current
    if (!el) return
    const scrolledUp = el.scrollHeight - el.scrollTop - el.clientHeight > 50
    setShowScrollBtn(scrolledUp)
    isScrolledUpRef.current = scrolledUp
  }

  const scrollToBottom = () => {
    scrollContainerRef.current?.scrollTo({ top: scrollContainerRef.current.scrollHeight, behavior: 'smooth' })
  }

  // messages 변경 시 자동 스크롤 (사용자가 위로 스크롤 중이 아닐 때만)
  useEffect(() => {
    if (isLoadingLog) return
    if (!isScrolledUpRef.current) {
      if (scrollInstantRef.current) {
        const el = scrollContainerRef.current
        if (el) el.scrollTop = el.scrollHeight
        scrollInstantRef.current = false
      } else {
        scrollToBottom()
      }
    }
  }, [messages, isLoadingLog])

  // Alt+N 단축키: 새 채팅
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.altKey && e.key === 'n') {
        e.preventDefault()
        if (!isGenerating) {
          streamRef.current?.discard()
          logAbortRef.current?.abort(); logAbortRef.current = null; currentSessionRef.current = null
          setReportCheckedIds(new Set())
          setSearchParams({})
          setActiveChatId(null)
          setMessages([])
          setChatId(null)
          setPanelOpen(false)
          setShowScrollBtn(false)
          isScrolledUpRef.current = false
          // 새 채팅 → PDF split 닫기 (closePdfViewer와 동일 — effect deps 회피 위해 인라인)
          setPdfViewer(prev => { if (prev) URL.revokeObjectURL(prev.url); return null })
        }
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [isGenerating, setSearchParams])

  const closeAllMenus = () => {
    setPanelOpen(false)
    setProfileMenuOpen(false)
    setUtilMenuOpen(false)
  }

  const handleMenuToggle = (menu: 'profile' | 'util') => {
    const current = menu === 'profile' ? profileMenuOpen : utilMenuOpen
    closeAllMenus()
    if (!current) {
      if (menu === 'profile') setProfileMenuOpen(true)
      else setUtilMenuOpen(true)
    }
  }

  const handleBulkDeleteRequest = (uids: string[]) => {
    setPendingBulkUids(uids)
    setBulkDeleteModal(true)
  }

  // 삭제/이름변경은 훅(API+목록+토스트+에러)에 위임. active-세션이 지워졌을 때의 채팅뷰 정리만 MainPage 고유.
  const handleBulkDeleteConfirm = async () => {
    setBulkDeleteModal(false)
    setBulkDeleteResetSignal(s => s + 1)  // 삭제 확정 → Sidebar 선택삭제 모드/선택 초기화
    const uids = pendingBulkUids
    setPendingBulkUids([])
    const ok = await bulkDelete(uids)
    if (ok && activeChatId && uids.includes(activeChatId)) {
      streamRef.current?.discard()
      leaveSession()
      setSearchParams({})
      setActiveChatId(null)
      setMessages([])
      setChatId(null)
      setPanelOpen(false)
      closePdfViewer()  // 보던 세션이 선택삭제에 포함 → PDF split 닫기
    }
  }

  const handleDeleteConfirm = async () => {
    const uid = targetChatUid
    if (!uid) { setDeleteModal(false); return }
    const ok = await deleteChat(uid)
    if (ok && activeChatId === uid) {
      streamRef.current?.discard()
      leaveSession()
      setSearchParams({})
      setActiveChatId(null)
      setMessages([])
      setChatId(null)
      setPanelOpen(false)
      closePdfViewer()  // 보던 세션 삭제 → PDF split 닫기 (안 닫으면 welcome인데 우측 PDF 남아 레이아웃 깨짐)
    }
    setDeleteModal(false)
    setTargetChatUid(null)
  }

  const handleRenameConfirm = async (newTitle: string) => {
    const uid = targetChatUid
    if (uid) await renameChat(uid, newTitle)
    setChangeNameModal(false)
    setTargetChatUid(null)
  }

  // ===== Fake streaming =====
  // ⚠️ 실제 SSE 붙으면 이 함수만 reader 루프로 교체
  //
  // 설계:
  //  - 메시지가 여러 개라 단일 text state(useFakeStream 훅) 대신 messages[aiId].planContent를 직접 갱신.
  //  - 응답이 길어도 답답하지 않게 "토큰 perTick개씩" 노출 → 총 tick 수를 ~250로 제한(약 6초 상한).
  //  - 공백/개행은 split 정규식으로 보존 (마크다운 레이아웃 유지).
  //  - finalize: 스트리밍이 끝난 뒤에야 isGenerating=false + 버튼/출처/MCP 노출 (생성 중 느낌 유지).
  const streamMessageText = (aiId: string, fullText: string, finalize: () => void) => {
    streamRef.current?.discard()  // 진행 중인 스트림 정리

    const tokens = fullText.split(/(\s+)/)             // 단어 + 공백/개행 토큰 (구분자 보존)
    const total = tokens.length
    const perTick = Math.max(1, Math.ceil(total / 250)) // 길수록 한 번에 더 많이 → 총 시간 상한
    const INTERVAL = 24
    let i = 0
    let timer = 0
    let cancelled = false

    const clear = () => { cancelled = true; window.clearTimeout(timer); streamRef.current = null }

    const tick = () => {
      if (cancelled) return
      i = Math.min(total, i + perTick)
      const partial = tokens.slice(0, i).join('')
      setMessages(prev => prev.map(m => m.id === aiId ? { ...m, planContent: partial } : m))
      if (i >= total) { clear(); finalize(); return }
      timer = window.setTimeout(tick, INTERVAL)
    }

    streamRef.current = {
      // 중단 버튼 — 이미 전체 응답을 받은 상태라 즉시 전체 노출 + 마무리 (데이터 손실 없음)
      complete: () => {
        if (cancelled) return
        clear()
        setMessages(prev => prev.map(m => m.id === aiId ? { ...m, planContent: fullText } : m))
        finalize()
      },
      // 화면 전환(새 채팅/다른 세션 등) — 타이머만 정리, 마무리 X (메시지 자체가 교체됨)
      discard: () => { clear() },
    }
    tick()
  }

  const handleSend = async (content: string) => {
    const isFirstMessage = chatId === null

    if (activeChatId === null) setActiveChatId('new')

    const userId = generateId()
    const aiId = generateId()

    setMessages(prev => [
      ...prev,
      { id: userId, role: 'user', content },
      { id: aiId, role: 'ai', isGenerating: true, planContent: '' },
    ])

    const controller = new AbortController()
    abortControllerRef.current = controller
    const timeoutId = setTimeout(() => controller.abort(), 600_000)

    try {
      const res = await apiFetch('/api/v1/rnd/chat/query', {
        method: 'POST',
        // 첫 질문은 chatId 생략 → 서버가 자동 발급 / 후속 질문은 result.chatId 재사용
        body: JSON.stringify(chatId ? { chatId, question: content } : { question: content }),
        signal: controller.signal,
      })

      const data: ChatQueryResponse = await res.json()

      if (isRndOk(data)) {
        // ★ chatId — BACK_API.md b9c3699 이후 백엔드 빌드에 따라 위치가 다를 수 있음:
        //   - 명세상: result.chatId (data 바깥) — 명시적으로 "실측 검증" 표기됨
        //   - 실측: result.data.chatId — b9c3699 DTO 변경 후 백엔드 응답이 여기로 옴 (2026-05-27 로컬 검증)
        // 둘 다 fallback으로 처리 (백엔드 응답 변동에 안전)
        const receivedChatId = data.result.chatId ?? data.result.data?.chatId

        if (receivedChatId) {
          setChatId(receivedChatId)
          setActiveChatId(receivedChatId)
          setSearchParams({ session: receivedChatId })
          currentSessionRef.current = receivedChatId  // 사이드바에서 같은 세션 재클릭 시 log 재호출(라이브 메시지 덮어쓰기) 방지
        } else {
          console.warn('[handleSend] response missing chatId in BOTH result.chatId and result.data.chatId', data.result)
        }

        if (isFirstMessage && receivedChatId) {
          // 제목은 first_user_message(=방금 보낸 첫 메시지 content) 기준으로 통일 — CSS ellipsis가 '...' 처리.
          // chat_session_title은 백엔드가 임의 길이로 잘라 보내서(예: '...pu') 다른 항목과 불일치 → 사용 안 함
          prependSession({ uid: receivedChatId, title: content, date: '방금' })
        }

        const flowData = data.result.data.agentFlowExecutedData
        // ★ /log 복원 경로와 동일 소스로 통일 — state.Excute_mcp(실제 실행된 MCP)만 카드로.
        //   이전엔 state.mcp_list(서버 카탈로그 25개)를 썼는데 raw 영어 description이 그대로 노출돼 문제였음.
        const mcpDetails = flowData ? extractExecutedMcpDetails(flowData) : undefined

        const text = data.result.data.text
        // BACK_API 3-X (2026-05-28 확정): agentFlowExecutedData의 humanInputAgentflow_* STOPPED 노드로 plan 판정
        const isPlan = isPlanResponse(data.result)
        // BACK_API 3-4 A안: plan 메시지일 때만 state.planning_result 추출 (일반 답변엔 노출 X)
        const planSteps = isPlan ? extractPlanningSteps(flowData) : undefined
        // 출처 — plan 여부 무관 (있으면 항상 노출)
        const sourceDocuments = extractSourceDocuments(data.result.data.sourceDocuments)
        // 추론 과정 — plan이 아닌(최종 답변) 응답에만 Visible Reasoner 스텝 추출 (PDF 12p)
        const reasoningSteps = isPlan ? undefined : extractReasoningSteps(flowData)

        // BACK_API 3-6 (d8a76d4): /chat/query 응답 headers의 trace_id 저장 → /chat/abort 호출에 사용
        const newTraceId = data.headers?.['x-genos-trace-id']
        if (newTraceId) setTraceId(newTraceId)
        // BACK_API 3-1 (d8a76d4): STOPPED 노드의 nodeId 저장 → proceed/reject body.startNodeId에 사용
        const newStartNodeId = extractStoppedNodeId(flowData)
        if (newStartNodeId) setStartNodeId(newStartNodeId)

        const aiHeaderLabel = isPlan ? undefined : PROCEED_HEADER
        // 스트리밍 종료(또는 중단) 후에야 isGenerating 해제 + 버튼/출처/MCP 노출 + 패널 오픈
        const finalize = () => {
          setMessages(prev => prev.map(m =>
            m.id === aiId ? { ...m, isGenerating: false, mcpDetails, isPlan, isPlanMsg: isPlan, planSteps, reasoningSteps, sourceDocuments, headerLabel: aiHeaderLabel, traceId: newTraceId ?? undefined } : m
          ))
          if (mcpDetails && mcpDetails.length > 0) {
            setMcpSingleId(aiId)
            setPanelOpen(true)
          }
        }

        if (reasoningSteps) setMessages(prev => prev.map(m => m.id === aiId ? { ...m, reasoningSteps, reasoningAnimate: true, headerLabel: aiHeaderLabel } : m))
        // plan(raw JSON)은 미리 마크다운 리스트로 변환해 스트리밍 → JSON 깜빡임 없이 plan도 스르륵 노출.
        // 일반 답변은 planContentToMarkdown이 그대로 통과시킴 (idempotent).
        streamMessageText(aiId, planContentToMarkdown(text), finalize)
      } else {
        // 케이스 2: 빈 AI 말풍선 + 방금 보낸 user 말풍선 제거 → alert로 재전송 안내 (화면 깔끔)
        setMessages(prev => prev.filter(m => m.id !== aiId && m.id !== userId))
        if (isFirstMessage) setActiveChatId(null)  // 첫 질문 실패 → welcome 화면 복귀
        setAlertMessage(RND_ALERT.query)
      }
    } catch (err) {
      const isAbort = err instanceof Error && err.name === 'AbortError'
      if (isAbort) {
        // 사용자가 stop 버튼으로 명시적 중단 — 빈 AI 메시지(추론 과정 헤더만 남는 케이스) 제거
        setMessages(prev => prev.filter(m => m.id !== aiId))
      } else {
        // 네트워크/서버 에러 — 케이스 2와 동일 복구 (말풍선 제거 + alert)
        setMessages(prev => prev.filter(m => m.id !== aiId && m.id !== userId))
        if (isFirstMessage) setActiveChatId(null)
        setAlertMessage(RND_ALERT.query)
      }
    } finally {
      clearTimeout(timeoutId)
      abortControllerRef.current = null
    }
  }

  // BACK_API 3-6 + test.md 검증:
  //   1) 클라이언트 측 fetch 즉시 취소 (AbortController) — 사용자 UI 응답성 확보
  //   2) 서버 측 jwai 워크플로우 정리 요청 (/chat/abort) — chat_id + trace_id 둘 다 snake_case 필수
  // ⚠️ 동기 응답 기반이라 효과 제한적 (응답이 거의 끝나갈 시점 호출 가능성 높음). 그래도 명세상 호출.
  // chatId/traceId 없으면 (init 직후 등) abort API 스킵.
  const handleStop = () => {
    // fake streaming 중이면 스트림만 즉시 완료 (서버 응답은 이미 수신 완료 → /abort 불필요)
    if (streamRef.current) { streamRef.current.complete(); return }
    abortControllerRef.current?.abort()
    if (!chatId || !traceId) return
    apiFetch('/api/v1/rnd/chat/abort', {
      method: 'POST',
      body: JSON.stringify({ chat_id: chatId, trace_id: traceId }),
    }).catch(() => { /* abort 자체 실패는 silent — UX에 영향 X */ })
  }

  // 새 AI 메시지 추가 없음. plan 메시지의 isPlan만 false로 (버튼 숨김).
  const handlePlanAbort = async (fromMsgId: string) => {
    setMessages(prev => prev.map(m => m.id === fromMsgId ? { ...m, isPlan: false } : m))
    if (!chatId) return
    setIsCancelling(true)
    try {
      const res = await apiFetch('/api/v1/rnd/chat/query/cancel', {
        method: 'POST',
        body: JSON.stringify({ chatId, startNodeId: startNodeId ?? DEFAULT_START_NODE_ID }),
      })
      const data = await res.json() as unknown
      // 케이스 4: 취소 실패 → 낙관적으로 숨겼던 plan 버튼 복구 + alert (다시 시도 가능)
      if (!isRndOk(data)) {
        setMessages(prev => prev.map(m => m.id === fromMsgId ? { ...m, isPlan: true } : m))
        setAlertMessage(RND_ALERT.cancel)
      }
    } catch {
      setMessages(prev => prev.map(m => m.id === fromMsgId ? { ...m, isPlan: true } : m))
      setAlertMessage(RND_ALERT.cancel)
    }
    finally { setIsCancelling(false) }
  }

  const handlePlanAction = async (action: 'proceed' | 'modify', question: string, fromMsgId: string) => {
    if (!chatId) {
      console.warn('[handlePlanAction] chatId is null — skipping', { action })
      showToast('chatId가 없어 요청을 보낼 수 없습니다. 새로고침 후 다시 시도해 주세요.')
      return
    }
    const endpoint: 'reject' | 'proceed' = action === 'proceed' ? 'proceed' : 'reject'
    const aiId = generateId()
    // 수정(modify)만 사용자 입력값을 채팅에 노출 — 실패 시 제거하려고 id를 변수로 보관
    const userMsgId = action === 'modify' ? generateId() : null
    // 실행(proceed) 로딩 중 헤더 = "추론 과정" (AI 추론 중). 완료 시 finalize가 PROCEED_HEADER("AI 분석 결과")로 교체.
    //   그 외(modify=reject)는 디폴트 "답변 생성 계획" (보통 재계획 응답이라)
    const aiHeaderLabel = action === 'proceed' ? REASONING_HEADER : undefined
    // 케이스 5(proceed)/3(modify=reject) 실패 시 띄울 alert
    const alertMsg = action === 'proceed' ? RND_ALERT.proceed : RND_ALERT.reject
    setMessages(prev => [
      // 이전 plan 메시지의 버튼 숨김 — 이미 액션이 선택됨
      ...prev.map(m => m.id === fromMsgId ? { ...m, isPlan: false } : m),
      // 수정 액션만 사용자 입력값을 채팅에 노출 (진행은 silent)
      ...(userMsgId ? [{ id: userMsgId, role: 'user' as const, content: question }] : []),
      { id: aiId, role: 'ai', isGenerating: true, planContent: '', headerLabel: aiHeaderLabel },
    ])

    // BACK_API 3-1 (d8a76d4): proceed/reject body에 startNodeId 명시.
    // /chat/query 응답에서 추출한 STOPPED 노드 nodeId 사용. 없으면 백엔드 default('humanInputAgentflow_0') 위임.
    // 명시 전달이 안전 — 현재 jwai는 항상 humanInputAgentflow_0이지만 향후 다른 nodeId 줄 경우 대비.
    const body: Record<string, unknown> = { chatId, startNodeId: startNodeId ?? DEFAULT_START_NODE_ID }
    if (action === 'modify') body.question = question

    const callOnce = async (): Promise<ChatQueryResponse> => {
      const controller = new AbortController()
      abortControllerRef.current = controller
      const timeoutId = setTimeout(() => controller.abort(), 600_000)
      try {
        const res = await apiFetch(`/api/v1/rnd/chat/query/${endpoint}`, {
          method: 'POST',
          body: JSON.stringify(body),
          signal: controller.signal,
        })
        return await res.json() as ChatQueryResponse
      } finally {
        clearTimeout(timeoutId)
        abortControllerRef.current = null
      }
    }

    try {
      // ★ proceed/reject는 1회만 호출 (수석 확정 2026-06-11: 백엔드가 1회 proceed로 실행 완료 응답을 줌).
      //   이전 자동 1회 재호출(2단계 proceed 우회)은 제거 — 두 번 실행되면 안 됨.
      const data = await callOnce()

      if (isRndOk(data)) {
        // ⚠️ proceed 응답은 구조 다름: text가 result.data.text가 아니라 result.data.json 안에 있음
        //   또는 agentFlowExecutedData 마지막 노드의 결과에 본문이 들어옴
        //   - result.data.text                                          ← 일반 query 응답
        //   - result.data.json.text                                     ← proceed/reject 응답 (가능성)
        //   - result.data.json.agentFlowExecutedData[].data.state...    ← MCP 실행 결과
        const rd = data.result.data as unknown as Record<string, unknown>
        const jsonField = (rd?.json ?? null) as Record<string, unknown> | null
        const text =
          (data.result.data.text as string | undefined)
          ?? (jsonField?.text as string | undefined)
          ?? extractTextFromAgentFlow(
            (data.result.data.agentFlowExecutedData ?? (jsonField?.agentFlowExecutedData as AgentFlowNode[] | undefined)) ?? null
          )
          ?? '(응답 본문을 찾을 수 없습니다 — 콘솔 확인)'
        const flowData = data.result.data.agentFlowExecutedData ?? (jsonField?.agentFlowExecutedData as AgentFlowNode[] | undefined)
        console.log('[handlePlanAction] response', {
          textFound: !!(data.result.data.text || jsonField?.text),
          flowDataLen: flowData?.length ?? 0,
          dataKeys: Object.keys(data.result.data ?? {}),
          jsonKeys: jsonField ? Object.keys(jsonField) : null,
        })
        const mcpDetails = flowData ? extractExecutedMcpDetails(flowData) : undefined
        // 응답이 또 plan일 수 있음 (proceed 후 재계획 패턴 — BACK_API 3-X 실측). agentFlowExecutedData 기반 판정
        const isPlan = isPlanResponse(data.result)
        // BACK_API 3-4 A안: plan일 때만 state.planning_result 추출 (text 자연어와 불일치 가능 — JSON 우선)
        const planSteps = isPlan ? extractPlanningSteps(flowData) : undefined
        // 출처 — proceed 응답은 data.result.data 또는 jsonField에 있음 (구조 비대칭 — text/flowData와 동일 패턴)
        const sourceDocuments = extractSourceDocuments(
          (data.result.data as { sourceDocuments?: unknown }).sourceDocuments ?? jsonField?.sourceDocuments
        )
        // 추론 과정 — proceed로 실제 실행된 최종 답변에 Visible Reasoner 스텝 (또 plan이면 X)
        const reasoningSteps = isPlan ? undefined : extractReasoningSteps(flowData)
        // reject/proceed 응답에도 chatId가 갱신될 수 있음 — 양쪽 위치 모두 lookup
        const echoChatId = data.result.chatId ?? data.result.data?.chatId
        if (echoChatId && echoChatId !== chatId) {
          setChatId(echoChatId)
        }
        // BACK_API 3-1 (d8a76d4): proceed/reject 응답이 또 plan이면 STOPPED 노드 nodeId 갱신
        const newStartNodeId = extractStoppedNodeId(flowData)
        if (newStartNodeId) setStartNodeId(newStartNodeId)
        //   보고서 적용용으로 메시지에 trace_id 부여 + abort용 traceId state도 최신값으로 갱신.
        const newTraceId = data.headers?.['x-genos-trace-id']
        if (newTraceId) setTraceId(newTraceId)

        // 헤더 라벨은 "내용이 계획이냐"로 결정 — proceed로 만들었어도 응답이 또 계획이면 '답변 생성 계획'
        const resolvedHeaderLabel = isPlan ? undefined : PROCEED_HEADER
        // 스트리밍 종료(또는 중단) 후에야 isGenerating 해제 + 버튼/출처/MCP 노출 + 패널 오픈
        const finalize = () => {
          setMessages(prev => prev.map(m =>
            m.id === aiId ? { ...m, isGenerating: false, mcpDetails, isPlan, isPlanMsg: isPlan, planSteps, reasoningSteps, sourceDocuments, headerLabel: resolvedHeaderLabel, traceId: newTraceId ?? undefined } : m
          ))
          if (mcpDetails && mcpDetails.length > 0) {
            setMcpSingleId(aiId)
            setPanelOpen(true)
          }
        }
        if (reasoningSteps) setMessages(prev => prev.map(m => m.id === aiId ? { ...m, reasoningSteps, reasoningAnimate: true, headerLabel: resolvedHeaderLabel } : m))
        // plan(raw JSON)은 미리 마크다운 리스트로 변환해 스트리밍 → JSON 깜빡임 없이 plan도 스르륵 노출.
        // 일반 답변은 planContentToMarkdown이 그대로 통과시킴 (idempotent).
        streamMessageText(aiId, planContentToMarkdown(text), finalize)
      } else {
        // 케이스 3/5: 빈 AI 말풍선(+modify면 입력 user 말풍선) 제거 + plan 버튼 복구 + alert
        setMessages(prev => prev
          .filter(m => m.id !== aiId && (userMsgId === null || m.id !== userMsgId))
          .map(m => m.id === fromMsgId ? { ...m, isPlan: true } : m)
        )
        setAlertMessage(alertMsg)
      }
    } catch (err) {
      // (controller/timeoutId 정리는 callOnce 내부 finally에서 처리됨)
      const isAbort = err instanceof Error && err.name === 'AbortError'
      if (isAbort) {
        // 사용자가 stop 버튼으로 명시적 중단 — 빈 AI 메시지만 제거 + plan 복구 (입력값은 보존)
        setMessages(prev => prev
          .filter(m => m.id !== aiId)
          .map(m => m.id === fromMsgId ? { ...m, isPlan: true } : m)
        )
      } else {
        setMessages(prev => prev
          .filter(m => m.id !== aiId && (userMsgId === null || m.id !== userMsgId))
          .map(m => m.id === fromMsgId ? { ...m, isPlan: true } : m)
        )
        setAlertMessage(alertMsg)
      }
    }
  }

  const handleSelectChat = async (uid: string) => {
    // 같은 세션 재클릭 — 이미 보고 있거나 로딩 중이면 무시 (연타 시 중복 /chat/log 호출·에러 방지)
    if (currentSessionRef.current === uid) return
    currentSessionRef.current = uid
    // 다른 세션으로 전환 — 진행 중이던 이전 log 요청 취소
    logAbortRef.current?.abort()
    const controller = new AbortController()
    logAbortRef.current = controller

    streamRef.current?.discard()
    setReportCheckedIds(new Set())   // 세션 전환 → 보고서 적용 선택 초기화
    setSearchParams({ session: uid })
    setActiveChatId(uid)
    setChatId(uid)
    setMessages([])
    setPanelOpen(false)
    closePdfViewer()  // 다른 세션 전환 → 이전 세션 출처 PDF split 닫기
    setShowScrollBtn(false)
    isScrolledUpRef.current = false
    setIsLoadingLog(true)
    try {
      const res = await apiFetch('/api/v1/rnd/chat/log', {
        method: 'POST',
        body: JSON.stringify({ chat_session_id: uid }),
        signal: controller.signal,
      })
      const data: ChatLogResponse = await res.json()
      if (isRndOk(data)) {
        const chatlog = data.result.data?.chatlog ?? []
        const newMessages: Message[] = []
        // 마지막 메시지에만 isPlan 검출 (중간 STOPPED는 다음 메시지에서 처리된 흐름)
        const lastIdx = chatlog.length - 1
        chatlog.forEach((item, idx) => {
          const question = item.data.question
          if (isReportArtifact(question)) return
          const qStr = typeof question === 'string' ? question : ''
          if (qStr === CANCEL_QUESTION) return
          const isSilentQ = SILENT_QUESTIONS.has(qStr)
          // '진행'/'취소' 시스템 키워드 + 빈/공백 question은 user 메시지 노출 X (빈 말풍선 방지)
          if (!isSilentQ && qStr.trim()) {
            newMessages.push({ id: generateId(), role: 'user', content: qStr })
          }
          const msgIsPlan = isPlanResponse({ data: { agentFlowExecutedData: item.data.agentFlowExecutedData } })
          const isLastPlan = idx === lastIdx && msgIsPlan
          newMessages.push({
            id: item.data.chatMessageId ?? generateId(),
            role: 'ai',
            planContent: item.data.text,
            isGenerating: false,
            mcpDetails: item.data.agentFlowExecutedData
              ? extractExecutedMcpDetails(item.data.agentFlowExecutedData)
              : undefined,
            isPlan: isLastPlan,
            isPlanMsg: msgIsPlan,   // plan 성격(마지막 여부 무관) — 보고서 적용 노출 제외 판정용
            planSteps: isLastPlan ? extractPlanningSteps(item.data.agentFlowExecutedData) : undefined,
            reasoningSteps: msgIsPlan ? undefined : extractReasoningSteps(item.data.agentFlowExecutedData),
            headerLabel: msgIsPlan ? undefined : PROCEED_HEADER,
            sourceDocuments: extractSourceDocuments(item.data.sourceDocuments),
            traceId: item.trace_id,
          })
        })
        scrollInstantRef.current = true
        setMessages(newMessages)
      } else {
        setAlertMessage(RND_ALERT.log)
      }
    } catch (err) {
      // 다른 세션으로 전환되며 취소된 요청 — 조용히 무시 (새 요청이 화면 갱신/스피너 담당)
      if (err instanceof Error && err.name === 'AbortError') return
      console.error('[chat/log select] 처리 중 오류:', err)
      setAlertMessage(RND_ALERT.log)
    } finally {
      // 내 요청이 여전히 최신일 때만 정리 — 더 새로운 전환이 이미 일어났으면 그쪽이 책임
      if (logAbortRef.current === controller) {
        logAbortRef.current = null
        setIsLoadingLog(false)
      }
    }
  }

  // 보고서 적용 체크 토글 — 최대 REPORT_MAX개. 초과 시도 시 기획서 안내 모달.
  const handleToggleReportApply = (msgId: string) => {
    setReportCheckedIds(prev => {
      const next = new Set(prev)
      if (next.has(msgId)) {
        next.delete(msgId)
        return next
      }
      if (next.size >= REPORT_MAX) {
        setAlertMessage(`보고서 생성 시 적용 가능한 개수는 최대 ${REPORT_MAX}개이며,\n초과된 내용은 포함할 수 없습니다.`)
        return prev
      }
      next.add(msgId)
      return next
    })
  }

  const applicableReportTraceIds = () =>
    messages
      .filter(m => m.role === 'ai' && !m.isPlanMsg && !!m.traceId && !isFailedAnswer(m.planContent))
      .map(m => m.traceId as string)

  const runReport = async (traceIds: string[]) => {
    if (reportSubmittingRef.current) return
    if (!chatId) { showToast('세션 정보가 없어 보고서를 생성할 수 없습니다.'); return }
    if (traceIds.length === 0) return
    reportSubmittingRef.current = true
    setReportMarkdown('')
    setReportTitle('')
    setReportSections([])
    setReportLoading(true)
    setReportPreviewOpen(true)
    const controller = new AbortController()
    reportAbortRef.current = controller
    let timedOut = false
    const timeoutId = setTimeout(() => { timedOut = true; controller.abort() }, 600_000)
    try {
      const res = await apiFetch('/api/v1/rnd/chat/report', {
        method: 'POST',
        body: JSON.stringify({ chatId, question: { session_id: chatId, trace_ids: traceIds } }),
        signal: controller.signal,
      })
      const data = await res.json() as ReportResponse
      const parsed = extractReport(data.result?.data)
      if (!parsed.ok) {
        setReportPreviewOpen(false)
        setAlertMessage(parsed.message.trim()
          ? parsed.message
          : '보고서 생성에 실패했습니다.\n적용한 답변이 충분한지 확인 후\n다시 시도해 주세요.')
        return
      }
      setReportMarkdown(parsed.markdown)
      setReportTitle(parsed.title)
      setReportSections(parsed.sections)
    } catch (err) {
      if (err instanceof Error && err.name === 'AbortError') {
        if (timedOut) {
          setReportPreviewOpen(false)
          setAlertMessage('보고서 생성 시간이 초과되었습니다.\n잠시 후 다시 시도해 주세요.')
        }
      } else {
        setReportPreviewOpen(false)
        setAlertMessage('보고서 생성 중 오류가 발생했습니다.\n잠시 후 다시 시도해 주세요.')
      }
    } finally {
      clearTimeout(timeoutId)
      if (reportAbortRef.current === controller) reportAbortRef.current = null
      setReportLoading(false)
      reportSubmittingRef.current = false
    }
  }

  // 상단 다운로드 아이콘 클릭 — 보고서 적용 항목 수로 분기
  const handleGenerateReport = () => {
    if (reportSubmittingRef.current) return
    if (!chatId) { showToast('세션 정보가 없어 보고서를 생성할 수 없습니다.'); return }
    const selectedTraceIds = messages
      .filter(m => m.role === 'ai' && !m.isPlanMsg && reportCheckedIds.has(m.id) && !!m.traceId && !isFailedAnswer(m.planContent))
      .map(m => m.traceId as string)
    if (selectedTraceIds.length > 0) {
      runReport(selectedTraceIds)
      return
    }
    // 선택 0개 — 전체 AI 분석결과 개수로 분기
    const allCount = applicableReportTraceIds().length
    if (allCount === 0) {
      setAlertMessage('보고서에 적용할\nAI 분석 결과가 없습니다.')
      return
    }
    if (allCount <= REPORT_MAX) {
      setReportAllConfirm(true)   // 3-1: "모든 AI 분석 결과를 보고서에 적용하시겠습니까?"
    } else {
      setAlertMessage('선택된 내용이 없습니다.\n보고서에 적용할 내용을 선택해 주세요.')   // 3-2
    }
  }

  // 3-1 확인 → 채팅 내 모든 AI 분석 결과로 보고서 생성
  const handleConfirmReportAll = () => {
    setReportAllConfirm(false)
    runReport(applicableReportTraceIds())
  }

  // 미리보기 모달 기본 파일명 — "보고서title_YYMMDD". title 없으면(구버전/실패) 세션 제목 폴백.
  const reportNameBase = reportTitle || ([...pinnedChatList, ...normalChatList].find(c => c.uid === activeChatId)?.title ?? '보고서')
  const reportDefaultFilename = `${reportNameBase}_${todayYYMMDD()}`

  return (
    <div className={`wrap ${sidebarOpen ? 'open' : 'close'}${pdfViewer ? ' pdf-open' : ''}`}>
      <Sidebar
        pinnedList={pinnedChatList}
        normalList={normalChatList}
        activeChatId={activeChatId}
        onToggleSidebar={() => setSidebarOpen(p => { localStorage.setItem('sidebarOpen', String(!p)); return !p })}
        onNewChat={() => { streamRef.current?.discard(); leaveSession(); setSearchParams({}); setActiveChatId(null); setMessages([]); setChatId(null); setPanelOpen(false); closePdfViewer(); setShowScrollBtn(false); isScrolledUpRef.current = false; setIsLoadingLog(false) }}
        onSelectChat={handleSelectChat}
        onDeleteModal={uid => { setTargetChatUid(uid); setDeleteModal(true) }}
        onChangeNameModal={uid => { setTargetChatUid(uid); setChangeNameModal(true) }}
        onPinChat={pinChat}
        onUnpinChat={unpinChat}
        onBulkDeleteRequest={handleBulkDeleteRequest}
        resetSelectionSignal={bulkDeleteResetSignal}
        showMcpInfo
        hasMore={normalHasNext}
        loadingMore={loadingMoreSessions}
        onLoadMore={loadMoreSessions}
      />

      <div className={`container-wrap${isDetail ? ' detail' : ''}`}>
        <TopNavigation
          isDetail={isDetail}
          chatTitle={[...pinnedChatList, ...normalChatList].find(c => c.uid === activeChatId)?.title ?? ''}
          profileMenuOpen={profileMenuOpen}
          utilMenuOpen={utilMenuOpen}
          onMenuToggle={handleMenuToggle}
          onDeleteModal={() => { if (activeChatId) { setTargetChatUid(activeChatId); setDeleteModal(true) } }}
          onChangeNameModal={() => { if (activeChatId) { setTargetChatUid(activeChatId); setChangeNameModal(true) } }}
          isActivePinned={pinnedChatList.some(c => c.uid === activeChatId)}
          onPinChat={() => { if (activeChatId) { pinChat(activeChatId); setUtilMenuOpen(false) } }}
          onUnpinChat={() => { if (activeChatId) { unpinChat(activeChatId); setUtilMenuOpen(false) } }}
          onCloseMenus={closeAllMenus}
          onDownloadReport={handleGenerateReport}
          // MCP 전체보기 (헤더 아이콘) — 토글. 열 땐 전체 프롬프트 목록(단일 아님 = undefined)
          onMcpPanel={() => {
            if (panelOpen && mcpSingleId === undefined) { setPanelOpen(false) }
            else { setMcpSingleId(undefined); setPanelOpen(true) }
          }}
        />

        <div className={`work-split${pdfViewer ? ' pdf-open' : ''}`}>
          <div className="chat-col">
        <div ref={scrollContainerRef} className={`content-wrap${isDetail ? ' scroll-container' : ''}`} onScroll={handleScroll}>
          <div className="content">
            <div className="content-inner">
              {!isDetail ? (
                <>
                  <div className="welcome-msg">
                    <div className="text-wrap01">안녕하세요 {user?.userName ?? ''}님,</div>
                    <div className="text-wrap02">오늘은 어떤 연구 데이터를 분석해 드릴까요?</div>
                  </div>
                  <InputArea
                    isGenerating={false}
                    disabled={false}
                    onSend={handleSend}
                    onStop={handleStop}
                    focusKey="welcome"
                  />
                </>
              ) : isLoadingLog ? (
                <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', minHeight: 'calc(100vh - 220px)' }}>
                  <div className="fixed-8bar-spinner" style={{ transform: 'scale(2)' }}>
                    {Array.from({ length: 8 }, (_, i) => (
                      <div key={i} className={`bar bar${i + 1}`} />
                    ))}
                  </div>
                </div>
              ) : (
                <>
                  {messages.map(msg =>
                    msg.role === 'user'
                      ? <ChatMessageUser key={msg.id} content={msg.content ?? ''} onCopy={() => showToast('복사되었습니다.')} />
                      : <ChatMessageAI
                          key={msg.id}
                          id={msg.id}
                          planContent={msg.planContent ?? ''}
                          isGenerating={msg.isGenerating ?? false}
                          mcpDetails={msg.mcpDetails}
                          onMcpOpen={() => {
                            setProfileMenuOpen(false)
                            setUtilMenuOpen(false)
                            // 메시지별 버튼 — 그 프롬프트 카드만 바로(단일 모드). 이미 그 상태로 열려있으면 닫기(토글)
                            if (panelOpen && mcpSingleId === msg.id) {
                              setPanelOpen(false)
                            } else {
                              setMcpSingleId(msg.id)
                              setPanelOpen(true)
                            }
                          }}
                          // ★ 채팅 노출 라벨 = API question 일치 (취소/진행은 라벨 그대로 question으로)
                          isPlan={msg.isPlan ?? false}
                          planActionsDisabled={isGenerating}
                          onReject={() => handlePlanAbort(msg.id)}
                          onProceed={() => handlePlanAction('proceed', '진행', msg.id)}
                          onModifyReject={q => handlePlanAction('modify', q, msg.id)}
                          headerLabel={msg.headerLabel}
                          planSteps={msg.planSteps}
                          reasoningSteps={msg.reasoningSteps}
                          reasoningAnimate={msg.reasoningAnimate}
                          sourceDocuments={msg.sourceDocuments}
                          onPdfError={msg => setAlertMessage(msg)}
                          onPdfView={openPdfViewer}
                          canApplyReport={!msg.isPlanMsg && !!msg.traceId && !isFailedAnswer(msg.planContent)}
                          reportApplyChecked={reportCheckedIds.has(msg.id)}
                          onToggleReportApply={() => handleToggleReportApply(msg.id)}
                        />
                  )}
                  
                  {isCancelling && (
                    <div className="ai-header" style={{ marginTop: 40 }}>
                      <div className="title-group" style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', columnGap: 12 }}>
                        <div className="fixed-8bar-spinner">
                          {Array.from({ length: 8 }, (_, i) => (
                            <div key={i} className={`bar bar${i + 1}`} />
                          ))}
                        </div>
                        <div className="text-wrap" style={{ fontWeight: 600, fontSize: 18, lineHeight: '120%', color: '#82828d' }}>취소 요청을 처리중 입니다.</div>
                      </div>
                    </div>
                  )}
                </>
              )}
            </div>
          </div>
        </div>
          </div>

          {pdfViewer && (
            <PdfViewerPanel
              key={pdfViewer.url}
              url={pdfViewer.url}
              fileName={pdfViewer.fileName}
              bboxes={pdfViewer.bboxes}
              initialPage={pdfViewer.initialPage}
              onClose={closePdfViewer}
            />
          )}
        </div>

        {isDetail && (
          <InputArea
            isGenerating={isGenerating}
            disabled={isCancelling}
            onSend={handleSend}
            onStop={handleStop}
            focusKey={activeChatId}
          />
        )}
      </div>

      <div className={`bottom-floating-wrap${isDetail && showScrollBtn ? ' active' : ''}`}>
        <div className="btn-bottom-floating" onClick={scrollToBottom} />
      </div>

      <Panel
        isOpen={panelOpen}
        hasData={panelHasData}
        promptGroups={mcpPromptGroups}
        singleGroupId={mcpSingleId}
        onClose={() => setPanelOpen(false)}
        onToolClick={(mcpName, call) => {
          setSelectedToolCall({ mcpName, step: call.step, tool: call.tool, input: call.input, output: call.output })
          setViewReportModal(true)
        }}
        onPdfError={msg => setAlertMessage(msg)}
        onPdfView={openPdfViewer}
      />

      <Modals
        deleteModal={deleteModal}
        changeNameModal={changeNameModal}
        viewReportModal={viewReportModal}
        reportCancelModal={reportCancelModal}
        bulkDeleteModal={bulkDeleteModal}
        chatTitle={targetChatUid ? ([...pinnedChatList, ...normalChatList].find(c => c.uid === targetChatUid)?.title ?? '') : ''}
        onCloseDelete={() => { setDeleteModal(false); setTargetChatUid(null) }}
        onConfirmDelete={handleDeleteConfirm}
        onCloseChangeName={() => { setChangeNameModal(false); setTargetChatUid(null) }}
        onConfirmChangeName={handleRenameConfirm}
        onCloseViewReport={() => { setViewReportModal(false); setSelectedToolCall(null) }}
        onCloseReportCancel={() => setReportCancelModal(false)}
        onReportCancel={() => { setViewReportModal(false); setReportCancelModal(true) }}
        toolCallDetail={selectedToolCall}
        onCloseBulkDelete={() => { setBulkDeleteModal(false); setPendingBulkUids([]) }}
        onConfirmBulkDelete={handleBulkDeleteConfirm}
        alertMessage={alertMessage}
        onCloseAlert={() => setAlertMessage(null)}
        reportPreviewOpen={reportPreviewOpen}
        reportLoading={reportLoading}
        reportMarkdown={reportMarkdown}
        reportTitle={reportTitle}
        reportSections={reportSections}
        reportFilename={reportDefaultFilename}
        onCloseReportPreview={() => { reportAbortRef.current?.abort(); setReportPreviewOpen(false) }}
        onReportError={msg => setAlertMessage(msg)}
        reportAllConfirmModal={reportAllConfirm}
        onCloseReportAllConfirm={() => setReportAllConfirm(false)}
        onConfirmReportAll={handleConfirmReportAll}
      />

      {/* Toast는 App-level(ToastProvider)에서 렌더 — 페이지 이동해도 사라지지 않음 */}
    </div>
  )
}
