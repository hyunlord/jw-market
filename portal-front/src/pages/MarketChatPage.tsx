import { useState, useEffect, useRef, useCallback, useMemo } from 'react'
import { useSearchParams, useLocation } from 'react-router-dom'
import { apiFetch } from '../utils/apiFetch'
import { useAuth } from '../context/AuthContext'
import Sidebar from '../components/main/Sidebar'
import TopNavigation from '../components/main/TopNavigation'
import ChatMessageUser, { type UserFileChip } from '../components/main/ChatMessageUser'
import ChatMessageAI from '../components/main/ChatMessageAI'
import AnswerInspectionPanel from '../components/main/AnswerInspectionPanel'
import EvidencePopover from '../components/main/EvidencePopover'
import InputArea from '../components/main/InputArea'
import Modals from '../components/main/Modals'
import { useToast } from '../context/ToastContext'
import { useChatSessions } from '../utils/useChatSessions'
import { isMarketOk, MARKET_ALERT, MARKET_CHAT_API, marketStreamFailureAlert } from '../utils/marketChatApi'
import { consumeMarketStream, MARKET_STREAM_CLIENT_TIMEOUT_MS, marketStreamConnectionNotice, marketStreamTerminationNotice } from '../utils/marketStream'
import type { MarketChart } from '../utils/marketStream'
import type { MarketTable } from '../utils/marketTables'
import type { ReportSection } from '../components/main/ReportPreviewModal'
import { answerSectionsHaveContent, answerSectionsToPlainMarkdown, type AnswerSectionState, type EvidenceDisplayCatalog, type EvidenceGroup } from '../utils/answerSections'
import { restoreMarketAnswerSurface } from '../utils/marketChatRestore'
import {
  inspectionDetailFromChatLogData,
  laneExecutionsFromChatLogData,
  type AnswerInspectionDetail,
  type LaneExecutionMap,
} from '../utils/answerInspection'
import {
  selectionPolicyFromChatLogData,
  traceSourceForInspectionLabel,
  traceToolResultsFromChatLogData,
  unnarratedRecordsFromChatLogData,
  type SelectionPolicy,
  type TraceToolResult,
  type UnnarratedRecord,
} from '../utils/traceToolResults'
import { captureFirstTtft } from '../utils/marketTtft'
import { useMarketDocuments } from '../utils/useMarketDocuments'
import { deleteMarketDocument, fetchMarketDocuments, parseDocIdsFromText } from '../utils/marketDocuments'
import { loadSessionLogFirst, sessionHasDocumentReferences } from '../utils/marketSessionLoad'
import type { ReasoningStep } from '../utils/planSignal'
import {
  createEvidencePopoverViewKey,
  emptyEvidencePopoverViewState,
  type EvidencePopoverViewState,
} from '../utils/evidencePopoverViewState'
import {
  createMarketDetailLookup,
  marketDetailContractFromChatLogData,
  type MarketDetailLookup,
} from '../utils/marketDetail'

// 시장분석 전체 채팅 페이지 — R&D MainPage와 구조 동일하나 API·식별자만 Market용.
//   (ChatMessageAI 컴포넌트엔 기능이 남아 있어, 추후 백엔드가 데이터를 주면 prop만 연결하면 됨.)

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
  sections?: AnswerSectionState[]
  isGenerating?: boolean
  headerLabel?: string
  traceId?: string
  inspectionDetail?: AnswerInspectionDetail
  laneExecutions?: LaneExecutionMap
  toolResults?: TraceToolResult[]
  unnarratedRecords?: UnnarratedRecord[]
  selectionPolicy?: SelectionPolicy
  reasoningSteps?: ReasoningStep[]
  reasoningAnimate?: boolean
  reasoningStreaming?: boolean 
  reasoningInitiallyExpanded?: boolean
  ttftMs?: number
  charts?: MarketChart[]
  chartError?: string
  tables?: MarketTable[]
  tableError?: string
  streamNotice?: string
  detailLookup?: MarketDetailLookup
  files?: UserFileChip[]
}

interface ChatLogItem {
  trace_id?: string
  data: {
    question: unknown          // 보통 string (Market은 보고서 산출물 턴이 없음)
    text: string
    chatMessageId?: string
    agentFlowExecutedData?: unknown
    temp_documents?: { temp_document_id?: number; file_name?: string; file_path?: string }[] 
    sourceDocuments?: { metadata?: { file_name?: string } }[] 
    _jw_chat_agent_direct?: boolean 
    _chat_agent_restored?: boolean 
    answer_sections?: unknown
    evidence_catalog?: unknown
    tables?: unknown
    structured_tables?: unknown
    charts?: unknown
    restore_partial?: unknown
    detail_on_demand?: unknown
    genos_persist?: unknown
  }
}

interface ChatLogResponse {
  result: { code: number; data: { chatlog?: ChatLogItem[] } | null }
  status: string
}

// Market 답변엔 plan 개념이 없음 → 헤더는 항상 'AI 분석 결과'.
const MARKET_HEADER = 'AI 분석 결과'

const REPORT_MAX = 10

