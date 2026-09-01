import { useRef, useEffect, useLayoutEffect, useState } from 'react'
import { openSourcePdf, extractDocId } from '../../utils/openSourcePdf'
import type { ChunkBbox } from '../../utils/planSignal'

// visible_used_tools.content.used_tools[] 한 항목 = MCP 도구 한 번 호출 (step/tool/args/result)
interface ToolCall {
  step: number
  tool: string
  input: unknown   // = args
  output: unknown  // = result
}

interface SourceDoc {
  fileName: string
  pageNos: number[]
  pageContent: string
  filePath?: string
  chunkBboxes?: ChunkBbox[]
}

interface TaskCard {
  id: string
  title: string
  description: string
  toolCalls: ToolCall[]
  sourceDocuments: SourceDoc[]
}

interface McpPromptGroup {
  id: string
  prompt: string
  cards: TaskCard[]
}

const mcpLoadingSpinner = (
  <svg className="spinner" width="18" height="18" viewBox="0 0 24 24" fill="none">
    <path d="M23 12C23 18.0751 18.0751 23 12 23C5.92487 23 1 18.0751 1 12C1 5.92487 5.92487 1 12 1C18.0751 1 23 5.92487 23 12ZM3.2 12C3.2 16.8601 7.13989 20.8 12 20.8C16.8601 20.8 20.8 16.8601 20.8 12C20.8 7.13989 16.8601 3.2 12 3.2C7.13989 3.2 3.2 7.13989 3.2 12Z" fill="#D1D2D7" />
    <circle className="answer-spinner-progress" cx="12" cy="12" r="10" fill="none" stroke="#00A9E5" strokeWidth="2" strokeLinecap="round" />
  </svg>
)

interface Props {
  isOpen: boolean
  hasData: boolean
  promptGroups: McpPromptGroup[]
  singleGroupId?: string
  streaming?: boolean
  onClose: () => void
  // MCP 도구 li 클릭 시 — 부모(MainPage)가 viewReportModal 띄움. card.title이 mcpName 그대로.
  onToolClick?: (mcpName: string, call: ToolCall) => void
  // PDF 열기 실패 시 호출 — MainPage가 알림 모달 표시
  onPdfError?: (message: string) => void
  // PDF 출처 클릭 시 호출 — MainPage가 오른쪽 뷰어 패널에 표시 (미지정 시 새 탭 폴백)
  onPdfView?: (url: string, fileName: string, bboxes?: ChunkBbox[], initialPage?: number) => void
}

