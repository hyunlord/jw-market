import { useState, useRef, useEffect, useMemo, Children, type ReactNode } from 'react'
import ReactMarkdown, { type Components } from 'react-markdown'
import type { PlanStep, SourceDoc, ChunkBbox, ReasoningStep } from '../../utils/planSignal'
import { planContentToMarkdown } from '../../utils/planSignal'
import { openSourcePdf, extractDocId } from '../../utils/openSourcePdf'
import { formatTtft } from '../../utils/marketTtft'
import { portalMarkdownRehypePlugins, portalMarkdownRemarkPlugins } from '../../utils/markdownSanitize'
import type { MarketChart } from '../../utils/marketStream'
import type { AnswerSectionState, EvidenceDisplayCatalog, EvidenceGroup } from '../../utils/answerSections'
import type { MarketTable } from '../../utils/marketTables'
import type { SelectionPolicy } from '../../utils/traceToolResults'
import type { MarketSourceItem } from '../../utils/marketSources'
import { parseMarketAnswerSources } from '../../utils/marketSources'
import { chatAnswerStateKey } from '../../utils/chatAnswerSections'
import CollapsibleAnswerMarkdown from './CollapsibleAnswerMarkdown'
import MarketCharts from './MarketCharts'
import MarketTables from './MarketTables'
import SectionSlotAnswer from './SectionSlotAnswer'
import { MarketSourceCitationText, MarketSourcesSection } from './MarketSources'

// 본문 인용 마커: [doc_id] (p.PAGES) — 백엔드가 답변 text에 박아 보냄. 예: [101989] (p.4) / (p.1, 4) / (p.1-2)
const CITATION_RE = /\[(\d+)\]\s*\(p\.\s*([\d,\s-]+)\)/g

// 인용 마커를 칩(span) + 페이지별 버튼으로 변환. onCite(docId, page)로 PDF 뷰어 연결.
function citationsInString(text: string, keyBase: string, onCite: (docId: string, page: number) => void): ReactNode[] {
  const nodes: ReactNode[] = []
  let last = 0
  let m: RegExpExecArray | null
  const re = new RegExp(CITATION_RE.source, 'g')   // 전역 정규식 lastIndex 공유 회피
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) nodes.push(text.slice(last, m.index))
    const docId = m[1]
    const pages = m[2].split(',').map(s => s.trim()).filter(Boolean)   // "1, 4" → ["1","4"]
    nodes.push(
      <span className="ref-chip" key={`${keyBase}-${m.index}`}>
        {/* 표시만 +1 클릭 매핑(onCite)은 원본 docId 그대로 */}
        [{Number(docId) + 1}] (p.
        {pages.map((tok, i) => {
          const pageNum = parseInt(tok, 10)   // 범위 "1-2"는 시작 페이지로
          return (
            <button
              type="button"
              key={i}
              onClick={() => { if (Number.isFinite(pageNum)) onCite(docId, pageNum) }}
            >{tok}</button>
          )
        })}
        )
      </span>
    )
    last = m.index + m[0].length
  }
  if (last < text.length) nodes.push(text.slice(last))
  return nodes
}

// react-markdown children 중 문자열 부분만 칩으로 변환 (요소 자식은 그대로)
function withCitations(children: ReactNode, keyBase: string, onCite: (docId: string, page: number) => void): ReactNode {
  return Children.toArray(children).flatMap((child, i) =>
    typeof child === 'string' ? citationsInString(child, `${keyBase}-${i}`, onCite) : [child]
  )
}

function withMarketSourceCitations(
  children: ReactNode,
  keyBase: string,
  sources: readonly MarketSourceItem[],
  anchorPrefix: string,
  onInspectionSourceOpen?: (sourceLabel: string) => void,
): ReactNode {
  return Children.toArray(children).map((child, index) => (
    typeof child === 'string'
      ? <MarketSourceCitationText
          key={`${keyBase}-${index}`}
          text={child}
          sources={sources}
          anchorPrefix={anchorPrefix}
          onInspectionSourceOpen={onInspectionSourceOpen}
        />
      : child
  ))
}

// [1,4,4,6] → "1, 4, 6"
function formatPages(pages: number[]): string {
  return [...new Set(pages)].filter(n => Number.isFinite(n)).sort((a, b) => a - b).join(', ')
}