function todayYYMMDD(): string {
  const d = new Date()
  return `${String(d.getFullYear()).slice(2)}${String(d.getMonth() + 1).padStart(2, '0')}${String(d.getDate()).padStart(2, '0')}`
}

// 보고서 본문 = 세션의 AI 답변 text들을 턴 순서대로 결합 (report API 미사용 — 응답값 그대로).
function composeMarketReport(msgs: Message[]): string {
  return msgs
    .filter(m => m.role === 'ai' && (m.planContent ?? '').trim())
    .map(m => (m.planContent ?? '').trim())
    .join('\n\n---\n\n')
}

function buildDocNameMap(docs: { document_id?: number; file_name?: string }[]): Map<string, number> {
  const map = new Map<string, number>()
  docs.forEach(d => { if (d.file_name && d.document_id != null) map.set(d.file_name, d.document_id) })
  return map
}

function turnFiles(item: ChatLogItem, docIdByName: Map<string, number>, existingNames: Set<string>): UserFileChip[] {
  const names: string[] = []
  const seen = new Set<string>()
  const add = (n?: string) => { if (n && !seen.has(n)) { seen.add(n); names.push(n) } }
  ;(item.data.temp_documents ?? []).forEach(t => add(t.file_name))
  ;(item.data.sourceDocuments ?? []).forEach(s => add(s.metadata?.file_name))
  return names
    .filter(name => existingNames.has(name))
    .map(name => ({ documentId: docIdByName.get(name) ?? 0, fileName: name }))
}

function chatlogToMessages(chatlog: ChatLogItem[], docs: { document_id?: number; file_name?: string }[] = [], conversationId?: string): Message[] {
  const docIdByName = buildDocNameMap(docs)  // §9-B document_id (있으면)
  chatlog.forEach(item => { if (typeof item.data.text === 'string') parseDocIdsFromText(item.data.text, docIdByName) })
  const existingNames = new Set(docs.map(d => d.file_name).filter((n): n is string => !!n))
  const hasRestored = chatlog.some(i => i.data._chat_agent_restored === true)
  const items = hasRestored ? chatlog.filter(i => i.data._jw_chat_agent_direct !== true) : chatlog
  const msgs: Message[] = []
  items.forEach(item => {
    const q = item.data.question
    const qStr = typeof q === 'string' ? q : ''
    const files = turnFiles(item, docIdByName, existingNames)
    if (qStr.trim() || files.length > 0) {
      msgs.push({ id: generateId(), role: 'user', content: qStr, files: files.length ? files : undefined })
    }
    const restored = restoreMarketAnswerSurface(item.data)
    const detailContract = marketDetailContractFromChatLogData(item.data)
    msgs.push({
      id: item.data.chatMessageId ?? generateId(),
      role: 'ai',
      planContent: restored.planContent,
      sections: restored.sections,
      isGenerating: false,
      headerLabel: MARKET_HEADER,
      traceId: item.trace_id,
      inspectionDetail: inspectionDetailFromChatLogData(item.data),
      laneExecutions: laneExecutionsFromChatLogData(item.data),
      toolResults: traceToolResultsFromChatLogData(item.data),
      unnarratedRecords: unnarratedRecordsFromChatLogData(item.data),
      selectionPolicy: selectionPolicyFromChatLogData(item.data),
      reasoningSteps: restored.reasoningSteps,
      reasoningInitiallyExpanded: restored.reasoningInitiallyExpanded,
      tables: restored.tables,
      charts: restored.charts,
      chartError: restored.chartError,
      streamNotice: restored.streamNotice,
      detailLookup: createMarketDetailLookup(detailContract, conversationId, item.trace_id),
    })
  })
  return msgs
}

// Market 챗봇에서 "전체 채팅 화면으로 이동" 시 넘어오는 navigate state (요구사항 6 — 대화 유지 이동)
interface MarketChatNavState {
  chatId?: string
  messages?: { role: 'user' | 'ai'; content: string; reasoningSteps?: ReasoningStep[]; reasoningInitiallyExpanded?: boolean; sections?: AnswerSectionState[]; tables?: MarketTable[]; tableError?: string; charts?: MarketChart[]; chartError?: string; inspectionDetail?: AnswerInspectionDetail; laneExecutions?: LaneExecutionMap; toolResults?: TraceToolResult[]; unnarratedRecords?: UnnarratedRecord[]; selectionPolicy?: SelectionPolicy; streamNotice?: string; detailLookup?: MarketDetailLookup; files?: UserFileChip[] }[]
}

