// 시장분석 Agent 챗봇 — 원인분석·심층분석 공용
// Provider(채팅 상태 + API) + Trigger(버튼) + Panel(우측 슬라이드)을 한 모듈에.
// ★ Provider는 페이지별로 감싸므로 페이지 이탈 시 언마운트 = 챗봇 자동 닫힘
//   대화 상태도 페이지 인스턴스 단위 (원인분석↔심층분석 이동 시 초기화 — 스펙상 닫힘이 정상).
import { createContext, useContext, useRef, useState, useEffect, useCallback, useMemo } from 'react'
import type { ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import { apiFetch } from '../../utils/apiFetch'
import { MARKET_ALERT, MARKET_CHAT_API, marketStreamFailureAlert } from '../../utils/marketChatApi'
import { consumeMarketStream, MARKET_STREAM_CLIENT_TIMEOUT_MS, marketStreamConnectionNotice, marketStreamTerminationNotice } from '../../utils/marketStream'
import type { MarketChart } from '../../utils/marketStream'
import { answerSectionsToPlainMarkdown, type AnswerSectionState } from '../../utils/answerSections'
import type { MarketTable } from '../../utils/marketTables'
import type { AnswerInspectionDetail, LaneExecutionMap } from '../../utils/answerInspection'
import type { SelectionPolicy, TraceToolResult, UnnarratedRecord } from '../../utils/traceToolResults'
import { captureFirstTtft } from '../../utils/marketTtft'
import { useMarketDocuments, type PendingDoc } from '../../utils/useMarketDocuments'
import { deleteMarketDocument, parseDocIdsFromText, fetchMarketDocuments } from '../../utils/marketDocuments'
import type { ReasoningStep } from '../../utils/planSignal'
import ChatMessageUser, { type UserFileChip } from './ChatMessageUser'
import ChatMessageAI from './ChatMessageAI'
import FilePreviewList from './FilePreviewList'
import type { UploadProgress } from '../../utils/uploadProgress.ts'
import AttachmentListPopup from './AttachmentListPopup'
import { createMarketDetailLookup, type MarketDetailLookup } from '../../utils/marketDetail.ts'

function generateId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') return crypto.randomUUID()
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => {
    const r = Math.random() * 16 | 0
    return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16)
  })
}

const MARKET_HEADER = 'AI 분석 결과'

interface AgentMessage {
  id: string
  role: 'user' | 'ai'
  content?: string
  planContent?: string
  isGenerating?: boolean
  headerLabel?: string
  reasoningSteps?: ReasoningStep[]
  reasoningAnimate?: boolean
  reasoningStreaming?: boolean   // 스트리밍 중 = 마지막 스텝 스피너
  ttftMs?: number
  sections?: AnswerSectionState[]
  tables?: MarketTable[]
  tableError?: string
  charts?: MarketChart[]
  inspectionDetail?: AnswerInspectionDetail
  laneExecutions?: LaneExecutionMap
  chartError?: string
  toolResults?: TraceToolResult[]
  unnarratedRecords?: UnnarratedRecord[]
  selectionPolicy?: SelectionPolicy
  streamNotice?: string
  detailLookup?: MarketDetailLookup
  files?: UserFileChip[]   // user 메시지 첨부 파일 말풍선 (v1.75 3-5)
}

// "전체 채팅 화면으로 이동" 시 넘길 대화 스냅샷 (요구사항 6)
export interface AgentChatSnapshot {
  chatId: string | null
  messages: { role: 'user' | 'ai'; content: string; reasoningSteps?: ReasoningStep[]; reasoningInitiallyExpanded?: boolean; sections?: AnswerSectionState[]; tables?: MarketTable[]; tableError?: string; charts?: MarketChart[]; chartError?: string; inspectionDetail?: AnswerInspectionDetail; laneExecutions?: LaneExecutionMap; toolResults?: TraceToolResult[]; unnarratedRecords?: UnnarratedRecord[]; selectionPolicy?: SelectionPolicy; streamNotice?: string; detailLookup?: MarketDetailLookup; files?: UserFileChip[] }[]
}