export default function Panel({ isOpen, hasData, promptGroups, singleGroupId, streaming, onClose, onToolClick, onPdfError, onPdfView }: Props) {
  const panelRef = useRef<HTMLDivElement>(null)
  const handleRef = useRef<HTMLDivElement>(null)
  const onCloseRef = useRef(onClose)
  useLayoutEffect(() => { onCloseRef.current = onClose })
  // 가로 리사이즈 진행 중 플래그 방지
  const resizingRef = useRef(false)

  // 카드별 "출처" / "도구 실행 결과" 섹션 펼침 상태 — card.id 키. default 접힘으로 일관성 유지
  const [expandedSources, setExpandedSources] = useState<Set<string>>(new Set())
  const toggleSources = (cardId: string) => setExpandedSources(prev => {
    const next = new Set(prev)
    if (next.has(cardId)) next.delete(cardId); else next.add(cardId)
    return next
  })
  const [expandedTools, setExpandedTools] = useState<Set<string>>(new Set())
  const toggleTools = (cardId: string) => setExpandedTools(prev => {
    const next = new Set(prev)
    if (next.has(cardId)) next.delete(cardId); else next.add(cardId)
    return next
  })
  const [expandedGroups, setExpandedGroups] = useState<Set<string>>(new Set())
  const toggleGroup = (id: string) => setExpandedGroups(prev => {
    const next = new Set(prev)
    if (next.has(id)) next.delete(id); else next.add(id)
    return next
  })

  useEffect(() => {
    if (!isOpen) return
    const handler = (e: MouseEvent) => {
      if (resizingRef.current) return   // 리사이즈 직후 뒤따르는 click — 닫지 않음
      if (panelRef.current?.contains(e.target as Node)) return
      // 열려 있는 모달(`.modal-overlay.open`) 내부/overlay 자체 클릭은 Panel 외부로 보지 않음.
      // (모달 X 버튼·모달 안 텍스트 클릭 시 Panel 같이 닫히던 문제 회피)
      const target = e.target as Element | null
      if (target && target.closest && target.closest('.modal-overlay.open')) return
      if (target && target.closest && target.closest('.btn-mcp')) return
      if (target && target.closest && target.closest('.btn-panel')) return   // 헤더 MCP 전체보기 아이콘 — 토글이라 여기서 안 닫음
      onCloseRef.current()
    }
    document.addEventListener('click', handler, true)
    return () => document.removeEventListener('click', handler, true)
  }, [isOpen])

  useEffect(() => {
    const handle = handleRef.current
    const panel = panelRef.current
    if (!handle || !panel) return

    let startX = 0, startWidth = 0, startLeft = 0

    const onMouseMove = (e: MouseEvent) => {
      const deltaX = startX - e.clientX
      const newWidth = Math.max(340, Math.min(window.innerWidth * 0.5, startWidth + deltaX))
      panel.style.width = `${newWidth}px`
      panel.style.left = `${startLeft - (newWidth - startWidth)}px`
    }

    const onMouseUp = () => {
      document.removeEventListener('mousemove', onMouseMove)
      document.removeEventListener('mouseup', onMouseUp)
      document.body.style.cursor = ''
      setTimeout(() => { resizingRef.current = false }, 0)
    }

    const onMouseDown = (e: MouseEvent) => {
      startX = e.clientX
      startWidth = panel.offsetWidth
      startLeft = panel.offsetLeft
      resizingRef.current = true
      document.body.style.cursor = 'ew-resize'
      document.addEventListener('mousemove', onMouseMove)
      document.addEventListener('mouseup', onMouseUp)
    }

    handle.addEventListener('mousedown', onMouseDown)
    return () => handle.removeEventListener('mousedown', onMouseDown)
  }, [])

  const renderCard = (card: TaskCard) => (
    <div key={card.id} className="task-card">
      <div className="task-card-header">
        <div className="icon-check" />
        <div className="task-card-title">{card.title}</div>
      </div>
      <div className="task-card-body">
        <div className="task-card-description">{card.description}</div>
        {card.toolCalls.length > 0 && (() => {
          const toolsOpen = expandedTools.has(card.id)
          return (
            <div className={`task-result${toolsOpen ? ' open' : ''}`}>
              <button
                type="button"
                className="task-result-toggle"
                onClick={() => toggleTools(card.id)}
              >
                <span className="task-result-chevron" aria-hidden="true" />
                <span className="task-result-title">도구 실행 결과 ({card.toolCalls.length})</span>
              </button>
              {toolsOpen && (
                <div className="task-result-list">
                  <ul>
                    {card.toolCalls.map((call, i) => (
                      <li key={i}>
                        <a
                          href="#"
                          onClick={e => {
                            e.preventDefault()
                            onToolClick?.(card.title, call)
                          }}
                        >{call.tool}</a>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          )
        })()}
        {/* 카드별 출처 — 화살표 토글 + 파일 아이콘/페이지수 행. li hover 시 회색 배경 + 클릭 시 PDF 새 창 (ChatMessageAI 출처와 동일 패턴) */}
        {card.sourceDocuments.length > 0 && (() => {
          const open = expandedSources.has(card.id)
          return (
            <div className={`tool-sources${open ? ' open' : ''}`}>
              <button
                type="button"
                className="tool-sources-toggle"
                onClick={() => toggleSources(card.id)}
              >
                <span className="tool-sources-chevron" aria-hidden="true" />
                <span>출처 ({card.sourceDocuments.length})</span>
              </button>
              {open && (
                <ul className="tool-sources-list">
                  {card.sourceDocuments.map((doc, i) => {
                    const canOpenPdf = extractDocId(doc.filePath) !== null
                    // 파일명에서 확장자 분리 (예: "Foo.pdf" → name "Foo" + ext ".pdf")
                    const dotIdx = doc.fileName.lastIndexOf('.')
                    const namePart = dotIdx > 0 ? doc.fileName.slice(0, dotIdx) : doc.fileName
                    const extPart = dotIdx > 0 ? doc.fileName.slice(dotIdx) : ''
                    return (
                      <li
                        key={i}
                        className="s-list-item"
                        role={canOpenPdf ? 'button' : undefined}
                        tabIndex={canOpenPdf ? 0 : undefined}
                        style={{ cursor: canOpenPdf ? 'pointer' : 'default' }}
                        onClick={canOpenPdf ? () => openSourcePdf(doc.filePath, onPdfError, onPdfView, doc.pageNos?.[0], doc.chunkBboxes) : undefined}
                      >
                        <div className="file-item">
                          <div className="file-item-icon" />
                          <div className="file-item-content">
                            <div className="file-item-name">{namePart}</div>
                            {extPart && <div className="file-item-ext">{extPart}</div>}
                          </div>
                        </div>
                      </li>
                    )
                  })}
                </ul>
              )}
            </div>
          )
        })()}
      </div>
    </div>
  )

  return (
    <div ref={panelRef} className={`btn-panel-layer ${isOpen ? 'open' : 'close'}`}>
      <div ref={handleRef} className="resize-handle-left" />
      <div className="inner-wrap">
        {!hasData ? (
          <div className="no-data-box">
            <div className="icon-wrap">
              <svg xmlns="http://www.w3.org/2000/svg" width="36" height="36" viewBox="0 0 36 36" fill="none">
                <path d="M9 3H19.3428C20.1384 3 20.9022 3.3163 21.4648 3.87891L29.1211 11.5352C29.6837 12.0978 30 12.8616 30 13.6572V30C30 31.6569 28.6569 33 27 33H9C7.34315 33 6 31.6569 6 30V6C6 4.34315 7.34315 3 9 3Z" stroke="#B2B2B8" strokeWidth="2"/>
                <path d="M20 3V11C20 12.1046 20.8954 13 22 13H30" stroke="#B2B2B8" strokeWidth="2" strokeLinejoin="round"/>
              </svg>
            </div>
            <div className="text-wrap">
              현재 진행 중인 실행 계획이 없거나,<br />
              표시할 데이터가 없습니다.
            </div>
          </div>
        ) : (
          <>
            <div className="panel-header">
              <div className="panel-title">MCP 실행 정보</div>
              <div className="btn-panel-close" onClick={onClose}>패널 닫기</div>
            </div>
            <div className="panel-content scroll-container">
              {singleGroupId
                ? (
                  <>
                    {(promptGroups.find(g => g.id === singleGroupId)?.cards ?? []).map(renderCard)}
                    {streaming && (
                      <div className="task-card">
                        <div className="task-card-header">
                          <div className="answer-spinner">{mcpLoadingSpinner}</div>
                          <div className="task-card-title" style={{ color: '#82828d', fontSize: 15, fontWeight: 500 }}>도구 실행 중…</div>
                        </div>
                      </div>
                    )}
                  </>
                ) : (
                  promptGroups.map(group => {
                    const open = expandedGroups.has(group.id)
                    return (
                      <div key={group.id} className="task-card-wrap">
                        <div className="task-card-inner">
                          <div className="tp-tx-area">{group.prompt}</div>
                          <div className={`bt-info-area${open ? ' open' : ''}`}>
                            <div className="btn-view-mcp-info" onClick={() => toggleGroup(group.id)}>
                              <div className="icon-arrow" />
                              MCP 정보 보기
                            </div>
                            {open && group.cards.map(renderCard)}
                          </div>
                        </div>
                      </div>
                    )
                  })
                )}
            </div>
          </>
        )}
      </div>
    </div>
  )
}
