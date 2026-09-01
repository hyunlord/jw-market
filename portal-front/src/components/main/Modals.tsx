import { useState } from 'react'
import ReportPreviewModal, { type ReportSection } from './ReportPreviewModal'

// MCP 도구 호출 상세 — Panel에서 도구 li 클릭 시 viewReportModal에 표시할 데이터
// visible_used_tools.content.used_tools[] 한 항목 기준 (test.md 확정)
export interface ToolCallDetail {
  mcpName: string   // 카드 제목 = content.mcp_server (예: 'biomcp-mcp-server')
  step: number      // used_tools[].step (1부터)
  tool: string      // 도구 이름 = used_tools[].tool (예: 'search_articles')
  input: unknown    // used_tools[].args
  output: unknown   // used_tools[].result (객체 / "[tool error]" / JSON string / null)
}

// 모든 props optional — 호출하는 페이지에서 필요한 모달만 활성화 (예: LoginPage는 alertMessage만 사용)
interface Props {
  deleteModal?: boolean
  changeNameModal?: boolean
  viewReportModal?: boolean
  reportCancelModal?: boolean
  bulkDeleteModal?: boolean
  alertMessage?: string | null   // 값 있으면 알림 모달 open, null/'' 이면 닫힘
  agentSelectModal?: boolean      // RND·MARKET 둘 다 가진 사용자 로그인 시 이동 에이전트 선택 모달
  chatTitle?: string
  toolCallDetail?: ToolCallDetail | null   // viewReportModal에 표시할 도구 input/output
  reportPreviewOpen?: boolean
  reportLoading?: boolean
  reportMarkdown?: string
  reportTitle?: string
  reportSections?: ReportSection[]
  reportFilename?: string
  onCloseReportPreview?: () => void
  onReportError?: (msg: string) => void
  // 보고서 "전체 적용" 확인 모달 (기획서 3-1 — 선택 0개 + 전체 ≤10개)
  reportAllConfirmModal?: boolean
  onCloseReportAllConfirm?: () => void
  onConfirmReportAll?: () => void
  onCloseDelete?: () => void
  onConfirmDelete?: () => void
  onCloseChangeName?: () => void
  onConfirmChangeName?: (title: string) => void
  onCloseViewReport?: () => void
  onCloseReportCancel?: () => void
  onReportCancel?: () => void
  onCloseBulkDelete?: () => void
  onConfirmBulkDelete?: () => void
  onCloseAlert?: () => void
  onSelectRnd?: () => void
  onSelectMarket?: () => void
}

// unknown 값을 JSON 문자열로 안전 변환 (이미 string이면 JSON 파싱 시도 → 실패 시 raw)
function safeStringify(v: unknown): string {
  if (v === undefined || v === null) return '(없음)'
  if (typeof v === 'string') {
    // text 필드가 JSON 직렬화 형태일 수 있음 → 파싱 시도해서 pretty
    try { return JSON.stringify(JSON.parse(v), null, 2) } catch { return v }
  }
  try { return JSON.stringify(v, null, 2) } catch { return String(v) }
}

// 객체/배열을 보기 좋게 문자열로. ⚠️ 핵심: 문자열 배열(또는 [{text}])은 JSON.stringify 하면
// 내부 줄바꿈이 \n으로 이스케이프돼 글자로 보임 → 문자열은 raw로 이어붙여 실제 줄바꿈 보존.
function stringifyResult(v: unknown): string {
  if (Array.isArray(v) && v.length > 0) {
    // ["...\n...", ...] — 문자열 배열은 줄바꿈으로 이어붙여 raw 출력
    if (v.every(el => typeof el === 'string')) return (v as string[]).join('\n')
    // [{text}, ...] — text만 추출해 이어붙임
    if (v.every(el => el != null && typeof el === 'object' && typeof (el as { text?: unknown }).text === 'string')) {
      return (v as { text: string }[]).map(el => el.text).join('\n')
    }
  }
  try { return JSON.stringify(v, null, 2) } catch { return String(v) }
}