// 같은 PDF(doc_id) 출처들을 한 항목으로 병합. 페이지 표기는 i_page, 하이라이트용 bboxes는 청크 전체 합집합.
interface SourceGroup {
  key: string
  fileName: string
  filePath?: string
  canOpen: boolean
  pageRange: string
  bboxes: ChunkBbox[]
}

function groupSources(docs: SourceDoc[]): SourceGroup[] {
  const order: string[] = []
  const map = new Map<string, { fileName: string; filePath?: string; docId: number | null; pages: number[]; bboxes: ChunkBbox[] }>()
  docs.forEach((d, i) => {
    const docId = extractDocId(d.filePath)
    const key = docId !== null ? `id:${docId}` : `idx:${i}`   // doc_id 없으면 병합 안 하고 개별
    let g = map.get(key)
    if (!g) { g = { fileName: d.fileName, filePath: d.filePath, docId, pages: [], bboxes: [] }; map.set(key, g); order.push(key) }
    for (const p of (d.pageNos ?? [])) g.pages.push(p)
    for (const b of (d.chunkBboxes ?? [])) g.bboxes.push(b)
  })
  return order.map(key => {
    const g = map.get(key)!
    return { key, fileName: g.fileName, filePath: g.filePath, canOpen: g.docId !== null, pageRange: formatPages(g.pages), bboxes: g.bboxes }
  })
}

// MCP 도구 한 번 호출 — MainPage 정의와 동일 구조 (structural typing으로 호환)
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