interface AgentChatContextValue {
  open: boolean
  openChat: () => void
  closeChat: () => void
  messages: AgentMessage[]
  isGenerating: boolean
  chatId: string | null
  sendMessage: (content: string, files?: PendingDoc[]) => void
  stop: () => void
  resetChat: () => void
  onAlert?: (msg: string) => void
  snapshot: () => AgentChatSnapshot
  pendingDocs: PendingDoc[]
  uploading: boolean
  uploadProgress: UploadProgress | null
  pickFiles: (files: FileList | File[]) => void
  retryUploadStatus: () => void
  removePending: (documentId: number) => void
  clearPending: () => void
  removeSentFile: (messageId: string, documentId: number) => void
}

const AgentChatContext = createContext<AgentChatContextValue | null>(null)

// 모듈 내부 전용 훅 (Fast Refresh only-export-components 룰 회피 위해 export 안 함)
function useAgentChat(): AgentChatContextValue {
  const ctx = useContext(AgentChatContext)
  if (!ctx) throw new Error('AgentChat 컴포넌트는 <AgentChatProvider> 안에서만 사용하세요')
  return ctx
}

export function AgentChatProvider({ children, onAlert }: { children: ReactNode; onAlert?: (msg: string) => void }) {
  const [open, setOpen] = useState(false)
  const [messages, setMessages] = useState<AgentMessage[]>([])
  const [chatId, setChatId] = useState<string | null>(null)
  const sessionIdRef = useRef<string | null>(null) 
  const abortControllerRef = useRef<AbortController | null>(null)
  const streamRef = useRef<{ complete: () => void; discard: () => void } | null>(null)

  const isGenerating = messages.some(m => m.role === 'ai' && m.isGenerating)

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
      onAlert: onAlert ?? (() => {}),
    })

  const sendMessage = async (content: string, files: PendingDoc[] = []) => {
    // 스트림은 conversationId로 받지만 파일업로드(app_session_id)·복원(/log)과 식별자 통일 위해 이 UUID 재사용.
    const effectiveChatId = ensureSessionId()

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
        onAlert?.(marketStreamFailureAlert(res))
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
          setMessages(prev => prev.map(m => m.id === aiId ? { ...m, sections, planContent: latestText } : m))
        },
        onSteps: steps => setMessages(prev => prev.map(m => m.id === aiId ? { ...m, reasoningSteps: steps, reasoningStreaming: true } : m)),
        onTables: tables => setMessages(prev => prev.map(m => m.id === aiId ? { ...m, tables } : m)),
        onTableError: tableError => setMessages(prev => prev.map(m => m.id === aiId ? { ...m, tableError } : m)),
        onCharts: charts => setMessages(prev => prev.map(m => m.id === aiId ? { ...m, charts } : m)),
        onChartError: chartError => setMessages(prev => prev.map(m => m.id === aiId ? { ...m, chartError } : m)),
      })

      const text = result.sections === undefined ? result.text : answerSectionsToPlainMarkdown(result.sections)
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
      setMessages(prev => prev.map(m =>
        m.id === aiId
          ? {
              ...m, isGenerating: false, planContent: text,
              sections: result.sections,
              tables: result.tables,
              tableError: result.tableError,
              reasoningSteps: result.steps.length ? result.steps : undefined,
              reasoningStreaming: false,
              inspectionDetail: result.inspectionDetail,
              laneExecutions: result.laneExecutions,
              toolResults: result.toolResults,
              unnarratedRecords: result.unnarratedRecords,
              selectionPolicy: result.selectionPolicy,
              detailLookup: createMarketDetailLookup(result.detailContract, result.conversationId ?? effectiveChatId, result.traceId),
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
      if (!(err instanceof Error && err.name === 'AbortError')) onAlert?.(MARKET_ALERT.query)
    } finally {
      clearTimeout(timeoutId)
      abortControllerRef.current = null
    }
  }

  const stop = () => {
    if (streamRef.current) { streamRef.current.complete(); return }
    abortControllerRef.current?.abort()
  }

  const resetChat = () => {
    streamRef.current?.discard()
    abortControllerRef.current?.abort()
    setMessages([])
    setChatId(null)
    sessionIdRef.current = null
    resetDocs()
  }

  const removeSentFile = (messageId: string, documentId: number) => {
    const sid = sessionIdRef.current
    if (sid) void deleteMarketDocument(sid, documentId)
    setMessages(prev => prev.map(m =>
      m.id === messageId ? { ...m, files: (m.files ?? []).filter(f => f.documentId !== documentId) } : m
    ))
  }

  const snapshot = (): AgentChatSnapshot => ({
    chatId,
    messages: messages
      .filter(m => (m.role === 'user' ? (m.content || (m.files?.length ?? 0) > 0) : (m.planContent || m.streamNotice || (m.reasoningSteps?.length ?? 0) > 0)))
      .map(m => ({ role: m.role, content: (m.role === 'user' ? m.content : m.planContent) ?? '', reasoningSteps: m.reasoningSteps, reasoningInitiallyExpanded: Boolean(m.reasoningSteps?.length), sections: m.sections, tables: m.tables, tableError: m.tableError, charts: m.charts, chartError: m.chartError, inspectionDetail: m.inspectionDetail, laneExecutions: m.laneExecutions, toolResults: m.toolResults, unnarratedRecords: m.unnarratedRecords, selectionPolicy: m.selectionPolicy, streamNotice: m.streamNotice, detailLookup: m.detailLookup, files: m.files })),
  })

  return (
    <AgentChatContext.Provider
      value={{
        open, openChat: () => setOpen(true), closeChat: () => setOpen(false),
        messages, isGenerating, chatId, sendMessage, stop, resetChat, onAlert, snapshot,
        pendingDocs, uploading, uploadProgress, pickFiles, retryUploadStatus, removePending, clearPending, removeSentFile,
      }}
    >
      {children}
    </AgentChatContext.Provider>
  )
}