// used_tools[].result 안전 포맷 (test.md §3-4). result는 아래 형태로 옴:
//   - 객체/객체배열 (정상)        → JSON pretty-print
//   - 문자열 배열 / [{text}]       → 줄바꿈 보존 raw (위 stringifyResult)
//   - "[tool error]" 등 짧은 에러  → 에러 표시 + 원문
//   - JSON string                 → parse 성공 시 정리, 실패 시 raw
//   - null / "" / undefined        → "(결과 없음)"
function formatToolResult(result: unknown): { isError: boolean; body: string } {
  if (result === undefined || result === null || result === '') {
    return { isError: false, body: '(결과 없음)' }
  }
  if (typeof result === 'string') {
    const t = result.trim()
    // 짧은 에러 마커만 에러 처리 — "[tool error]" 등 (긴 JSON 배열 string 오탐 방지 위해 길이 제한)
    if (t.length <= 100 && t.startsWith('[') && t.toLowerCase().includes('error')) {
      return { isError: true, body: result }
    }
    // JSON string이면 파싱해서 정리, 아니면 raw 그대로
    try {
      const parsed = JSON.parse(t)
      if (parsed && typeof parsed === 'object') return { isError: false, body: stringifyResult(parsed) }
    } catch { /* raw */ }
    return { isError: false, body: result }
  }
  // 이미 객체/배열
  return { isError: false, body: stringifyResult(result) }
}

const noop = () => {}

