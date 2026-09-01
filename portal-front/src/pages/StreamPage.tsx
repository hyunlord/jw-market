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
import { createPlanActionLock, runWithPlanActionLock } from '../utils/planActionLock'


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
    isPlan?: boolean
    isPlanMsg?: boolean
    headerLabel?: string
    planSteps?: PlanStep[]
    reasoningSteps?: ReasoningStep[]
    reasoningAnimate?: boolean
    allowLiveReasoning?: boolean
    loadingLabel?: string 
    sourceDocuments?: SourceDoc[]
    traceId?: string
}

interface TaskCard {
    id: string
    title: string
    description: string
    toolCalls: ToolCall[]
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
        output?: {
            content?: unknown
            sourceDocuments?: unknown[]
        }
    }
    status: string
}

interface ToolCall {
    step: number
    tool: string
    input: unknown
    output: unknown
}

interface McpExecutedDetail {
    nodeId: string
    name: string
    description: string
    toolCalls: ToolCall[]
    sourceDocuments: SourceDoc[]
}

interface ChatLogItem {
    trace_id?: string
    duration?: number
    data: {
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

const PROCEED_HEADER = 'AI 분석 결과'

const DEFAULT_START_NODE_ID = 'humanInputAgentflow_0'

const SILENT_QUESTIONS = new Set(['진행', '취소'])

const CANCEL_QUESTION = '[REJECT]'

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

const REPORT_MAX = 10

const FAILED_ANSWER_TEXT = '답변 생성에 실패했습니다.'
function isFailedAnswer(text?: string): boolean {
    return (text ?? '').trim() === FAILED_ANSWER_TEXT
}

interface ReportData {
    text?: string
    overrideConfig?: { vars?: { report_md?: unknown; status?: unknown; message?: unknown } }
}
interface ReportResponse {
    result?: { data?: ReportData | null }
}

type ReportExtract =
    | { ok: true; markdown: string; title: string; sections: ReportSection[] }
    | { ok: false; message: string }

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
        const parsed = safeJsonParse<Record<string, unknown>>(t)
        if (parsed) {
            const title = typeof parsed.title === 'string' ? parsed.title.trim() : ''
            const sections = normalizeSections(parsed.sections)
            const markdown = sections.length
                ? composeMarkdown(title, sections)
                : (typeof parsed.markdown === 'string' ? parsed.markdown
                    : typeof parsed.report_md === 'string' ? parsed.report_md : '')
            if (markdown.trim() || sections.length) return { ok: true, markdown, title, sections }
        }
    }
    if (t) return { ok: true, markdown: text, title: '', sections: [] }
    return { ok: false, message: '' }
}

function composeMarkdown(title: string, sections: ReportSection[]): string {
    const parts: string[] = []
    if (title) parts.push(`# ${title}`)
    for (const s of sections) {
        if (s.title) parts.push(`## ${s.title}`)
        if (s.text.trim()) parts.push(s.text)
    }
    return parts.join('\n\n')
}

function todayYYMMDD(): string {
    const d = new Date()
    const yy = String(d.getFullYear()).slice(2)
    const mm = String(d.getMonth() + 1).padStart(2, '0')
    const dd = String(d.getDate()).padStart(2, '0')
    return `${yy}${mm}${dd}`
}


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

function ensureParsed<T = unknown>(v: unknown): T | null {
    if (v == null) return null
    if (typeof v === 'string') {
        try { return JSON.parse(v) as T } catch { return null }
    }
    return v as T
}

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
                input: t.args,
                output: t.result,
            }))
        out.push({
            nodeId: n.nodeId,
            name: content.mcp_server,
            description: typeof content.description === 'string' ? content.description : '',
            toolCalls,
            sourceDocuments: extractSourceDocuments(n.data?.output?.sourceDocuments) ?? [],
        })
    }
    return out
}

function mcpDetailsToTaskCards(details: McpExecutedDetail[]): TaskCard[] {
    return details.map((d, i) => ({
        id: `${d.nodeId}-${i}`,
        title: d.name,
        description: typeof d.description === 'string' ? d.description : '',
        toolCalls: d.toolCalls,
        sourceDocuments: d.sourceDocuments,
    }))
}

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

function extractTextFromAgentFlow(flowData: AgentFlowNode[] | null | undefined): string | undefined {
    if (!flowData || flowData.length === 0) return undefined
    for (let i = flowData.length - 1; i >= 0; i--) {
        const state = flowData[i]!.data?.state
        if (!state) continue
        const result = state.Excute_mcp_result
        if (typeof result === 'string' && result.trim()) return result
        const planning = state.planning_result
        if (typeof planning === 'string' && planning.trim()) return planning
        const desc = state.Excute_mcp_description
        if (typeof desc === 'string' && desc.trim()) return desc
    }
    return undefined
}