function splitRationale(rationale: string): { subtitle: string; body: string } {
  const trimmed = rationale.trim()
  const nlIdx = trimmed.indexOf('\n')
  const firstLine = (nlIdx === -1 ? trimmed : trimmed.slice(0, nlIdx)).trim()
  const rest = nlIdx === -1 ? '' : trimmed.slice(nlIdx + 1).trim()
  const heading = firstLine.match(/^#{1,6}\s+(.*)$/)
  return heading ? { subtitle: heading[1].trim(), body: rest } : { subtitle: firstLine, body: rest }
}

const reasoningSpinner = (
  <svg className="spinner" width="24" height="24" viewBox="0 0 24 24" fill="none">
    <path d="M23 12C23 18.0751 18.0751 23 12 23C5.92487 23 1 18.0751 1 12C1 5.92487 5.92487 1 12 1C18.0751 1 23 5.92487 23 12ZM3.2 12C3.2 16.8601 7.13989 20.8 12 20.8C16.8601 20.8 20.8 16.8601 20.8 12C20.8 7.13989 16.8601 3.2 12 3.2C7.13989 3.2 3.2 7.13989 3.2 12Z" fill="#D1D2D7" />
    <circle className="answer-spinner-progress" cx="12" cy="12" r="10" fill="none" stroke="#060B11" strokeWidth="2" strokeLinecap="round" />
  </svg>
)

function useSmoothReveal(full: string, active: boolean): string {
  const [shown, setShown] = useState(full)
  useEffect(() => {
    if (!active) return
    let raf = 0
    const tick = () => {
      setShown(prev => {
        if (prev.length >= full.length) return prev === full ? prev : full
        const step = Math.max(3, Math.ceil((full.length - prev.length) / 7))
        return full.slice(0, prev.length + step)
      })
      raf = requestAnimationFrame(tick)
    }
    raf = requestAnimationFrame(tick)
    return () => cancelAnimationFrame(raf)
  }, [active, full])
  return active ? shown : full
}

function ReasoningTimeline({ steps, animate, streaming }: { steps: ReasoningStep[]; animate: boolean; streaming?: boolean }) {
  const [doneCount, setDoneCount] = useState(animate ? 0 : steps.length)
  useEffect(() => {
    if (streaming !== undefined) return
    if (!animate || steps.length === 0) return
    const t = setInterval(() => {
      setDoneCount(prev => {
        if (prev >= steps.length) { clearInterval(t); return prev }
        return prev + 1
      })
    }, 600)
    return () => clearInterval(t)
  }, [animate, steps.length, streaming])

  // 실시간 모드: 스트림 진행 중이면 마지막(최신) 스텝이 진행 중(스피너), 그 앞은 완료(체크). 종료되면 전부 완료.
  const effectiveDone = streaming !== undefined
    ? (streaming ? steps.length - 1 : steps.length)
    : doneCount

  return (
    <div className="reasoning-timeline">
      {steps.map((step, i) => {
        const { subtitle, body } = splitRationale(step.rationale)
        const state = i < effectiveDone ? 'done' : i === effectiveDone ? 'current' : 'pending'
        return (
          <div key={`${step.nodeId}-${i}`} className={`answer-wrap${state === 'pending' ? ' disabled' : ''}`}>
            <div className="answer-tit-wrap">
              {state === 'done'
                ? <div className="answer-spinner-end" />
                : <div className="answer-spinner">{reasoningSpinner}</div>}
              <div className="tx-tit">{subtitle}</div>
            </div>
            {body && (
              <div className="tx-answer">
                <ReactMarkdown
                  remarkPlugins={portalMarkdownRemarkPlugins}
                  rehypePlugins={portalMarkdownRehypePlugins}
                >
                  {body}
                </ReactMarkdown>
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}

interface Props {
  id: string
  planContent: string
  isGenerating: boolean
  mcpDetails?: McpExecutedDetail[]
  onMcpOpen?: (details: McpExecutedDetail[]) => void
  // isPlan === true 일 때만 취소/수정/실행 버튼이 active. plan-pending 상태에서 비활성화 가능
  isPlan?: boolean
  planActionsDisabled?: boolean  // 호출 중(plan-executing) 또는 이미 처리됨일 때 true
  onReject?: () => void         // 취소 → POST /chat/abort { chat_id, trace_id } (snake_case, 단순 작업 중단)
  onProceed?: () => void        // 실행 → POST /chat/query/proceed { chatId, question:'진행', startNodeId }
  onModifyReject?: (question: string) => void  // 수정 → POST /chat/query/reject { chatId, question:입력값, startNodeId }
  // 헤더 텍스트 — 미지정 시 '답변 생성 계획'. 실행(/proceed) 클릭으로 만든 메시지는 '추론 과정'
  headerLabel?: string
  // 추론 과정(Visible Reasoner) 스텝 — 있으면 답변 본문 위에 순차 완료 타임라인으로 노출 (PDF 12p)
  reasoningSteps?: ReasoningStep[]
  // 순차 완료 애니메이션 여부 — 라이브 proceed만 true, /log 복원 시 false(즉시 완료)
  reasoningAnimate?: boolean
  // ★ 실시간 스트림 모드 — 지정 시 가짜 애니메이션 대신 실제 노드 도착으로 스피너/체크 구동 (StreamPage 전용).
  //   true=스트림 진행 중(마지막 스텝 스피너), false=완료(전부 체크). undefined면 기존 animate 방식(MainPage).
  reasoningStreaming?: boolean
  reasoningInitiallyExpanded?: boolean
  ttftMs?: number
  // BACK_API.md 3-4 A안: state.planning_result에서 추출한 구조화 plan. 있으면 text 대신 이걸 우선 노출
  // (reject 응답 등에서 text 자연어와 실제 실행될 plan이 불일치할 수 있어 신뢰성 위해 state 우선)
  planSteps?: PlanStep[]
  // 출처 문서 — 있으면 ai-content 아래 + MCP 실행 정보 버튼 위에 펼침/접힘 섹션 렌더
  sourceDocuments?: SourceDoc[]
  // PDF 열기 실패 시 호출 — MainPage가 알림 모달 표시
  onPdfError?: (message: string) => void
  // PDF 출처 클릭 시 호출 — MainPage가 오른쪽 뷰어 패널에 표시 (미지정 시 새 탭 폴백)
  onPdfView?: (url: string, fileName: string, bboxes?: ChunkBbox[], initialPage?: number) => void
  canApplyReport?: boolean
  reportApplyChecked?: boolean
  onToggleReportApply?: () => void
  loadingLabel?: string
  charts?: MarketChart[]
  chartError?: string
  tables?: MarketTable[]
  tableError?: string
  streamNotice?: string
  selectionPolicy?: SelectionPolicy
  sections?: AnswerSectionState[]
  onEvidenceOpen?: (evidenceId: string, evidence: readonly { evidenceId: string; label: string }[], catalog: EvidenceDisplayCatalog | undefined, group: EvidenceGroup | undefined) => void
  onInspectionOpen?: () => void
  onInspectionSourceOpen?: (sourceLabel: string) => void
  inspectionOpen?: boolean
}

export default function ChatMessageAI({
  id, planContent, isGenerating, mcpDetails, onMcpOpen,
  isPlan = false, planActionsDisabled = false,
  onReject, onProceed, onModifyReject,
  headerLabel, reasoningSteps, reasoningAnimate = false, reasoningStreaming, reasoningInitiallyExpanded = false, ttftMs, sourceDocuments, onPdfError, onPdfView,
  canApplyReport = false, reportApplyChecked = false, onToggleReportApply, loadingLabel, charts = [], chartError, tables = [], tableError, streamNotice, selectionPolicy, sections,
  onInspectionOpen, onInspectionSourceOpen, onEvidenceOpen, inspectionOpen = false,
}: Props) {
  // ⚠️ planSteps prop은 인터페이스에 보존되지만 현재 화면 노출엔 사용 X (data.result.data.text를 우선).
  //    백엔드가 reject 응답의 text/state.planning_result 일치 작업하면서 다시 state 우선이 필요해질 경우
  //    `planSteps`를 destructure에 추가하고 위 본문 렌더에서 planSteps 우선 분기로 한 줄 복원하면 됨.
  const [contentExpanded, setContentExpanded] = useState(true)
  const [reasoningExpanded, setReasoningExpanded] = useState(isGenerating || reasoningInitiallyExpanded)
  const [reasoningWasGenerating, setReasoningWasGenerating] = useState(isGenerating)
  if (reasoningWasGenerating !== isGenerating) {
    setReasoningWasGenerating(isGenerating)
    if (isGenerating || reasoningWasGenerating || reasoningInitiallyExpanded) setReasoningExpanded(true)
  }
  const [editPanelOpen, setEditPanelOpen] = useState(false)
  const [editValue, setEditValue] = useState('')
  const editRef = useRef<HTMLTextAreaElement>(null)
  const fullMarkdown = planContentToMarkdown(planContent)
  const marketAnswer = useMemo(() => parseMarketAnswerSources(fullMarkdown), [fullMarkdown])
  const sourceAnchorPrefix = `market-source-${id.replace(/[^A-Za-z0-9_-]/g, '-') || 'answer'}`
  const shownMarkdown = useSmoothReveal(marketAnswer.bodyMarkdown, isGenerating)

  // 인용 칩 클릭 → 해당 출처 문서를 PDF 뷰어에 표시.
  //   ⚠️ 인용 마커 [N]은 sourceDocuments 배열의 0-based 인덱스 (백엔드 변경 — 이전 doc_id 아님).
  //      예: [0] (p.4) → sourceDocuments[0]을 p.4로 열기. page=initialPage, 하이라이트는 그 문서의 chunk_bboxes.
  const markdownComponents = useMemo<Components>(() => {
    const onCite = (refIdx: string, page: number) => {
      const docs = sourceDocuments ?? []
      const num = Number(refIdx)
      const target = docs.find(d => extractDocId(d.filePath) === num)
        ?? (Number.isInteger(num) ? docs[num] : undefined)
      if (!target) { onPdfError?.('해당 출처 문서를 찾을 수 없습니다.'); return }
      const bboxes = target.chunkBboxes ?? []
      openSourcePdf(target.filePath, onPdfError, onPdfView, page, bboxes.length ? bboxes : undefined)
    }
    return {
      table: ({ children }) => <div className="ai-table-wrap"><table>{children}</table></div>,
      a: ({ href, children }) => <a href={href} target="_blank" rel="noopener noreferrer">{children}</a>,
      p: ({ children }) => <p>{withMarketSourceCitations(withCitations(children, 'p', onCite), 'p', marketAnswer.sources, sourceAnchorPrefix, onInspectionSourceOpen)}</p>,
      li: ({ children }) => <li>{withMarketSourceCitations(withCitations(children, 'li', onCite), 'li', marketAnswer.sources, sourceAnchorPrefix, onInspectionSourceOpen)}</li>,
      td: ({ children }) => <td>{withMarketSourceCitations(withCitations(children, 'td', onCite), 'td', marketAnswer.sources, sourceAnchorPrefix, onInspectionSourceOpen)}</td>,
    }
  }, [sourceDocuments, onPdfError, onPdfView, marketAnswer.sources, sourceAnchorPrefix, onInspectionSourceOpen])

  const sourceGroups = useMemo(() => groupSources(sourceDocuments ?? []), [sourceDocuments])

  const handleEditInput = () => {
    const el = editRef.current
    if (!el) return
    const BASE = 26, MAX = 26 * 3
    el.style.height = `${BASE}px`
    const sh = el.scrollHeight
    if (sh > BASE) {
      el.style.height = `${Math.min(sh, MAX)}px`
      el.style.overflowY = sh <= MAX ? 'hidden' : 'auto'
    } else {
      el.style.overflowY = 'hidden'
    }
  }

  return (
    <>
      {reasoningSteps && reasoningSteps.length > 0 && (
        <div className="chat-message-ai">
          <div className="ai-header">
            <div className="title-group">
              <div className="icon-wrap" />
              <div className="text-wrap">추론 과정</div>
            </div>
            <div className="toggle-group">
              <button
                type="button"
                aria-label={reasoningExpanded ? '추론 과정 접기' : '추론 과정 펼치기'}
                aria-expanded={reasoningExpanded}
                className={`btn-toggle-content${reasoningExpanded ? ' open' : ''}`}
                onClick={() => setReasoningExpanded(prev => !prev)}
              />
            </div>
          </div>
          <div className={`ai-content-wrap ${reasoningExpanded ? 'open' : 'close'}`}>
            <div className="ai-content" style={{ fontWeight: 400, fontSize: 16, lineHeight: '160%' }}>
              <ReasoningTimeline steps={reasoningSteps} animate={reasoningAnimate} streaming={reasoningStreaming} />
            </div>
          </div>
        </div>
      )}

    <div className="chat-message-ai">
      <div className="ai-header">
        <div className="title-group">
          {isGenerating ? (
            <div className="fixed-8bar-spinner">
              {Array.from({ length: 8 }, (_, i) => (
                <div key={i} className={`bar bar${i + 1}`} />
              ))}
            </div>
          ) : (
            <div className="icon-wrap" />
          )}
          <div className="text-wrap">{headerLabel ?? '답변 생성 계획'}</div>
        </div>
        <div className="answer-header-actions">
          {!isGenerating && onInspectionOpen && (
            <button
              type="button"
              className="answer-inspection-trigger"
              aria-controls="answer-inspection-panel"
              aria-expanded={inspectionOpen}
              onClick={onInspectionOpen}
            >
              조회 상세
            </button>
          )}
          <div className="toggle-group">
            <button
              type="button"
              aria-label={contentExpanded ? '답변 접기' : '답변 펼치기'}
              className={`btn-toggle-content${contentExpanded ? ' open' : ''}`}
              onClick={() => setContentExpanded(prev => !prev)}
            />
          </div>
        </div>
      </div>

      <div className={`ai-content-wrap ${contentExpanded ? 'open' : 'close'}`}>
        <div className="ai-content" style={{ fontWeight: 400, fontSize: 16, lineHeight: '160%' }}>
          {/* 본문은 항상 `result.data.text`(planContent) 우선.
              planContent 안에 `{ planning_result: [...] }` JSON 블록이 박혀 있으면 planContentToMarkdown이
              자동으로 ordered list로 변환 (구버전 호환). planSteps prop은 보존되지만 화면 노출엔 사용 X
              (백엔드 명세가 다시 state.planning_result 우선으로 정리되면 한 줄로 복원 가능). */}
          {sections !== undefined ? (
            <SectionSlotAnswer
              sections={sections}
              components={markdownComponents}
              onEvidenceOpen={onEvidenceOpen}
              tables={tables}
              tableError={tableError}
              selectionPolicy={selectionPolicy}
            />
          ) : isGenerating && loadingLabel && !fullMarkdown.trim() ? (
            <div className="answer-wrap">
              <div className="answer-tit-wrap">
                <div className="answer-spinner">{reasoningSpinner}</div>
                <div className="tx-tit">{loadingLabel}</div>
              </div>
            </div>
          ) : (
            <CollapsibleAnswerMarkdown
              key={chatAnswerStateKey(shownMarkdown, marketAnswer.sources.length)}
              markdown={shownMarkdown}
              components={markdownComponents}
              idPrefix={`answer-${id.replace(/[^A-Za-z0-9_-]/g, '-') || 'content'}`}
              sourceCount={marketAnswer.sources.length}
              sourceSectionIndex={marketAnswer.sourceSectionIndex}
              collapseEnabled={!isGenerating}
              renderSources={(
                <MarketSourcesSection
                  sources={marketAnswer.sources}
                  anchorPrefix={sourceAnchorPrefix}
                  hideTitle
                />
              )}
            />
          )}
          {streamNotice && <div className="market-stream-notice" role="alert">{streamNotice}</div>}
          {sections === undefined && <MarketTables tables={tables} error={tableError} selectionPolicy={selectionPolicy} />}
          <MarketCharts charts={charts} error={chartError} />
        </div>
        {ttftMs !== undefined && (
          <div className="ai-response-meta">{formatTtft(ttftMs)}</div>
        )}

        {!isGenerating && isPlan && (
          <>
            <div className="action-btn-group">
              <button
                type="button"
                className="btn-cancel"
                disabled={planActionsDisabled}
                onClick={() => onReject?.()}
              >취소</button>
              <button
                type="button"
                className="btn-edit"
                disabled={planActionsDisabled}
                onClick={() => setEditPanelOpen(p => !p)}
              >수정</button>
              <button
                type="button"
                className="btn-run"
                disabled={planActionsDisabled}
                onClick={() => onProceed?.()}
              >실행</button>
            </div>
            {planActionsDisabled && (
              <div className="plan-action-status" role="status" aria-live="polite">실행 중입니다</div>
            )}
            {editPanelOpen && (
              <div className="edit-panel">
                <div className="inner-wrap">
                  <textarea
                    ref={editRef}
                    id={`edit-textarea-${id}`}
                    className="edit-textarea"
                    placeholder="수정할 부분을 구체적으로 안내해 주세요."
                    value={editValue}
                    onChange={e => { setEditValue(e.target.value); handleEditInput() }}
                  />
                  <div className="ghost-div" />
                  <div className="edit-submit-group">
                    <a href="#" className="btn-edit-cancel" onClick={e => { e.preventDefault(); setEditPanelOpen(false) }}>취소</a>
                    <a
                      href="#"
                      className={`btn-edit-apply${editValue.trim() && !planActionsDisabled ? ' active' : ''}`}
                      onClick={e => {
                        e.preventDefault()
                        const q = editValue.trim()
                        if (!q || planActionsDisabled) return
                        onModifyReject?.(q)        // 수정 → /reject (question = 입력값) — 계획 재생성 요청
                        setEditPanelOpen(false)
                        setEditValue('')
                      }}
                    >전송</a>
                  </div>
                </div>
              </div>
            )}
          </>
        )}

        {/* 출처 — 같은 PDF 병합 + 제목 아래 페이지 표기 (퍼블 chat.html 디자인) */}
        {!isGenerating && sourceGroups.length > 0 && (
          <>
            <div className="separate-line"></div>
            <div className="source-wrap">
              <div className="source-title">
                출처 <span className="num">{sourceGroups.length}</span>
              </div>
              <div className="source-list">
                {sourceGroups.map(g => {
                  const dotIdx = g.fileName.lastIndexOf('.')   // 파일명/확장자 분리 (.pdf만 따로 표기)
                  const namePart = dotIdx > 0 ? g.fileName.slice(0, dotIdx) : g.fileName
                  const extPart = dotIdx > 0 ? g.fileName.slice(dotIdx) : ''
                  return (
                    <div
                      key={g.key}
                      className="s-list-item"
                      role={g.canOpen ? 'button' : undefined}
                      tabIndex={g.canOpen ? 0 : undefined}
                      style={{ cursor: g.canOpen ? 'pointer' : 'default' }}
                      onClick={g.canOpen ? () => openSourcePdf(g.filePath, onPdfError, onPdfView, undefined, g.bboxes.length ? g.bboxes : undefined) : undefined}
                    >
                      <div className="file-item">
                        <div className="file-item-icon" />
                        <div className="file-item-content">
                          <div className="file-item-name">{namePart}</div>
                          {extPart && <div className="file-item-ext">{extPart}</div>}
                        </div>
                      </div>
                      {g.pageRange && <div className="file-item-footer">{g.pageRange} page</div>}
                    </div>
                  )
                })}
              </div>
            </div>
          </>
        )}

        {!isGenerating && ((mcpDetails && mcpDetails.length > 0) || canApplyReport) && (
          <div className="action-btn-group02">
            {canApplyReport && (
              <div className="custom-checkbox">
                <input
                  type="checkbox"
                  id={`report-apply-${id}`}
                  className="checkbox-input"
                  checked={reportApplyChecked}
                  onChange={() => onToggleReportApply?.()}
                />
                <label htmlFor={`report-apply-${id}`} className="checkbox-label">
                  <span className="icon-check" />
                  <span className="text">보고서 적용</span>
                </label>
              </div>
            )}
            {mcpDetails && mcpDetails.length > 0 && (
              <a
                href="#"
                className="btn-mcp"
                onClick={e => { e.preventDefault(); onMcpOpen?.(mcpDetails) }}
              >
                MCP 실행 정보
              </a>
            )}
          </div>
        )}
      </div>
    </div>
    </>
  )
}