export default function Modals({
  deleteModal = false, changeNameModal = false, viewReportModal = false,
  reportCancelModal = false, bulkDeleteModal = false,
  alertMessage = null,
  agentSelectModal = false,
  chatTitle = '',
  toolCallDetail = null,
  reportPreviewOpen = false, reportLoading = false, reportMarkdown = '', reportTitle = '', reportSections = [], reportFilename = '',
  onCloseReportPreview = noop, onReportError = noop,
  reportAllConfirmModal = false, onCloseReportAllConfirm = noop, onConfirmReportAll = noop,
  onCloseDelete = noop, onConfirmDelete = noop,
  onCloseChangeName = noop, onConfirmChangeName = noop,
  onCloseViewReport = noop, onCloseReportCancel = noop, onReportCancel: _onReportCancel = noop,
  onCloseBulkDelete = noop, onConfirmBulkDelete = noop,
  onCloseAlert = noop,
  onSelectRnd = noop, onSelectMarket = noop,
}: Props) {
  const [changeName, setChangeName] = useState(chatTitle)
  const [lastChatTitle, setLastChatTitle] = useState(chatTitle)
  const MAX = 20

  // chatTitle prop이 바뀌면 입력 state도 동기화 — Adjust state during render 패턴
  // (useEffect 사용 시 `react-hooks/set-state-in-effect` 에러 + 한 번 더 렌더링됨)
  if (chatTitle !== lastChatTitle) {
    setLastChatTitle(chatTitle)
    setChangeName(chatTitle)
  }

  return (
    <>
      {/* 삭제 모달 */}
      <div id="modal-delete" className={`modal-overlay${deleteModal ? ' open' : ''}`}>
        <div className="modal-window">
          <div className="modal-content">
            <dl>
              <dt>해당 히스토리를 삭제하시겠습니까?</dt>
              <dd>삭제된 채팅은 복구할 수 없습니다.</dd>
            </dl>
          </div>
          <div className="modal-footer">
            <button className="btn-cancel" onClick={onCloseDelete}>취소</button>
            <button className="btn-confirm" onClick={onConfirmDelete}>확인</button>
          </div>
        </div>
      </div>

      {/* 선택삭제 모달 */}
      <div id="modal-bulk-delete" className={`modal-overlay${bulkDeleteModal ? ' open' : ''}`}>
        <div className="modal-window">
          <div className="modal-content">
            <dl>
              <dt>선택된 목록을 삭제하시겠습니까?</dt>
              <dd>삭제된 채팅은 복구할 수 없습니다.</dd>
            </dl>
          </div>
          <div className="modal-footer">
            <button className="btn-cancel" onClick={onCloseBulkDelete}>취소</button>
            <button className="btn-confirm" onClick={onConfirmBulkDelete}>확인</button>
          </div>
        </div>
      </div>

      {/* 이름 변경 모달 */}
      <div id="modal-change-name" className={`modal-overlay${changeNameModal ? ' open' : ''}`}>
        <div className="modal-window">
          <div className="modal-header">
            <div>채팅 이름 변경</div>
            <button className="btn-modal-close" onClick={onCloseChangeName}>닫기</button>
          </div>
          <div className="modal-content">
            <div>
              <input
                type="text"
                className="input-change"
                value={changeName}
                maxLength={MAX}
                onChange={e => setChangeName(e.target.value.slice(0, MAX))}
              />
              <div className="btm-text-number">{changeName.length}/{MAX}</div>
            </div>
          </div>
          <div className="modal-footer">
            <button className="btn-cancel" onClick={onCloseChangeName}>취소</button>
            <button
              className={`btn-change${changeName.trim() === '' || changeName === chatTitle ? ' dim' : ''}`}
              onClick={() => { if (changeName.trim() && changeName !== chatTitle) onConfirmChangeName(changeName) }}
            >
              이름 변경
            </button>
          </div>
        </div>
      </div>

      {/* 보고서 미리보기 모달 — MCP 도구 클릭 시 toolInput/toolOutput 표시 (퍼블 #modal-tool-view 디자인) */}
      <div
        id="modal-view-report"
        className={`modal-overlay${viewReportModal ? ' open' : ''}`}
        onClick={e => { if (e.target === e.currentTarget) onCloseViewReport() }}
      >
        <div className="modal-window">
          <div className="modal-header">
            <div>도구 실행 결과</div>
            <button className="btn-modal-close" onClick={onCloseViewReport}>닫기</button>
          </div>
          <div className="modal-content scroll-container">
            <div className="inner-title">
              {toolCallDetail ? toolCallDetail.mcpName : ''}
            </div>
            <div className="inner-s-title">Input</div>
            <div className="code-wrap-ty01">
              <div
                className="code-box scroll-container"
                style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-all', fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace' }}
              >
                {toolCallDetail ? safeStringify(toolCallDetail.input) : ''}
              </div>
            </div>
            <div className="inner-s-title">Output</div>
            <div className="code-wrap-ty02">
              {(() => {
                if (!toolCallDetail) return <div className="code-box scroll-container" />
                const { isError, body } = formatToolResult(toolCallDetail.output)
                return (
                  <div
                    className="code-box scroll-container"
                    style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-all', fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace', color: isError ? '#FF6B6B' : undefined }}
                  >
                    {isError ? `⚠️ ${body}` : body}
                  </div>
                )
              })()}
            </div>
          </div>
        </div>
      </div>

      {/* 수정 취소 확인 모달 */}
      <div id="modal-report-cancel" className={`modal-overlay${reportCancelModal ? ' open' : ''}`}>
        <div className="modal-window">
          <div className="modal-content">
            <div className="cancel-description">수정을 취소하시겠습니까?</div>
          </div>
          <div className="modal-footer">
            <button className="btn-cancel" onClick={onCloseReportCancel}>취소</button>
            <button className="btn-confirm" onClick={onCloseReportCancel}>확인</button>
          </div>
        </div>
      </div>

      {/* 알림 모달 — 확인 버튼만 (LoginPage 로그인 실패 등 공용) */}
      <div id="modal-login-alert" className={`modal-overlay${alertMessage ? ' open' : ''}`}>
        <div className="modal-window">
          <div className="modal-content">
            <dl>
              <dt style={{ whiteSpace: 'pre-line' }}>{alertMessage}</dt>
            </dl>
          </div>
          <div className="modal-footer">
            <button className="btn-confirm" onClick={onCloseAlert}>확인</button>
          </div>
        </div>
      </div>

      {/* 에이전트 선택 모달 — RND·MARKET 권한 둘 다 가진 사용자 로그인 시 (이동 전 선택, 닫기/취소 없음) */}
      <div id="modal-agent-select" className={`modal-overlay${agentSelectModal ? ' open' : ''}`}>
        <div className="modal-window">
          <div className="modal-content">
            <dl>
              <dt style={{ whiteSpace: 'pre-line' }}>{'이동할 에이전트를 선택해 주세요.\n버튼을 클릭하면 해당 화면으로 이동합니다.'}</dt>
            </dl>
          </div>
          <div className="modal-footer">
            <button className="btn-confirm" onClick={onSelectRnd}>신약 R&D</button>
            <button className="btn-confirm" onClick={onSelectMarket}>시장분석</button>
          </div>
        </div>
      </div>

      {/* 보고서 "전체 적용" 확인 모달*/}
      <div id="modal-report-all-confirm" className={`modal-overlay${reportAllConfirmModal ? ' open' : ''}`}>
        <div className="modal-window">
          <div className="modal-content">
            <dl>
              <dd>선택된 내용이 없습니다.<br />채팅 내 모든 AI 분석 결과를<br />보고서에 적용하시겠습니까?</dd>
            </dl>
          </div>
          <div className="modal-footer">
            <button className="btn-cancel" onClick={onCloseReportAllConfirm}>취소</button>
            <button className="btn-confirm" onClick={onConfirmReportAll}>확인</button>
          </div>
        </div>
      </div>

      <ReportPreviewModal
        open={reportPreviewOpen}
        loading={reportLoading}
        markdown={reportMarkdown}
        title={reportTitle}
        sections={reportSections}
        defaultFilename={reportFilename}
        onClose={onCloseReportPreview}
        onError={onReportError}
      />
    </>
  )
}