export default function MarketChatPage() {
  const location = useLocation()
  // 챗봇에서 넘어온 대화 상태 — 마운트 시 1회 캡처 (navigate state는 컴포넌트 수명 동안 불변).
  //   ref가 아닌 state로 두어 렌더 중 접근 허용 (react-hooks/refs 룰).
  const [navState] = useState<MarketChatNavState | null>(
    () => (location.state as MarketChatNavState | null) ?? null
  )
  const initialSession =
    navState?.chatId ?? new URLSearchParams(window.location.search).get('session')

  const [messages, setMessages] = useState<Message[]>(() => {
    const m = navState?.messages
    if (!m || m.length === 0) return []
    return m.map(msg =>
      msg.role === 'user'
        ? { id: generateId(), role: 'user' as const, content: msg.content, files: msg.files }
        : { id: generateId(), role: 'ai' as const, planContent: msg.content, isGenerating: false, headerLabel: MARKET_HEADER, sections: msg.sections, reasoningSteps: msg.reasoningSteps, reasoningInitiallyExpanded: msg.reasoningInitiallyExpanded, tables: msg.tables, tableError: msg.tableError, charts: msg.charts, chartError: msg.chartError, inspectionDetail: msg.inspectionDetail, laneExecutions: msg.laneExecutions, toolResults: msg.toolResults, unnarratedRecords: msg.unnarratedRecords, selectionPolicy: msg.selectionPolicy, streamNotice: msg.streamNotice, detailLookup: msg.detailLookup }
    )
  })
  const [alertMessage, setAlertMessage] = useState<string | null>(null)
  const [chatId, setChatId] = useState<string | null>(initialSession)
  const [traceId, setTraceId] = useState<string | null>(null)

  const {
    pinnedList: pinnedChatList,
    normalList: normalChatList,
    prependSession,
    normalHasNext, loadingMore: loadingMoreSessions, loadMore: loadMoreSessions,
    pinChat, unpinChat, renameChat, deleteChat, bulkDelete,
  } = useChatSessions({
    onError: setAlertMessage,
    listEndpoint: MARKET_CHAT_API.session,             // 일반 목록 Market
    pinnedEndpoint: MARKET_CHAT_API.sessionPinned,     // 고정 목록도 Market
    serverDatesUtc: true,                              
    // pin/unpin/rename/delete는 R&D 경로 재활용 (기본값)
  })

  const [targetChatUid, setTargetChatUid] = useState<string | null>(null)
  const abortControllerRef = useRef<AbortController | null>(null)
  const streamRef = useRef<{ complete: () => void; discard: () => void } | null>(null)
  const scrollContainerRef = useRef<HTMLDivElement>(null)
  const isScrolledUpRef = useRef(false)
  const lastScrollTopRef = useRef(0)
  const scrollInstantRef = useRef(false)
  const currentSessionRef = useRef<string | null>(initialSession)
  const sessionIdRef = useRef<string | null>(initialSession)   // app_session_id 확정 추적 (파일 업로드가 첫 질문보다 먼저)
  const logAbortRef = useRef<AbortController | null>(null)
  const { user } = useAuth()
  const { showToast } = useToast()

  const ensureSessionId = useCallback(() => {
    if (sessionIdRef.current) return sessionIdRef.current
    const id = generateId()
    sessionIdRef.current = id
    setChatId(id)
    return id
  }, [])

  const { pendingDocs, uploading, uploadProgress, pickFiles, retryUploadStatus, removePending, clearPending, resetDocs } =
    useMarketDocuments({
      ensureSessionId,
      hasSessionId: () => sessionIdRef.current !== null,
      onAlert: setAlertMessage,
    })

  // 챗봇에서 대화를 들고 넘어온 경우엔 log 재호출 없이 그대로 노출 → 스피너 X
  const [isLoadingLog, setIsLoadingLog] = useState<boolean>(
    () => !!initialSession && !(navState?.messages && navState.messages.length > 0)
  )

  const [, setSearchParams] = useSearchParams()
  const [activeChatId, setActiveChatId] = useState<string | null>(initialSession)

  const [sidebarOpen, setSidebarOpen] = useState(() => localStorage.getItem('sidebarOpen') === 'true')
  const [profileMenuOpen, setProfileMenuOpen] = useState(false)
  const [utilMenuOpen, setUtilMenuOpen] = useState(false)
  const [deleteModal, setDeleteModal] = useState(false)
  const [changeNameModal, setChangeNameModal] = useState(false)
  const [bulkDeleteModal, setBulkDeleteModal] = useState(false)
  const [pendingBulkUids, setPendingBulkUids] = useState<string[]>([])
  const [bulkDeleteResetSignal, setBulkDeleteResetSignal] = useState(0)
  const [showScrollBtn, setShowScrollBtn] = useState(false)
  const [reportOpen, setReportOpen] = useState(false)
  const [reportMarkdown, setReportMarkdown] = useState('')
  const [reportSections, setReportSections] = useState<ReportSection[]>([])
  const [reportDocTitle, setReportDocTitle] = useState('')
  const [reportFilename, setReportFilename] = useState('')
  const [reportCheckedIds, setReportCheckedIds] = useState<Set<string>>(new Set())
  const [reportAllConfirm, setReportAllConfirm] = useState(false)   // 선택 0개 시 "전체 적용?" 확인 모달
  const sourceJumpSequenceRef = useRef(0)
  const [inspectedAnswer, setInspectedAnswer] = useState<{ id: string; question: string; laneKey?: string; evidenceId?: string; focusRequestId?: number } | null>(null)
  const [evidencePopover, setEvidencePopover] = useState<{ evidenceId: string; evidence: readonly { evidenceId: string; label: string }[]; catalog?: EvidenceDisplayCatalog; group?: EvidenceGroup; detailLookup?: MarketDetailLookup } | null>(null)
  const [evidencePopoverViewByKey, setEvidencePopoverViewByKey] = useState<Record<string, EvidencePopoverViewState>>({})

  const handleToggleReportApply = (msgId: string) => {
    setReportCheckedIds(prev => {
      const next = new Set(prev)
      if (next.has(msgId)) { next.delete(msgId); return next }
      if (next.size >= REPORT_MAX) {
        setAlertMessage(`보고서 생성 시 적용 가능한 개수는 최대 ${REPORT_MAX}개이며,\n초과된 내용은 포함할 수 없습니다.`)
        return prev
      }
      next.add(msgId)
      return next
    })
  }

  const isDetail = activeChatId !== null

  const docIdByName = useMemo(() => {
    const m = new Map<string, number>()
    messages.forEach(msg => msg.files?.forEach(f => { if (f.documentId) m.set(f.fileName, f.documentId) }))
    return m
  }, [messages])
  // 복원 로그에 첨부 흔적이 있을 때만 서버 목록을 후행 조회해 문서 ID를 보정한다.
  const [serverHasDocs, setServerHasDocs] = useState(false)
  const [lastChatIdForDocs, setLastChatIdForDocs] = useState(chatId)
  if (chatId !== lastChatIdForDocs) {
    setLastChatIdForDocs(chatId)
    setServerHasDocs(false)
  }

  const hasSessionFiles = serverHasDocs || messages.some(m => (m.files?.length ?? 0) > 0) || pendingDocs.length > 0
  const isGenerating = messages.some(m => m.role === 'ai' && m.isGenerating)

  // 챗봇에서 대화를 들고 넘어왔으면 URL을 세션으로 맞추고 히스토리에 목록 보장 (첫 프롬프트 전송된 세션)
  useEffect(() => {
    const nav = navState
    if (nav?.chatId) {
      setSearchParams({ session: nav.chatId })
      const firstUser = nav.messages?.find(m => m.role === 'user')?.content
      if (firstUser) prependSession({ uid: nav.chatId, title: firstUser, date: '방금' })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // 새로고침/직접 진입 시 URL ?session= 복원 (챗봇 이동 케이스는 위 effect가 처리하므로 제외)
  useEffect(() => {
    if (navState?.chatId) return
    const sessionId = new URLSearchParams(window.location.search).get('session')
    if (!sessionId) return
    const controller = new AbortController()
    logAbortRef.current = controller
    loadSessionLogFirst<ChatLogResponse, { document_id?: number; file_name?: string }>({
      loadLog: () => apiFetch(MARKET_CHAT_API.log, { method: 'POST', body: JSON.stringify({ chat_session_id: sessionId }), signal: controller.signal }).then(r => r.json()),
      loadDocuments: () => fetchMarketDocuments(sessionId),
      shouldLoadDocuments: data => isMarketOk(data) && sessionHasDocumentReferences(data.result.data?.chatlog ?? []),
      publish: (data, docs) => {
        if (controller.signal.aborted) return
        if (isMarketOk(data)) {
          setServerHasDocs(docs.length > 0)
          const msgs = chatlogToMessages(data.result.data?.chatlog ?? [], docs, initialSession ?? undefined)
          if (msgs.length > 0) { scrollInstantRef.current = true; setMessages(msgs); return }
        } else {
          setAlertMessage(MARKET_ALERT.log)
        }
        setSearchParams({}); setActiveChatId(null); setChatId(null); sessionIdRef.current = null
      },
    })
      .catch((err: unknown) => {
        if (err instanceof Error && err.name === 'AbortError') return
        setAlertMessage(MARKET_ALERT.log)
        setSearchParams({}); setActiveChatId(null); setChatId(null); sessionIdRef.current = null
      })
      .finally(() => {
        if (logAbortRef.current === controller) { logAbortRef.current = null; setIsLoadingLog(false) }
      })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

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
        if (el) el.scrollTop = el.scrollHeight
      } else {
        scrollToBottom()
      }
    }
  }, [messages, isLoadingLog, isGenerating])

  const leaveSession = () => {
    logAbortRef.current?.abort()
    logAbortRef.current = null
    currentSessionRef.current = null
    setReportCheckedIds(new Set())
    setInspectedAnswer(null)
  }

  const resetToNewChat = () => {
    streamRef.current?.discard()
    leaveSession()
    setSearchParams({})
    setActiveChatId(null)
    setMessages([])
    setChatId(null)
    sessionIdRef.current = null
    resetDocs()
    setShowScrollBtn(false)
    isScrolledUpRef.current = false
    setIsLoadingLog(false)
    setInspectedAnswer(null)
  }

  const questionForAnswer = (answerIndex: number): string => {
    for (let index = answerIndex - 1; index >= 0; index -= 1) {
      const candidate = messages[index]
      if (candidate.role === 'user' && candidate.content?.trim()) return candidate.content.trim()
    }
    return '질문 내용을 확인할 수 없습니다'
  }

  // Alt+N — 새 채팅
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.altKey && e.key === 'n') {
        e.preventDefault()
        if (!isGenerating) resetToNewChat()
      }
    }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isGenerating])

  const closeAllMenus = () => { setProfileMenuOpen(false); setUtilMenuOpen(false) }
  const handleMenuToggle = (menu: 'profile' | 'util') => {
    const current = menu === 'profile' ? profileMenuOpen : utilMenuOpen
    closeAllMenus()
    if (!current) { if (menu === 'profile') setProfileMenuOpen(true); else setUtilMenuOpen(true) }
  }

  const handleSend = async (content: string) => {
    const files = pendingDocs
    clearPending()
    // ★ 파일 업로드로 이미 확정됐을 수 있는 app_session_id 재사용 (없으면 생성)
    //   스트림은 conversationId로 받지만 세션 목록·복원(/log)·파일업로드(app_session_id)·abort와
    //   식별자를 하나로 통일하기 위해 이 UUID를 그대로 conversationId로 전달.
    const effectiveChatId = ensureSessionId()
    const isFirstSend = activeChatId === null   // 화면상 첫 전송 = 사이드바 등록 대상

    if (isFirstSend) {
      setActiveChatId(effectiveChatId)
      setSearchParams({ session: effectiveChatId })
      currentSessionRef.current = effectiveChatId
      // 첫 질문 → 히스토리 목록 맨 앞에 추가 (세션이 백엔드에 생성됨)
      prependSession({ uid: effectiveChatId, title: content, date: '방금' })
    }

    const userId = generateId()
    const aiId = generateId()
    setMessages(prev => [
      ...prev,
      { id: userId, role: 'user', content, files: files.map(f => ({ documentId: f.documentId, fileName: f.fileName })) },
      { id: aiId, role: 'ai', isGenerating: true, planContent: '', headerLabel: MARKET_HEADER },
    ])

    const controller = new AbortController()
    abortControllerRef.current = controller
    const timeoutId = setTimeout(() => controller.abort(), MARKET_STREAM_CLIENT_TIMEOUT_MS)
    let latestText = ''

    try {
      const requestStartedAtMs = performance.now()
      const res = await apiFetch(MARKET_CHAT_API.queryStream, {
        method: 'POST',
        headers: { Accept: 'text/event-stream' },
        body: JSON.stringify({ question: content, conversationId: effectiveChatId }),
        signal: controller.signal,
      })

      if (!res.ok || !res.body) {
        setMessages(prev => prev.map(m => m.id === aiId ? {
          ...m, isGenerating: false, reasoningStreaming: false,
          streamNotice: marketStreamConnectionNotice(false),
        } : m))
        setAlertMessage(marketStreamFailureAlert(res))
        return
      }

      const result = await consumeMarketStream(res, {
        onAnswer: full => {
          latestText = full
          const answerReceivedAtMs = performance.now()
          setMessages(prev => prev.map(m => {
            if (m.id !== aiId) return m
            if (!full.trim()) return { ...m, planContent: full }
            return {
              ...m,
              planContent: full,
              ttftMs: captureFirstTtft(m.ttftMs, requestStartedAtMs, answerReceivedAtMs),
            }
          }))
        },
        onSections: sections => {
          latestText = answerSectionsToPlainMarkdown(sections)
          const answerReceivedAtMs = performance.now()
          setMessages(prev => prev.map(m => m.id === aiId ? {
            ...m,
            sections,
            planContent: latestText,
            ttftMs: answerSectionsHaveContent(sections)
              ? captureFirstTtft(m.ttftMs, requestStartedAtMs, answerReceivedAtMs)
              : m.ttftMs,
          } : m))
        },
        onSteps: steps => setMessages(prev => prev.map(m => m.id === aiId ? { ...m, reasoningSteps: steps, reasoningStreaming: true } : m)),
        onTables: tables => setMessages(prev => prev.map(m => m.id === aiId ? { ...m, tables } : m)),
        onTableError: tableError => setMessages(prev => prev.map(m => m.id === aiId ? { ...m, tableError } : m)),
        onCharts: charts => setMessages(prev => prev.map(m => m.id === aiId ? { ...m, charts } : m)),
        onChartError: chartError => setMessages(prev => prev.map(m => m.id === aiId ? { ...m, chartError } : m)),
      })

      const text = result.sections === undefined ? result.text : answerSectionsToPlainMarkdown(result.sections)
      // 첨부 말풍선 document_id 보강 = 응답 text 출처 테이블 파싱 (§9 라이브 첨부)
      if (files.length > 0) {
        const idByName = parseDocIdsFromText(text)
        if (idByName.size > 0) {
          setMessages(prev => prev.map(m =>
            m.id === userId && m.files
              ? { ...m, files: m.files.map(f => ({ ...f, documentId: idByName.get(f.fileName) ?? f.documentId })) }
              : m
          ))
        }
      }
      const newTraceId = result.traceId
      if (newTraceId) setTraceId(newTraceId)
      // 완료 시 추론 접힘으로 높이 급변 → 레이아웃 확정 후 즉시 바닥 고정(R&D StreamPage와 동일 처리)
      scrollInstantRef.current = true
      // step 이벤트로 만든 추론 과정 확정 반영 + streaming 종료(전부 완료 표시)
      setMessages(prev => prev.map(m =>
        m.id === aiId
          ? {
              ...m, isGenerating: false, planContent: text, sections: result.sections, traceId: newTraceId ?? undefined,
              reasoningSteps: result.steps.length ? result.steps : undefined,
              reasoningStreaming: false,
              inspectionDetail: result.inspectionDetail,
              laneExecutions: result.laneExecutions,
              toolResults: result.toolResults,
              unnarratedRecords: result.unnarratedRecords,
              selectionPolicy: result.selectionPolicy,
              detailLookup: createMarketDetailLookup(result.detailContract, result.conversationId ?? effectiveChatId, newTraceId),
              tables: result.tables,
              tableError: result.tableError,
              charts: result.charts,
              chartError: result.chartError,
              streamNotice: marketStreamTerminationNotice(result),
            }
          : m
      ))
    } catch (err) {
      setMessages(prev => prev.map(m => m.id === aiId ? {
        ...m, isGenerating: false, reasoningStreaming: false,
        streamNotice: marketStreamConnectionNotice(latestText.trim().length > 0),
      } : m))
      if (!(err instanceof Error && err.name === 'AbortError')) setAlertMessage(MARKET_ALERT.query)
    } finally {
      clearTimeout(timeoutId)
      abortControllerRef.current = null
    }
  }

  const handleStop = () => {
    if (streamRef.current) { streamRef.current.complete(); return }
    abortControllerRef.current?.abort()
    if (!chatId || !traceId) return
    apiFetch(MARKET_CHAT_API.abort, {
      method: 'POST',
      body: JSON.stringify({ chat_id: chatId, trace_id: traceId }),   // snake_case
    }).catch(() => { /* abort 실패 silent */ })
  }

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
    sessionIdRef.current = uid
    resetDocs()
    setMessages([])
    setShowScrollBtn(false)
    isScrolledUpRef.current = false
    setIsLoadingLog(true)
    try {
      await loadSessionLogFirst<ChatLogResponse, { document_id?: number; file_name?: string }>({
        loadLog: () => apiFetch(MARKET_CHAT_API.log, { method: 'POST', body: JSON.stringify({ chat_session_id: uid }), signal: controller.signal }).then(res => res.json()),
        loadDocuments: () => fetchMarketDocuments(uid),
        shouldLoadDocuments: data => isMarketOk(data) && sessionHasDocumentReferences(data.result.data?.chatlog ?? []),
        publish: (data, docs) => {
          if (controller.signal.aborted || currentSessionRef.current !== uid) return
          if (isMarketOk(data)) {
            setServerHasDocs(docs.length > 0)
            scrollInstantRef.current = true
            setMessages(chatlogToMessages(data.result.data?.chatlog ?? [], docs, uid))
          } else {
            setAlertMessage(MARKET_ALERT.log)
          }
        },
      })
    } catch (err) {
      if (err instanceof Error && err.name === 'AbortError') return
      setAlertMessage(MARKET_ALERT.log)
    } finally {
      if (logAbortRef.current === controller) { logAbortRef.current = null; setIsLoadingLog(false) }
    }
  }

  // ===== 세션 관리 (R&D 훅 재활용 — pin/rename/delete는 /rnd 경로가 Market uid로 동작) =====
  const handleBulkDeleteRequest = (uids: string[]) => { setPendingBulkUids(uids); setBulkDeleteModal(true) }

  const handleBulkDeleteConfirm = async () => {
    setBulkDeleteModal(false)
    setBulkDeleteResetSignal(s => s + 1)
    const uids = pendingBulkUids
    setPendingBulkUids([])
    const ok = await bulkDelete(uids)
    if (ok && activeChatId && uids.includes(activeChatId)) resetToNewChat()
  }

  const handleDeleteConfirm = async () => {
    const uid = targetChatUid
    if (!uid) { setDeleteModal(false); return }
    const ok = await deleteChat(uid)
    if (ok && activeChatId === uid) resetToNewChat()
    setDeleteModal(false)
    setTargetChatUid(null)
  }

  const handleRenameConfirm = async (newTitle: string) => {
    const uid = targetChatUid
    if (uid) await renameChat(uid, newTitle)
    setChangeNameModal(false)
    setTargetChatUid(null)
  }

  const removeSentFile = (messageId: string, documentId: number) => {
    const sid = sessionIdRef.current
    if (sid) void deleteMarketDocument(sid, documentId)
    setMessages(prev => prev.map(m =>
      m.id === messageId ? { ...m, files: (m.files ?? []).filter(f => f.documentId !== documentId) } : m
    ))
  }

  const fileUploadProps = {
    docs: pendingDocs,
    uploadProgress,
    uploading,
    onPickFiles: pickFiles,
    onRetryUploadStatus: retryUploadStatus,
    onRemovePending: removePending,
  }

  const openReport = (target: Message[]) => {
    const md = composeMarketReport(target)
    if (!md.trim()) { setAlertMessage('보고서로 만들 내용이 없습니다.'); return }
    const title = [...pinnedChatList, ...normalChatList].find(c => c.uid === activeChatId)?.title ?? '시장분석 보고서'
    setReportDocTitle(title)
    setReportMarkdown(md)
    setReportSections(target.filter(message => message.role === 'ai').map((message, index) => ({
      id: message.id,
      title: `AI 분석 결과 ${index + 1}`,
      text: (message.planContent ?? '').trim(),
      tables: message.tables ?? [],
      charts: message.charts ?? [],
    })))
    setReportFilename(`${title}_${todayYYMMDD()}`)
    setReportOpen(true)
  }

  const handleDownloadReport = () => {
    const answers = messages.filter(m => m.role === 'ai' && (m.planContent ?? '').trim())
    const selected = answers.filter(m => reportCheckedIds.has(m.id))
    if (selected.length > 0) { openReport(selected); return }
    if (answers.length === 0) { setAlertMessage('보고서에 적용할\nAI 분석 결과가 없습니다.'); return }
    if (answers.length <= REPORT_MAX) {
      setReportAllConfirm(true)  
    } else {
      setAlertMessage('선택된 내용이 없습니다.\n보고서에 적용할 내용을 선택해 주세요.')
    }
  }

  const handleConfirmReportAll = () => {
    setReportAllConfirm(false)
    openReport(messages.filter(m => m.role === 'ai' && (m.planContent ?? '').trim()))
  }

  return (
    <div className={`wrap ${sidebarOpen ? 'open' : 'close'}`}>
      <Sidebar
        pinnedList={pinnedChatList}
        normalList={normalChatList}
        activeChatId={activeChatId}
        onToggleSidebar={() => setSidebarOpen(p => { localStorage.setItem('sidebarOpen', String(!p)); return !p })}
        onNewChat={resetToNewChat}
        onSelectChat={handleSelectChat}
        onDeleteModal={uid => { setTargetChatUid(uid); setDeleteModal(true) }}
        onChangeNameModal={uid => { setTargetChatUid(uid); setChangeNameModal(true) }}
        onPinChat={pinChat}
        onUnpinChat={unpinChat}
        onBulkDeleteRequest={handleBulkDeleteRequest}
        resetSelectionSignal={bulkDeleteResetSignal}
        hasMore={normalHasNext}
        loadingMore={loadingMoreSessions}
        onLoadMore={loadMoreSessions}
      />

      <div className={`container-wrap${isDetail ? ' detail' : ''}`}>
        <TopNavigation
          isDetail={isDetail}
          section="market"
          navLeftLabel="시장분석"
          showReportButton={true}
          onDownloadReport={handleDownloadReport}
          showAttachments={hasSessionFiles}
          attachAppSessionId={chatId}
          attachDocIdByName={docIdByName}
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
          onAlertMessage={setAlertMessage}
        />

        <div className={`work-split${inspectedAnswer ? ' inspection-open' : ''}`}>
          <div className="chat-col">
        <div ref={scrollContainerRef} className={`content-wrap${isDetail ? ' scroll-container' : ''}`} onScroll={handleScroll}>
          <div className="content">
            <div className="content-inner">
              {!isDetail ? (
                <>
                  <div className="welcome-msg">
                    <div className="text-wrap01">안녕하세요 {user?.userName ?? ''}님,</div>
                    <div className="text-wrap02">시장분석 데이터에 대해 무엇이든 질문해 주세요.</div>
                  </div>
                  <InputArea isGenerating={false} disabled={false} onSend={handleSend} onStop={handleStop} focusKey="welcome" fileUpload={fileUploadProps} />
                </>
              ) : isLoadingLog ? (
                <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', minHeight: 'calc(100vh - 220px)' }}>
                  <div className="fixed-8bar-spinner" style={{ transform: 'scale(2)' }}>
                    {Array.from({ length: 8 }, (_, i) => (<div key={i} className={`bar bar${i + 1}`} />))}
                  </div>
                </div>
              ) : (
                messages.map((msg, index) =>
                  msg.role === 'user'
                    ? <ChatMessageUser key={msg.id} content={msg.content ?? ''} onRemoveFile={docId => removeSentFile(msg.id, docId)} onCopy={() => showToast('복사되었습니다.')} />
                    : <ChatMessageAI
                        key={msg.id}
                        id={msg.id}
                        planContent={msg.planContent ?? ''}
                        sections={msg.sections}
                        isGenerating={msg.isGenerating ?? false}
                        headerLabel={msg.headerLabel}
                        reasoningSteps={msg.reasoningSteps}
                        reasoningAnimate={msg.reasoningAnimate}
                        reasoningStreaming={msg.reasoningStreaming}
                        reasoningInitiallyExpanded={msg.reasoningInitiallyExpanded}
                        ttftMs={msg.ttftMs}
                        charts={msg.charts}
                        chartError={msg.chartError}
                        tables={msg.tables}
                        tableError={msg.tableError}
                        streamNotice={msg.streamNotice}
                        selectionPolicy={msg.selectionPolicy}
                        canApplyReport={(msg.planContent ?? '').trim().length > 0}
                        reportApplyChecked={reportCheckedIds.has(msg.id)}
                        onToggleReportApply={() => handleToggleReportApply(msg.id)}
                        onInspectionOpen={() => setInspectedAnswer({ id: msg.id, question: questionForAnswer(index) })}
                        onInspectionSourceOpen={sourceLabel => {
                          sourceJumpSequenceRef.current += 1
                          setInspectedAnswer({
                            id: msg.id,
                            question: questionForAnswer(index),
                            laneKey: traceSourceForInspectionLabel(sourceLabel),
                            focusRequestId: sourceJumpSequenceRef.current,
                          })
                        }}
                        onEvidenceOpen={(evidenceId, evidence, catalog, group) => {
                          setEvidencePopover({ evidenceId, evidence, catalog, group, detailLookup: msg.detailLookup })
                        }}
                        inspectionOpen={inspectedAnswer?.id === msg.id}
                      />
                )
              )}
            </div>
          </div>
        </div>
          </div>
          <AnswerInspectionPanel
            open={inspectedAnswer !== null}
            answerLabel={inspectedAnswer?.question ?? ''}
            detail={inspectedAnswer ? messages.find(message => message.id === inspectedAnswer.id)?.inspectionDetail : undefined}
            laneExecutions={inspectedAnswer ? messages.find(message => message.id === inspectedAnswer.id)?.laneExecutions : undefined}
            toolResults={inspectedAnswer ? messages.find(message => message.id === inspectedAnswer.id)?.toolResults : undefined}
            unnarratedRecords={inspectedAnswer ? messages.find(message => message.id === inspectedAnswer.id)?.unnarratedRecords : undefined}
            focusLaneKey={inspectedAnswer?.laneKey}
            focusEvidenceId={inspectedAnswer?.evidenceId}
            focusRequestId={inspectedAnswer?.focusRequestId}
            detailLookup={inspectedAnswer ? messages.find(message => message.id === inspectedAnswer.id)?.detailLookup : undefined}
            onClose={() => setInspectedAnswer(null)}
          />
          {evidencePopover && (() => {
            const viewKey = createEvidencePopoverViewKey({
              conversationId: evidencePopover.detailLookup?.conversationId ?? activeChatId ?? 'legacy',
              responseId: evidencePopover.detailLookup?.responseId ?? 'legacy',
              itemKey: evidencePopover.evidenceId,
            })
            return <EvidencePopover
            key={evidencePopover.evidenceId}
            evidenceId={evidencePopover.evidenceId}
            evidence={evidencePopover.evidence}
            catalog={evidencePopover.catalog}
            group={evidencePopover.group}
            detailLookup={evidencePopover.detailLookup}
            viewState={evidencePopoverViewByKey[viewKey] ?? emptyEvidencePopoverViewState()}
            onViewStateChange={state => setEvidencePopoverViewByKey(current => ({ ...current, [viewKey]: state }))}
            onClose={() => setEvidencePopover(null)}
            onSelectEvidence={evidenceId => setEvidencePopover(current => current ? { ...current, evidenceId } : current)}
          />
          })()}
        </div>

        {isDetail && (
          <InputArea isGenerating={isGenerating} disabled={isLoadingLog} onSend={handleSend} onStop={handleStop} focusKey={activeChatId} fileUpload={fileUploadProps} />
        )}
      </div>

      <div className={`bottom-floating-wrap${isDetail && showScrollBtn ? ' active' : ''}`}>
        <div className="btn-bottom-floating" onClick={scrollToBottom} />
      </div>

      <Modals
        deleteModal={deleteModal}
        changeNameModal={changeNameModal}
        bulkDeleteModal={bulkDeleteModal}
        chatTitle={targetChatUid ? ([...pinnedChatList, ...normalChatList].find(c => c.uid === targetChatUid)?.title ?? '') : ''}
        onCloseDelete={() => { setDeleteModal(false); setTargetChatUid(null) }}
        onConfirmDelete={handleDeleteConfirm}
        onCloseChangeName={() => { setChangeNameModal(false); setTargetChatUid(null) }}
        onConfirmChangeName={handleRenameConfirm}
        onCloseBulkDelete={() => { setBulkDeleteModal(false); setPendingBulkUids([]) }}
        onConfirmBulkDelete={handleBulkDeleteConfirm}
        alertMessage={alertMessage}
        onCloseAlert={() => setAlertMessage(null)}
        reportPreviewOpen={reportOpen}
        reportLoading={false}
        reportMarkdown={reportMarkdown}
        reportTitle={reportDocTitle}
        reportSections={reportSections}
        reportFilename={reportFilename}
        onCloseReportPreview={() => setReportOpen(false)}
        onReportError={setAlertMessage}
        reportAllConfirmModal={reportAllConfirm}
        onCloseReportAllConfirm={() => setReportAllConfirm(false)}
        onConfirmReportAll={handleConfirmReportAll}
      />
    </div>
  )
}