// 트리거 버튼 — 페이지 헤더(section-title 우측)에 배치
export function AgentChatTrigger() {
  const { openChat } = useAgentChat()
  return (
    <div className="btn-chat-wrap">
      <a href="#" className="btn-chat" onClick={e => { e.preventDefault(); openChat() }}>Agent 챗봇</a>
    </div>
  )
}

// 패널 — .wrap(flex)의 container-wrap 형제로 배치 → open 시 width 0→540px
export function AgentChatPanel({ userName }: { userName?: string }) {
  const { open, closeChat, messages, isGenerating, sendMessage, stop, resetChat, snapshot, chatId, onAlert,
    pendingDocs, uploading, uploadProgress, pickFiles, retryUploadStatus, removePending, clearPending, removeSentFile } = useAgentChat()
  const navigate = useNavigate()
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const contentRef = useRef<HTMLDivElement>(null)
  const attachBtnRef = useRef<HTMLAnchorElement>(null)
  const [inputValue, setInputValue] = useState('')
  const [showScrollBtn, setShowScrollBtn] = useState(false)
  const [attachOpen, setAttachOpen] = useState(false)

  // 첨부파일 목록 드롭다운 — 바깥 클릭 시 닫기 (트리거 버튼/레이어 내부 클릭은 유지)
  useEffect(() => {
    if (!attachOpen) return
    const handler = (e: MouseEvent) => {
      const t = e.target as Element
      if (attachBtnRef.current?.contains(t)) return
      if (t.closest('.attach-list-pop')) return
      setAttachOpen(false)
    }
    document.addEventListener('click', handler, true)
    return () => document.removeEventListener('click', handler, true)
  }, [attachOpen])

  // 첨부파일 목록 팝업 — 삭제 document_id는 §9-B에 없을 수 있어 messages files(query text 파싱분)에서 매핑
  const docIdByName = useMemo(() => {
    const m = new Map<string, number>()
    messages.forEach(msg => msg.files?.forEach(f => { if (f.documentId) m.set(f.fileName, f.documentId) }))
    return m
  }, [messages])
  // 서버 세션에 저장된 문서 유무 — 클라 상태(messages/pendingDocs)와 desync 대비해 서버 목록으로 보정.
  // chatId 변경·전송(messages)·업로드(pendingDocs)·팝업 오픈 시 재조회.
  const [serverHasDocs, setServerHasDocs] = useState(false)
  const [lastChatIdForDocs, setLastChatIdForDocs] = useState(chatId)
  if (chatId !== lastChatIdForDocs) {
    setLastChatIdForDocs(chatId)
    if (!chatId) setServerHasDocs(false)
  }
  useEffect(() => {
    if (!chatId) return
    let cancelled = false
    fetchMarketDocuments(chatId)
      .then(d => { if (!cancelled) setServerHasDocs(d.length > 0) })
      .catch(() => { /* 조회 실패 시 클라 상태로 폴백 */ })
    return () => { cancelled = true }
  }, [chatId, messages.length, pendingDocs.length, attachOpen])

  // 파일 아이콘은 세션에 파일이 있을 때만 노출 (서버 목록 OR 클라 상태)
  const hasSessionFiles = serverHasDocs || messages.some(m => (m.files?.length ?? 0) > 0) || pendingDocs.length > 0

  const isDetail = messages.length > 0

  const adjustHeight = () => {
    const el = textareaRef.current
    if (!el) return
    const BASE = 48, MAX = 26 * 5 + 20
    el.style.height = `${BASE}px`
    const sh = el.scrollHeight
    if (sh > BASE) {
      el.style.height = `${Math.min(sh, MAX)}px`
      el.style.overflowY = sh <= MAX ? 'hidden' : 'auto'
    } else {
      el.style.height = `${BASE}px`
      el.style.overflowY = 'hidden'
    }
  }

  const handleSend = () => {
    const content = inputValue.trim()
    if (!content || isGenerating || uploading) return   // 업로드 진행 중엔 전송 대기
    const files = pendingDocs
    clearPending()
    sendMessage(content, files)
    setInputValue('')
    if (textareaRef.current) { textareaRef.current.style.height = '48px'; textareaRef.current.style.overflowY = 'hidden' }
  }

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.nativeEvent.isComposing) return
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSend() }
  }

  const handleContentScroll = () => {
    const el = contentRef.current
    if (!el) return
    setShowScrollBtn(el.scrollHeight - el.scrollTop - el.clientHeight > 50)
  }

  const scrollToBottom = () => { contentRef.current?.scrollTo({ top: contentRef.current.scrollHeight, behavior: 'smooth' }) }

  // 메시지 갱신 시 자동 하단 스크롤
  useEffect(() => {
    const el = contentRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [messages])

  // 전체 채팅 화면으로 이동 — 대화 유지 상태로 이동 후 챗봇 닫기
  const handleGoFullScreen = () => {
    const snap = snapshot()
    closeChat()
    navigate('/market/chat', { state: { chatId: snap.chatId, messages: snap.messages } })
  }

  return (
    <div className={`agent-chat-wrap${open ? ' open' : ''}`}>
      <div className="inner-wrap">
        <div className="agent-chat-header">
          <div className="agent-chat-title">시장분석 챗봇</div>
          <div className="btns-right">
            {hasSessionFiles && (
              <a ref={attachBtnRef} href="#" className={`btn-attach${attachOpen ? ' active' : ''}`} onClick={e => { e.preventDefault(); setAttachOpen(o => !o) }}>
                <div className="tooltip-attach"><p>첨부파일 목록</p></div>
              </a>
            )}
            <a href="#" className="btn-new-chat" onClick={e => { e.preventDefault(); resetChat() }}>
              <div className="tooltip-new-chat"><p>새 채팅 (Alt + N)</p></div>
            </a>
            <a href="#" className="btn-toggle" onClick={e => { e.preventDefault(); handleGoFullScreen() }}>
              <div className="tooltip-toggle"><p>전체 화면 전환</p></div>
            </a>
            <a href="#" className="btn-close" onClick={e => { e.preventDefault(); closeChat() }} />
          </div>
          {attachOpen && hasSessionFiles && (
            <AttachmentListPopup
              asDropdown
              onClose={() => setAttachOpen(false)}
              appSessionId={chatId}
              docIdByName={docIdByName}
              onAlert={onAlert}
            />
          )}
        </div>

        <div className={`agent-chat-content-wrap scroll-container${isDetail ? ' chat-mode' : ''}`} ref={contentRef} onScroll={handleContentScroll}>
          <div className="agent-chat-content">
            <div className="agent-chat-content-inner">
              {!isDetail ? (
                <div className="text-c-wrap">
                  <div className="agent-chat-icon" />
                  <div className="agent-chat-text">
                    <dl>
                      <dt>안녕하세요 {userName ?? ''}님,</dt>
                      <dd>시장분석 데이터를 질문해 보세요</dd>
                    </dl>
                  </div>
                </div>
              ) : (
                messages.map(msg =>
                  msg.role === 'user'
                    ? <ChatMessageUser key={msg.id} content={msg.content ?? ''} onRemoveFile={docId => removeSentFile(msg.id, docId)} />
                    : <ChatMessageAI key={msg.id} id={msg.id} planContent={msg.planContent ?? ''} isGenerating={msg.isGenerating ?? false} headerLabel={msg.headerLabel} sections={msg.sections} reasoningSteps={msg.reasoningSteps} reasoningAnimate={msg.reasoningAnimate} reasoningStreaming={msg.reasoningStreaming} reasoningInitiallyExpanded={Boolean(msg.reasoningSteps?.length)} ttftMs={msg.ttftMs} tables={msg.tables} tableError={msg.tableError} charts={msg.charts} chartError={msg.chartError} selectionPolicy={msg.selectionPolicy} streamNotice={msg.streamNotice} />
                )
              )}
            </div>
          </div>
        </div>

        <div className="agent-chat-input-wrap">
          <div className="input-wrapper">
            <FilePreviewList
              docs={pendingDocs}
              uploadProgress={uploadProgress}
              onRemove={removePending}
              onRetryStatus={retryUploadStatus}
              onRetryUpload={() => fileInputRef.current?.click()}
            />
            <div className="btm-input-wrap">
              <button type="button" className="btn-file" onClick={() => fileInputRef.current?.click()} disabled={uploading}>
                <div className="tooltip-btn-file"><p>파일 업로드</p></div>
              </button>
              <input
                ref={fileInputRef}
                type="file"
                multiple
                accept=".pdf,.pptx,.docx,.xlsx"
                style={{ display: 'none' }}
                onChange={e => { if (e.target.files?.length) pickFiles(e.target.files); e.target.value = '' }}
              />
              <textarea
                ref={textareaRef}
                id="main-input"
                placeholder="AI에게 질문하거나 작업을 요청하세요."
                value={inputValue}
                onChange={e => { setInputValue(e.target.value); adjustHeight() }}
                onKeyDown={handleKeyDown}
              />
              {isGenerating
                ? <button type="button" className="btn-stop" onClick={stop} />
                : <button type="button" className={`btn-send${inputValue.trim() && !uploading ? ' active' : ''}`} onClick={handleSend} />
              }
            </div>
          </div>
          <div className={`bottom-floating-wrap${showScrollBtn ? ' active' : ''}`}>
            <div className="btn-bottom-floating" onClick={scrollToBottom} />
          </div>
        </div>
      </div>
    </div>
  )
}