function extractStreamFinalText(flowData: AgentFlowNode[] | undefined): string | undefined {
    if (!flowData?.length) return undefined
    const finalReport = flowData.find(n => n.nodeId === 'llmAgentflow_0' && n.nodeLabel === 'final report')
    const fr = finalReport?.data?.output?.content
    if (typeof fr === 'string' && fr.trim()) return fr
    const humanInput = flowData.find(n => n.nodeId === 'humanInputAgentflow_0')
    const hi = humanInput?.data?.output?.content
    if (typeof hi === 'string' && hi.trim()) return hi
    return undefined
}

function extractTraceIdFromFlow(flowData: AgentFlowNode[] | undefined): string | undefined {
    if (!flowData?.length) return undefined
    const m = JSON.stringify(flowData).match(
        /x-genos-trace-id[\\":\s]*([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/i,
    )
    return m?.[1]
}

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

async function* parseSseStream(body: ReadableStream<Uint8Array>): AsyncGenerator<SseFrame> {
    const reader = body.getReader()
    const decoder = new TextDecoder()
    let buffer = ''
    try {
        for (;;) {
            const { done, value } = await reader.read()
            if (done) break
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
        reader.releaseLock()
    }
}

function tokenText(data: string): string {
    try {
        const o: unknown = JSON.parse(data)
        if (typeof o === 'string') return o
        if (o && typeof o === 'object' && typeof (o as { data?: unknown }).data === 'string') {
            return (o as { data: string }).data
        }
        return data
    } catch {
        return data
    }
}

function safeJsonParse<T>(s: string): T | null {
    try { return JSON.parse(s) as T } catch { return null }
}

function normalizeFlowData(raw: unknown): AgentFlowNode[] | undefined {
    if (Array.isArray(raw)) return raw as AgentFlowNode[]
    if (raw && typeof raw === 'object') {
        const o = raw as Record<string, unknown>
        const arr = o.data ?? o.agentFlowExecutedData
        if (Array.isArray(arr)) return arr as AgentFlowNode[]
    }
    return undefined
}

function normalizeSourceDocsRaw(raw: unknown): unknown[] | undefined {
    if (Array.isArray(raw)) return raw
    if (raw && typeof raw === 'object') {
        const arr = (raw as Record<string, unknown>).data
        if (Array.isArray(arr)) return arr
    }
    return undefined
}

// ★ 스트림엔 sourceDocuments 이벤트가 없음(test2.md 실측). 출처는 'SD parsing'류 노드의
//   output.content(= [{pageContent, metadata:{file_name, i_page, file_path}}] 배열)에 실려 옴.
//   flowData에서 그 형태의 배열을 담은 노드를 찾아 raw로 반환(extractSourceDocuments가 파싱). 여러 개면 가장 완전한(마지막) 것.
function extractStreamSourceDocs(flowData: AgentFlowNode[] | undefined): unknown[] | undefined {
    if (!flowData) return undefined
    let found: unknown[] | undefined
    for (const n of flowData) {
        const parsed = ensureParsed<unknown>(n.data?.output?.content)
        if (Array.isArray(parsed) && parsed.length > 0) {
            const first = parsed[0] as { pageContent?: unknown; metadata?: { file_name?: unknown } }
            if (typeof first?.pageContent === 'string' && typeof first?.metadata?.file_name === 'string') {
                found = parsed
            }
        }
    }
    return found
}

interface QueryResultData {
    text?: unknown
    sourceDocuments?: unknown
    webReferences?: unknown
    agentFlowExecutedData?: unknown
    chat_session_title?: unknown
}

export default function MainPage() {
    const [messages, setMessages] = useState<Message[]>([])
    const [alertMessage, setAlertMessage] = useState<string | null>(null)
    const [chatId, setChatId] = useState<string | null>(() =>
        new URLSearchParams(window.location.search).get('session')
    )
    const [traceId, setTraceId] = useState<string | null>(null)
    const [startNodeId, setStartNodeId] = useState<string | null>(null)
    const {
        pinnedList: pinnedChatList,
        normalList: normalChatList,
        prependSession,
        normalHasNext, loadingMore: loadingMoreSessions, loadMore: loadMoreSessions,
        pinChat, unpinChat, renameChat, deleteChat, bulkDelete,
    } = useChatSessions({ onError: setAlertMessage })
    const [mcpSingleId, setMcpSingleId] = useState<string | undefined>(undefined)
    // MCP 패널 실시간 자동 오픈 — 요청(aiId)당 1회만 열고, 사용자가 닫으면 다시 안 열리게 추적
    const mcpPanelOpenedForRef = useRef<string | null>(null)
    // "도구 실행 중" 스피너 대상 — 첫 MCP 카드 등장(aiId) ~ 첫 답변 토큰 도착 사이에만 진행 표시.
    //   답변 토큰이 시작되면 MCP 실행 단계가 끝난 것이라 null로 클리어(전체 생성 끝까지 유령 스피너 방지).
    const [mcpRunningId, setMcpRunningId] = useState<string | null>(null)
    const [targetChatUid, setTargetChatUid] = useState<string | null>(null)
    const abortControllerRef = useRef<AbortController | null>(null)
    const planActionLockRef = useRef(createPlanActionLock())
    const streamRef = useRef<{ complete: () => void; discard: () => void } | null>(null)
    const scrollContainerRef = useRef<HTMLDivElement>(null)
    const isScrolledUpRef = useRef(false)
    const lastScrollTopRef = useRef(0)
    const scrollInstantRef = useRef(false)
    const currentSessionRef = useRef<string | null>(
        new URLSearchParams(window.location.search).get('session')
    )
    const logAbortRef = useRef<AbortController | null>(null)
    const { user } = useAuth()

    const [isLoadingLog, setIsLoadingLog] = useState<boolean>(() =>
        !!new URLSearchParams(window.location.search).get('session')
    )

    const [, setSearchParams] = useSearchParams()
    const [activeChatId, setActiveChatId] = useState<string | null>(() =>
        new URLSearchParams(window.location.search).get('session')
    )

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
                    const lastIdx = chatlog.length - 1
                    chatlog.forEach((item, idx) => {
                        const question = item.data.question
                        if (isReportArtifact(question)) return
                        const qStr = typeof question === 'string' ? question : ''
                        if (qStr === CANCEL_QUESTION) return
                        const isSilentQ = SILENT_QUESTIONS.has(qStr)
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
                            planSteps: isLastPlan ? extractPlanningSteps(item.data.agentFlowExecutedData) : undefined,
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
        setReportCheckedIds(new Set())
    }
    const [profileMenuOpen, setProfileMenuOpen] = useState(false)
    const [utilMenuOpen, setUtilMenuOpen] = useState(false)
    const [deleteModal, setDeleteModal] = useState(false)
    const [changeNameModal, setChangeNameModal] = useState(false)
    const [viewReportModal, setViewReportModal] = useState(false)
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

    const { showToast } = useToast()

    const isDetail = activeChatId !== null
    const isGenerating = messages.some(m => m.role === 'ai' && m.isGenerating)

    const handleScroll = () => {
        const el = scrollContainerRef.current
        if (!el) return
        const gap = el.scrollHeight - el.scrollTop - el.clientHeight
        setShowScrollBtn(gap > 50)
        if (el.scrollTop < lastScrollTopRef.current - 1) isScrolledUpRef.current = true
        if (gap <= 50) isScrolledUpRef.current = false 
        lastScrollTopRef.current = el.scrollTop
    }

    const scrollToBottom = () => {
        scrollContainerRef.current?.scrollTo({ top: scrollContainerRef.current.scrollHeight, behavior: 'smooth' })
    }

    useEffect(() => {
        if (isLoadingLog) return
        if (!isScrolledUpRef.current) {
            const el = scrollContainerRef.current
            if (scrollInstantRef.current) {
                if (el) el.scrollTop = el.scrollHeight
                scrollInstantRef.current = false
            } else if (isGenerating) {
                // ★ 스트리밍 중엔 즉시 스크롤 — smooth는 빠르게 늘어나는 token을 못 따라가 바닥에서 벌어지고,
                //   그 순간 handleScroll이 "위로 스크롤됨"으로 오판해 자동 스크롤이 멈춰버림.
                if (el) el.scrollTop = el.scrollHeight
            } else {
                scrollToBottom()
            }
        }
    }, [messages, isLoadingLog, isGenerating])

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

    const handleBulkDeleteConfirm = async () => {
        setBulkDeleteModal(false)
        setBulkDeleteResetSignal(s => s + 1)
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
            closePdfViewer()
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
            closePdfViewer()
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
            const res = await apiFetch('/api/v1/rnd/chat/query/stream', {
                method: 'POST',
                headers: { Accept: 'text/event-stream' },
                body: JSON.stringify(chatId ? { chatId, question: content } : { question: content }),
                signal: controller.signal,
            })

            if (!res.ok || !res.body) {
                setMessages(prev => prev.filter(m => m.id !== aiId && m.id !== userId))
                if (isFirstMessage) setActiveChatId(null)
                setAlertMessage(RND_ALERT.query)
                return
            }

            let streamedText = ''
            let flowData: AgentFlowNode[] | undefined
            let lastFlowRaw: string | undefined
            let sourceDocsRaw: unknown[] | undefined
            let liveChatId: string | undefined
            let streamTraceId: string | undefined = res.headers.get('x-genos-trace-id') || undefined
            let terminal: 'end' | 'error' | 'abort' | null = null
            let errorMsg = ''
            let queryResultData: QueryResultData | undefined

            for await (const frame of parseSseStream(res.body)) {
                switch (frame.event) {
                    case 'metadata': {
                        const meta = safeJsonParse<Record<string, unknown>>(frame.data)
                        if (typeof meta?.chatId === 'string') liveChatId = meta.chatId
                        const t = meta?.trace_id ?? meta?.traceId ?? meta?.['x-genos-trace-id']
                        if (typeof t === 'string') { streamTraceId = t; setTraceId(t) }
                        break
                    }
                    case 'token': {
                        // 첫 토큰 = 답변 스트리밍 시작 → MCP 실행 단계 종료. "도구 실행 중" 스피너 클리어.
                        if (streamedText === '') setMcpRunningId(null)
                        streamedText += tokenText(frame.data)
                        const partial = planContentToMarkdown(streamedText)
                        setMessages(prev => prev.map(m => m.id === aiId ? { ...m, planContent: partial } : m))
                        break
                    }
                    case 'nextAgentFlow': {
                        // ★ "도구 실행 중" 스피너 = 실제 MCP 에이전트가 실행 중일 때만.
                        //   agentAgentflow_* 노드 INPROGRESS = 도구 호출 실행 중(곧 카드 생김) → 스피너 ON.
                        //   그 외 노드(파싱/조건/최종답변 등) INPROGRESS → OFF. 마지막 카드 뒤엔 agent 노드가 없어
                        //   스피너가 안 뜸(유령 "다음 카드" 방지). 답변 토큰 시작 후(streamedText)엔 관여 안 함.
                        if (streamedText === '') {
                            const nf = safeJsonParse<{ nodeId?: string; status?: string }>(frame.data)
                            if (nf?.status === 'INPROGRESS' && typeof nf.nodeId === 'string') {
                                setMcpRunningId(nf.nodeId.startsWith('agentAgentflow') ? aiId : null)
                            }
                        }
                        break
                    }
                    case 'agentFlowExecutedData': {
                        // 누적 flow 원본 문자열 보관 — 최종(완전) 프레임을 finalize에서 1회만 파싱.
                        lastFlowRaw = frame.data
                        // ★ 답변 토큰이 시작되기 전(MCP·추론 단계)에만 매 프레임 파싱해 실시간 반영.
                        //   final report가 토큰을 뿜기 시작하면 추론·MCP 노드는 이미 전부 완료된 상태 → 이후 도착하는
                        //   누적 프레임(수 MB, 실측 마지막 프레임 4.28MB)을 매번 JSON.parse+추출하면 메인스레드가 막혀
                        //   토큰 렌더가 "뚝" 멈춘 것처럼 보임. 그래서 토큰 시작 후엔 원본만 보관하고 파싱은 스킵.
                        if (streamedText !== '') break
                        flowData = normalizeFlowData(safeJsonParse<unknown>(frame.data))
                        // agentFlowExecutedData 누적본엔 완료(FINISHED) 노드만 → Visible Reasoner 스텝이 프레임마다 늘어남.
                        //   plan(STOPPED 노드 존재) 상태면 추론 블록 미노출(계획 확인 단계 정책상 제외).
                        const planNow = isPlanResponse({ data: { agentFlowExecutedData: flowData } })
                        // ★ MCP 실행 정보 실시간 — visible_used_tools 노드(paper-rag/web 등) 완료 즉시 카드 반영.
                        const liveMcp = flowData ? extractExecutedMcpDetails(flowData) : []
                        const liveSteps = extractReasoningSteps(flowData)
                        const lastProgress = liveSteps && liveSteps.length ? liveSteps[liveSteps.length - 1].rationale : undefined
                        setMessages(prev => prev.map(m => {
                            if (m.id !== aiId) return m
                            const liveReasoning = (m.allowLiveReasoning && !planNow) ? liveSteps : m.reasoningSteps
                            const progressOverride = (!m.allowLiveReasoning && lastProgress) ? { loadingLabel: lastProgress } : {}
                            return { ...m, reasoningSteps: liveReasoning, mcpDetails: liveMcp.length ? liveMcp : undefined, ...progressOverride }
                        }))
                        // ★ MCP 카드 실시간 — 첫 카드 생기는 즉시 패널 자동 오픈(요청당 1회).
                        //   진행 스피너는 여기서 켜지 않음 — 아래 nextAgentFlow(실제 agent 실행)로만 제어(유령 카드 방지).
                        if (liveMcp.length > 0 && mcpPanelOpenedForRef.current !== aiId) {
                            mcpPanelOpenedForRef.current = aiId
                            setMcpSingleId(aiId)
                            setPanelOpen(true)
                        }
                        break
                    }
                    case 'sourceDocuments': {
                        sourceDocsRaw = normalizeSourceDocsRaw(safeJsonParse<unknown>(frame.data))
                        break
                    }
                    case 'queryResult': {
                        const qr = safeJsonParse<{ data?: QueryResultData }>(frame.data)
                        if (qr?.data && typeof qr.data === 'object') queryResultData = qr.data
                        break
                    }
                    case 'error': { terminal = 'error'; errorMsg = frame.data; break }
                    case 'abort': { terminal = 'abort'; break }
                    case 'end': { terminal = 'end'; break }
                }
            }

            // ★ 스트리밍 중엔 토큰 렌더 보호를 위해 마지막 대형 프레임 파싱을 미뤘음 → 여기서 최종 1회 파싱.
            if (lastFlowRaw) {
                const finalFlow = normalizeFlowData(safeJsonParse<unknown>(lastFlowRaw))
                if (finalFlow?.length) flowData = finalFlow
            }
            if (queryResultData?.agentFlowExecutedData) {
                const qrFlow = normalizeFlowData(queryResultData.agentFlowExecutedData)
                if (qrFlow?.length) flowData = qrFlow
            }
            if (queryResultData?.sourceDocuments != null) {
                sourceDocsRaw = Array.isArray(queryResultData.sourceDocuments) ? queryResultData.sourceDocuments : undefined
            } else if (!sourceDocsRaw && flowData) {
                const sd = extractStreamSourceDocs(flowData)
                if (sd) sourceDocsRaw = sd
            }

            if (terminal === 'error') {
                setMessages(prev => prev.filter(m => m.id !== aiId && m.id !== userId))
                if (isFirstMessage) setActiveChatId(null)
                setAlertMessage(errorMsg.trim() || RND_ALERT.query)
                return
            }

            if (!(flowData?.length) && !streamedText.trim()) {
                setMessages(prev => prev.filter(m => m.id !== aiId && m.id !== userId))
                if (isFirstMessage) setActiveChatId(null)
                setAlertMessage(RND_ALERT.query)
                return
            }

            const receivedChatId = liveChatId ?? chatId ?? undefined
            if (receivedChatId) {
                setChatId(receivedChatId)
                setActiveChatId(receivedChatId)
                setSearchParams({ session: receivedChatId })
                currentSessionRef.current = receivedChatId
            }
            if (isFirstMessage && receivedChatId) {
                prependSession({ uid: receivedChatId, title: content, date: '방금' })
            }

            const mcpDetails = flowData ? extractExecutedMcpDetails(flowData) : undefined
            const text = (typeof queryResultData?.text === 'string' && queryResultData.text.trim())
                ? queryResultData.text
                : (extractStreamFinalText(flowData) ?? (streamedText.trim() ? streamedText : (extractTextFromAgentFlow(flowData) ?? '')))
            const isPlan = isPlanResponse({ data: { agentFlowExecutedData: flowData } })
            const planSteps = isPlan ? extractPlanningSteps(flowData) : undefined
            const sourceDocuments = extractSourceDocuments(sourceDocsRaw)
            // plan은 추론 블록 미노출 — 진행문은 위 live 핸들러가 planContent로 처리. 답변(!isPlan)에만 추론 블록
            const reasoningSteps = isPlan ? undefined : extractReasoningSteps(flowData)

            const resolvedTraceId = streamTraceId ?? extractTraceIdFromFlow(flowData)
            if (resolvedTraceId) setTraceId(resolvedTraceId)
            const newStartNodeId = extractStoppedNodeId(flowData)
            if (newStartNodeId) setStartNodeId(newStartNodeId)

            const aiHeaderLabel = isPlan ? undefined : PROCEED_HEADER
            scrollInstantRef.current = true
            setMessages(prev => prev.map(m => m.id === aiId ? {
                ...m,
                isGenerating: false,
                planContent: planContentToMarkdown(text),
                mcpDetails, isPlan, isPlanMsg: isPlan, planSteps, reasoningSteps, sourceDocuments,
                headerLabel: aiHeaderLabel,
                reasoningAnimate: false,
                traceId: resolvedTraceId,
            } : m))
        } catch (err) {
            const isAbort = err instanceof Error && err.name === 'AbortError'
            if (isAbort) {
                setMessages(prev => prev.filter(m => m.id !== aiId))
            } else {
                setMessages(prev => prev.filter(m => m.id !== aiId && m.id !== userId))
                if (isFirstMessage) setActiveChatId(null)
                setAlertMessage(RND_ALERT.query)
            }
        } finally {
            clearTimeout(timeoutId)
            abortControllerRef.current = null
            setMcpRunningId(null)   // 요청 종료 시 진행 스피너 확실히 종료(토큰 없이 끝난 응답·에러·중단 안전망)
        }
    }

    const handleStop = () => {
        if (streamRef.current) { streamRef.current.complete(); return }
        abortControllerRef.current?.abort()
        if (!chatId || !traceId) return
        apiFetch('/api/v1/rnd/chat/abort', {
            method: 'POST',
            body: JSON.stringify({ chat_id: chatId, trace_id: traceId }),
        }).catch(() => {  })
    }

    const runPlanAbort = async (fromMsgId: string) => {
        setMessages(prev => prev.map(m => m.id === fromMsgId ? { ...m, isPlan: false } : m))
        if (!chatId) return
        setIsCancelling(true)
        try {
            const res = await apiFetch('/api/v1/rnd/chat/query/cancel', {
                method: 'POST',
                body: JSON.stringify({ chatId, startNodeId: startNodeId ?? DEFAULT_START_NODE_ID }),
            })
            const data = await res.json() as unknown
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

    const runPlanAction = async (action: 'proceed' | 'modify', question: string, fromMsgId: string) => {
        if (!chatId) {
            console.warn('[handlePlanAction] chatId is null — skipping', { action })
            showToast('chatId가 없어 요청을 보낼 수 없습니다. 새로고침 후 다시 시도해 주세요.')
            return
        }
        const endpoint: 'reject' | 'proceed' = action === 'proceed' ? 'proceed' : 'reject'
        const aiId = generateId()
        const userMsgId = action === 'modify' ? generateId() : null
        const aiHeaderLabel = action === 'proceed' ? PROCEED_HEADER : undefined
        const alertMsg = action === 'proceed' ? RND_ALERT.proceed : RND_ALERT.reject
        setMessages(prev => [
            ...prev,
            ...(userMsgId ? [{ id: userMsgId, role: 'user' as const, content: question }] : []),
            { id: aiId, role: 'ai', isGenerating: true, planContent: '', headerLabel: aiHeaderLabel, allowLiveReasoning: action === 'proceed' },
        ])

        const body: Record<string, unknown> = { chatId, startNodeId: startNodeId ?? DEFAULT_START_NODE_ID }
        if (action === 'modify') body.question = question

        // 실패/에러 복구 — 빈 AI 말풍선(+ modify면 입력 user 말풍선) 제거 + plan 버튼 복구.
        //   removeUserMsg=false는 명시적 중단(abort) 시 — 사용자가 입력한 modify 텍스트는 보존.
        const restorePlan = (removeUserMsg: boolean) => {
            setMessages(prev => prev
                .filter(m => m.id !== aiId && (!removeUserMsg || userMsgId === null || m.id !== userMsgId))
                .map(m => m.id === fromMsgId ? { ...m, isPlan: true } : m)
            )
        }

        const controller = new AbortController()
        abortControllerRef.current = controller
        const timeoutId = setTimeout(() => controller.abort(), 600_000)

        try {
            // handleSend(질문)과 동일한 SSE 소비 — proceed(실행)/reject(수정)도 스트림으로 처리.
            const res = await apiFetch(`/api/v1/rnd/chat/query/${endpoint}/stream`, {
                method: 'POST',
                headers: { Accept: 'text/event-stream' },
                body: JSON.stringify(body),
                signal: controller.signal,
            })

            if (!res.ok || !res.body) {
                restorePlan(true)
                setAlertMessage(alertMsg)
                return
            }

            let streamedText = ''
            let flowData: AgentFlowNode[] | undefined
            let lastFlowRaw: string | undefined
            let sourceDocsRaw: unknown[] | undefined
            let liveChatId: string | undefined
            let streamTraceId: string | undefined = res.headers.get('x-genos-trace-id') || undefined
            let terminal: 'end' | 'error' | 'abort' | null = null
            let errorMsg = ''
            let queryResultData: QueryResultData | undefined  

            for await (const frame of parseSseStream(res.body)) {
                switch (frame.event) {
                    case 'metadata': {
                        const meta = safeJsonParse<Record<string, unknown>>(frame.data)
                        if (typeof meta?.chatId === 'string') liveChatId = meta.chatId
                        const t = meta?.trace_id ?? meta?.traceId ?? meta?.['x-genos-trace-id']
                        if (typeof t === 'string') { streamTraceId = t; setTraceId(t) }
                        break
                    }
                    case 'token': {
                        // 첫 토큰 = 답변 스트리밍 시작 → MCP 실행 단계 종료. "도구 실행 중" 스피너 클리어.
                        if (streamedText === '') setMcpRunningId(null)
                        streamedText += tokenText(frame.data)
                        const partial = planContentToMarkdown(streamedText)
                        setMessages(prev => prev.map(m => m.id === aiId ? { ...m, planContent: partial } : m))
                        break
                    }
                    case 'nextAgentFlow': {
                        // ★ "도구 실행 중" 스피너 = 실제 MCP 에이전트가 실행 중일 때만.
                        //   agentAgentflow_* 노드 INPROGRESS = 도구 호출 실행 중(곧 카드 생김) → 스피너 ON.
                        //   그 외 노드(파싱/조건/최종답변 등) INPROGRESS → OFF. 마지막 카드 뒤엔 agent 노드가 없어
                        //   스피너가 안 뜸(유령 "다음 카드" 방지). 답변 토큰 시작 후(streamedText)엔 관여 안 함.
                        if (streamedText === '') {
                            const nf = safeJsonParse<{ nodeId?: string; status?: string }>(frame.data)
                            if (nf?.status === 'INPROGRESS' && typeof nf.nodeId === 'string') {
                                setMcpRunningId(nf.nodeId.startsWith('agentAgentflow') ? aiId : null)
                            }
                        }
                        break
                    }
                    case 'agentFlowExecutedData': {
                        // 누적 flow 원본 문자열 보관 — 최종(완전) 프레임을 finalize에서 1회만 파싱.
                        lastFlowRaw = frame.data
                        // ★ 답변 토큰이 시작되기 전(MCP·추론 단계)에만 매 프레임 파싱해 실시간 반영.
                        //   final report가 토큰을 뿜기 시작하면 추론·MCP 노드는 이미 전부 완료된 상태 → 이후 도착하는
                        //   누적 프레임(수 MB, 실측 마지막 프레임 4.28MB)을 매번 JSON.parse+추출하면 메인스레드가 막혀
                        //   토큰 렌더가 "뚝" 멈춘 것처럼 보임. 그래서 토큰 시작 후엔 원본만 보관하고 파싱은 스킵.
                        if (streamedText !== '') break
                        flowData = normalizeFlowData(safeJsonParse<unknown>(frame.data))
                        // agentFlowExecutedData 누적본엔 완료(FINISHED) 노드만 → Visible Reasoner 스텝이 프레임마다 늘어남.
                        //   plan(STOPPED 노드 존재) 상태면 추론 블록 미노출(계획 확인 단계 정책상 제외).
                        const planNow = isPlanResponse({ data: { agentFlowExecutedData: flowData } })
                        // ★ MCP 실행 정보 실시간 — visible_used_tools 노드(paper-rag/web 등) 완료 즉시 카드 반영.
                        const liveMcp = flowData ? extractExecutedMcpDetails(flowData) : []
                        const liveSteps = extractReasoningSteps(flowData)
                        const lastProgress = liveSteps && liveSteps.length ? liveSteps[liveSteps.length - 1].rationale : undefined
                        setMessages(prev => prev.map(m => {
                            if (m.id !== aiId) return m
                            // 추론 과정 실시간 — proceed(실행) turn(allowLiveReasoning)만. query/reject(계획)는 추론 블록 미노출.
                            const liveReasoning = (m.allowLiveReasoning && !planNow) ? liveSteps : m.reasoningSteps
                            // ★ query/reject — plan 오기 전엔 진행문("🔍 요청을 분석하고…")을 loadingLabel로 노출(원형 스피너+문구).
                            const progressOverride = (!m.allowLiveReasoning && lastProgress) ? { loadingLabel: lastProgress } : {}
                            return { ...m, reasoningSteps: liveReasoning, mcpDetails: liveMcp.length ? liveMcp : undefined, ...progressOverride }
                        }))
                        // ★ MCP 카드 실시간 — 첫 카드 생기는 즉시 패널 자동 오픈(요청당 1회).
                        //   진행 스피너는 여기서 켜지 않음 — 아래 nextAgentFlow(실제 agent 실행)로만 제어(유령 카드 방지).
                        if (liveMcp.length > 0 && mcpPanelOpenedForRef.current !== aiId) {
                            mcpPanelOpenedForRef.current = aiId
                            setMcpSingleId(aiId)
                            setPanelOpen(true)
                        }
                        break
                    }
                    case 'sourceDocuments': {
                        sourceDocsRaw = normalizeSourceDocsRaw(safeJsonParse<unknown>(frame.data))
                        break
                    }
                    case 'queryResult': {
                        const qr = safeJsonParse<{ data?: QueryResultData }>(frame.data)
                        if (qr?.data && typeof qr.data === 'object') queryResultData = qr.data
                        break
                    }
                    case 'error': { terminal = 'error'; errorMsg = frame.data; break }
                    case 'abort': { terminal = 'abort'; break }
                    case 'end': { terminal = 'end'; break }
                }
            }

            // ★ 스트리밍 중엔 토큰 렌더 보호를 위해 마지막 대형 프레임 파싱을 미뤘음 → 여기서 최종 1회 파싱.
            if (lastFlowRaw) {
                const finalFlow = normalizeFlowData(safeJsonParse<unknown>(lastFlowRaw))
                if (finalFlow?.length) flowData = finalFlow
            }
            if (queryResultData?.agentFlowExecutedData) {
                const qrFlow = normalizeFlowData(queryResultData.agentFlowExecutedData)
                if (qrFlow?.length) flowData = qrFlow
            }
            if (queryResultData?.sourceDocuments != null) {
                sourceDocsRaw = Array.isArray(queryResultData.sourceDocuments) ? queryResultData.sourceDocuments : undefined
            } else if (!sourceDocsRaw && flowData) {
                const sd = extractStreamSourceDocs(flowData)
                if (sd) sourceDocsRaw = sd
            }

            if (terminal === 'error') {
                restorePlan(true)
                setAlertMessage(errorMsg.trim() || alertMsg)
                return
            }

            if (!(flowData?.length) && !streamedText.trim()) {
                restorePlan(true)
                setAlertMessage(alertMsg)
                return
            }

            // proceed/reject 응답에도 chatId가 갱신될 수 있음 (metadata 이벤트)
            if (liveChatId && liveChatId !== chatId) setChatId(liveChatId)

            const mcpDetails = flowData ? extractExecutedMcpDetails(flowData) : undefined
            const text = (typeof queryResultData?.text === 'string' && queryResultData.text.trim())
                ? queryResultData.text
                : (extractStreamFinalText(flowData) ?? (streamedText.trim() ? streamedText : (extractTextFromAgentFlow(flowData) ?? '')))
            // 응답이 또 plan일 수 있음 (proceed 후 재계획) — agentFlowExecutedData 기반 판정
            const isPlan = isPlanResponse({ data: { agentFlowExecutedData: flowData } })
            const planSteps = isPlan ? extractPlanningSteps(flowData) : undefined
            const sourceDocuments = extractSourceDocuments(sourceDocsRaw)
            const reasoningSteps = isPlan ? undefined : extractReasoningSteps(flowData)

            const resolvedTraceId = streamTraceId ?? extractTraceIdFromFlow(flowData)
            if (resolvedTraceId) setTraceId(resolvedTraceId)
            const newStartNodeId = extractStoppedNodeId(flowData)
            if (newStartNodeId) setStartNodeId(newStartNodeId)

            // 헤더 라벨은 "내용이 계획이냐"로 결정 — proceed로 만들었어도 응답이 또 계획이면 디폴트
            const resolvedHeaderLabel = isPlan ? undefined : PROCEED_HEADER
            scrollInstantRef.current = true
            setMessages(prev => prev.map(m => {
                if (m.id === fromMsgId) return { ...m, isPlan: false }
                if (m.id !== aiId) return m
                return {
                    ...m,
                    isGenerating: false,
                    planContent: planContentToMarkdown(text),
                    mcpDetails, isPlan, isPlanMsg: isPlan, planSteps, reasoningSteps, sourceDocuments,
                    headerLabel: resolvedHeaderLabel,
                    reasoningAnimate: false,
                    traceId: resolvedTraceId,
                }
            }))
        } catch (err) {
            const isAbort = err instanceof Error && err.name === 'AbortError'
            if (isAbort) {
                restorePlan(false)
            } else {
                restorePlan(true)
                setAlertMessage(alertMsg)
            }
        } finally {
            clearTimeout(timeoutId)
            abortControllerRef.current = null
            setMcpRunningId(null)   // 요청 종료 시 진행 스피너 확실히 종료(토큰 없이 끝난 응답·에러·중단 안전망)
        }
    }

    const handlePlanAbort = (fromMsgId: string) =>
        runWithPlanActionLock(planActionLockRef.current, () => runPlanAbort(fromMsgId))

    const handlePlanAction = (action: 'proceed' | 'modify', question: string, fromMsgId: string) =>
        runWithPlanActionLock(planActionLockRef.current, () => runPlanAction(action, question, fromMsgId))

    const handleSelectChat = async (uid: string) => {
        if (currentSessionRef.current === uid) return
        currentSessionRef.current = uid
        logAbortRef.current?.abort()
        const controller = new AbortController()
        logAbortRef.current = controller

        streamRef.current?.discard()
        setReportCheckedIds(new Set())
        setSearchParams({ session: uid })
        setActiveChatId(uid)
        setChatId(uid)
        setMessages([])
        setPanelOpen(false)
        closePdfViewer()
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
                const lastIdx = chatlog.length - 1
                chatlog.forEach((item, idx) => {
                    const question = item.data.question
                    if (isReportArtifact(question)) return
                    const qStr = typeof question === 'string' ? question : ''
                    if (qStr === CANCEL_QUESTION) return
                    const isSilentQ = SILENT_QUESTIONS.has(qStr)
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
                        isPlanMsg: msgIsPlan,
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
            if (err instanceof Error && err.name === 'AbortError') return
            console.error('[chat/log select] 처리 중 오류:', err)
            setAlertMessage(RND_ALERT.log)
        } finally {
            if (logAbortRef.current === controller) {
                logAbortRef.current = null
                setIsLoadingLog(false)
            }
        }
    }

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
        const allCount = applicableReportTraceIds().length
        if (allCount === 0) {
            setAlertMessage('보고서에 적용할\nAI 분석 결과가 없습니다.')
            return
        }
        if (allCount <= REPORT_MAX) {
            setReportAllConfirm(true)
        } else {
            setAlertMessage('선택된 내용이 없습니다.\n보고서에 적용할 내용을 선택해 주세요.')
        }
    }

    const handleConfirmReportAll = () => {
        setReportAllConfirm(false)
        runReport(applicableReportTraceIds())
    }

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
                                                            if (panelOpen && mcpSingleId === msg.id) {
                                                                setPanelOpen(false)
                                                            } else {
                                                                setMcpSingleId(msg.id)
                                                                setPanelOpen(true)
                                                            }
                                                        }}
                                                        isPlan={msg.isPlan ?? false}
                                                        planActionsDisabled={isGenerating}
                                                        onReject={() => handlePlanAbort(msg.id)}
                                                        onProceed={() => handlePlanAction('proceed', '진행', msg.id)}
                                                        onModifyReject={q => handlePlanAction('modify', q, msg.id)}
                                                        headerLabel={msg.headerLabel}
                                                        planSteps={msg.planSteps}
                                                        reasoningSteps={msg.reasoningSteps}
                                                        reasoningAnimate={msg.reasoningAnimate}
                                                        reasoningStreaming={msg.isGenerating}
                                                        sourceDocuments={msg.sourceDocuments}
                                                        onPdfError={msg => setAlertMessage(msg)}
                                                        onPdfView={openPdfViewer}
                                                        canApplyReport={!msg.isPlanMsg && !!msg.traceId && !isFailedAnswer(msg.planContent)}
                                                        reportApplyChecked={reportCheckedIds.has(msg.id)}
                                                        onToggleReportApply={() => handleToggleReportApply(msg.id)}
                                                        loadingLabel={msg.loadingLabel}
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
                        disabled={isCancelling || isLoadingLog}
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
                streaming={mcpRunningId !== null && mcpRunningId === mcpSingleId}
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

            {                                                             }
        </div>
    )
}
