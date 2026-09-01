import { useRef, useState, Children, type ReactNode } from 'react'
import ReactMarkdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import type { MarketChart } from '../../utils/marketChartContract'
import type { MarketTable } from '../../utils/marketTables'
import { reportArtifactsToMarkdown } from '../../utils/reportExport'

// 보고서 응답 구조 (report-integration-guide.html §2): sections[] = 섹션별 {title, text, references, tool_servers}
export interface ReportToolCall {
  plan_step?: number
  tool: string
  args?: unknown
  result?: string
  result_truncated?: string
}
export interface ReportToolServer {
  server: string
  calls: ReportToolCall[]
}
// 본문 [N] 인용이 가리키는 출처 (문서/웹). marker(문자열)가 본문 [N]의 N과 매칭
export interface ReportReference {
  marker: string
  type: 'doc' | 'web'
  title: string
  file_name?: string   // doc일 때 파일명 (뷰어 연결용 — 단, doc_id 없어 PDF 직접 열기는 불가)
  url?: string         // web일 때 링크
}
export interface ReportSection {
  id: string
  title: string
  section_type?: string
  text: string
  references?: ReportReference[]
  tool_servers?: ReportToolServer[]
  tables?: readonly MarketTable[]
  charts?: readonly MarketChart[]
}

// 본문 [N] 인용 마커 (가이드 §3 — (p.N) 없이 숫자만). 예: [6] / [10][11]
const REF_RE = /\[(\d+)\]/g

// 문자열 속 [N]을 references와 매칭해 각주 링크로 변환. web=새 탭 url / doc=하단 목록 앵커 / 미매칭=평문
function refsInString(text: string, keyBase: string, refMap: Map<string, ReportReference>, secId: string): ReactNode[] {
  const nodes: ReactNode[] = []
  let last = 0
  let m: RegExpExecArray | null
  const re = new RegExp(REF_RE.source, 'g')
  while ((m = re.exec(text)) !== null) {
    if (m.index > last) nodes.push(text.slice(last, m.index))
    const marker = m[1]
    const r = refMap.get(marker)
    const key = `${keyBase}-${m.index}`
    if (!r) {
      nodes.push(m[0])   // 매칭 없으면 그대로 (깨지지 않게)
    } else if (r.type === 'web' && r.url) {
      nodes.push(<a key={key} className="ref-cite" href={r.url} target="_blank" rel="noopener noreferrer" title={r.title}>[{marker}]</a>)
    } else {
      const anchor = `ref-${secId}-${marker}`
      nodes.push(
        <a key={key} className="ref-cite" href={`#${anchor}`} title={r.title}
          onClick={e => { e.preventDefault(); document.getElementById(anchor)?.scrollIntoView({ behavior: 'smooth', block: 'center' }) }}
        >[{marker}]</a>
      )
    }
    last = m.index + m[0].length
  }
  if (last < text.length) nodes.push(text.slice(last))
  return nodes
}

// react-markdown children 중 문자열만 각주 변환 (요소 자식은 그대로)
function withRefs(children: ReactNode, refMap: Map<string, ReportReference>, secId: string): ReactNode {
  return Children.toArray(children).flatMap((child, i) =>
    typeof child === 'string' ? refsInString(child, `${secId}-${i}`, refMap, secId) : [child]
  )
}

// 섹션 본문 렌더용 markdown 컴포넌트 (p/li/td 문자열 자식의 [N]을 각주로)
function buildRefComponents(refs: ReportReference[], secId: string): Components {
  const refMap = new Map(refs.map(r => [r.marker, r]))
  const tf = (children: ReactNode) => withRefs(children, refMap, secId)
  return {
    table: ({ children }) => <div className="ai-table-wrap"><table>{children}</table></div>,
    a: ({ href, children }) => <a href={href} target="_blank" rel="noopener noreferrer">{children}</a>,
    p: ({ children }) => <p>{tf(children)}</p>,
    li: ({ children }) => <li>{tf(children)}</li>,
    td: ({ children }) => <td>{tf(children)}</td>,
  }
}

// 섹션 references를 markdown 목록으로 (다운로드 본문에 포함)
function refsToMarkdown(refs: ReportReference[]): string {
  if (!refs.length) return ''
  const lines = refs.map(r => {
    const tail = r.type === 'web' && r.url ? ` (${r.url})` : r.file_name ? ` (${r.file_name})` : ''
    return `- [${r.marker}] ${r.title}${tail}`
  })
  return `**참고문헌**\n\n${lines.join('\n')}`
}

interface Props {
  open: boolean
  loading: boolean
  markdown: string                // sections 없는 구버전 응답용 fallback 본문
  title?: string                  // 보고서 제목 → 최상단 h1
  sections?: ReportSection[]      // 섹션별 본문 + 출처 (있으면 이걸로 렌더/편집)
  defaultFilename: string
  onClose: () => void
  onError?: (msg: string) => void   // PDF 생성 실패 알림
}

function formatArgs(args: unknown): string {
  if (args === undefined || args === null) return '(없음)'
  if (typeof args === 'string') return args
  try { return JSON.stringify(args, null, 2) } catch { return String(args) }
}

// 출처 — 서버 1개 = 토글 버튼 1개. 펼치면 그 서버의 도구 호출들(tool/입력/출력) 표시. (병합 안 함, 응답 그대로)
function ToolServerButton({ server }: { server: ReportToolServer }) {
  const [open, setOpen] = useState(false)
  return (
    <div className="report-srv">
      <button
        type="button"
        className={`report-srv-btn${open ? ' open' : ''}`}
        aria-expanded={open}
        onClick={() => setOpen(o => !o)}
      >
        🔧 {server.server} <span className="cnt">({server.calls.length})</span>
      </button>
      {open && (
        <div className="report-calls">
          {server.calls.map((c, i) => (
            <div className="report-call" key={i}>
              <div className="report-call-name">
                {c.tool}
                {typeof c.plan_step === 'number' && <span className="step">step {c.plan_step}</span>}
              </div>
              <div className="report-call-lbl">입력</div>
              <pre>{formatArgs(c.args)}</pre>
              <div className="report-call-lbl">출력</div>
              <pre>{c.result_truncated || c.result || '(결과 없음)'}</pre>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

const MAX_FILENAME = 50

function escapeHtml(s: string): string {
  return s.replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c] ?? c
  ))
}

// 파일명에서 OS 금지 문자 + 제어문자 제거 (입력 즉시 호출 — 공백/점은 타이핑 중이라 보존)
function stripInvalidFilenameChars(s: string): string {
  // eslint-disable-next-line no-control-regex
  return s.replace(/[\\/:*?"<>|\x00-\x1f]/g, '')
}

// 다운로드 직전 최종 정리 — 금지문자 제거 + 연속 공백 1칸 + 앞뒤 공백·점 제거 + 길이 제한
function finalizeFilename(s: string): string {
  return stripInvalidFilenameChars(s)
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/^\.+/, '')
    .slice(0, MAX_FILENAME)
}

// textarea 높이를 내용에 맞춰 자동 조절 (섹션별 편집창이 내용만큼 늘어나도록)
function autoSize(el: HTMLTextAreaElement | null): void {
  if (!el) return
  el.style.height = 'auto'
  el.style.height = `${el.scrollHeight}px`
}

export default function ReportPreviewModal({ open, loading, markdown, title = '', sections = [], defaultFilename, onClose, onError }: Props) {
  // ★ 섹션을 합치지 않고 배열 그대로 유지 — 편집해도 섹션 경계가 살아있어 출처가 제자리에 붙음.
  //   sections 없으면(구버전) markdown 통째를 단일 유닛으로 처리.
  const hasSections = sections.length > 0
  const units = hasSections
    ? sections.map(s => ({
        id: s.id,
        title: s.title,
        text: s.text,
        references: s.references ?? [],
        // 빈 호출(calls 0개) 서버는 버튼 숨김. 그 외엔 병합 없이 응답 그대로.
        tool_servers: (s.tool_servers ?? []).filter(sv => sv.calls.length > 0),
        tables: s.tables ?? [],
        charts: s.charts ?? [],
      }))
    : [{ id: 'doc', title: '', text: markdown, references: [] as ReportReference[], tool_servers: [] as ReportToolServer[], tables: [] as MarketTable[], charts: [] as MarketChart[] }]

  const [texts, setTexts] = useState<string[]>(() => units.map(u => u.text))
  const [titles, setTitles] = useState<string[]>(() => units.map(u => u.title))  
  const [docTitle, setDocTitle] = useState(title)                                
  const [lastMarkdown, setLastMarkdown] = useState(markdown)
  const [editing, setEditing] = useState(false)
  const [drafts, setDrafts] = useState<string[]>([])           
  const [titleDrafts, setTitleDrafts] = useState<string[]>([]) 
  const [docTitleDraft, setDocTitleDraft] = useState('')       
  const [filename, setFilename] = useState(defaultFilename)
  const [cancelConfirm, setCancelConfirm] = useState(false)
  const [downloadConfirm, setDownloadConfirm] = useState<null | 'md' | 'pdf'>(null)   // 수정 모드 다운로드 확인
  const [downloading, setDownloading] = useState(false)
  const pdfRef = useRef<HTMLDivElement>(null)

  // 새 보고서 도착(markdown 변경) 시 모든 상태 리셋 — Adjust state during render 패턴
  if (markdown !== lastMarkdown) {
    setLastMarkdown(markdown)
    setTexts(units.map(u => u.text))
    setTitles(units.map(u => u.title))
    setDocTitle(title)
    setFilename(stripInvalidFilenameChars(defaultFilename))
    setEditing(false)
    setDrafts([])
    setTitleDrafts([])
    setDocTitleDraft('')
    setCancelConfirm(false)
    setDownloadConfirm(null)
  }

  // 화면/다운로드에 쓸 현재 값 (수정 중이면 초안)
  const curTexts = editing ? drafts : texts
  const curTitles = editing ? titleDrafts : titles
  const curDocTitle = editing ? docTitleDraft : docTitle

  // 섹션 제목·본문을 하나의 markdown으로 합침 (다운로드·인쇄 시점에만) — 편집돼도 배열 그대로라 순서대로 합치면 끝
  const composeBody = (textList: string[], titleList: string[], docT: string): string => {
    if (!hasSections) return textList[0] ?? ''
    const parts: string[] = []
    if (docT.trim()) parts.push(`# ${docT}`)
    units.forEach((u, i) => {
      const ti = (titleList[i] ?? '').trim()
      if (ti) parts.push(`## ${ti}`)
      const t = textList[i] ?? ''
      if (t.trim()) parts.push(t)
      const artifacts = reportArtifactsToMarkdown({ tables: u.tables, charts: u.charts })
      if (artifacts) parts.push(artifacts)
      const refsMd = refsToMarkdown(u.references)   // 다운로드 본문에 섹션 참고문헌 포함
      if (refsMd) parts.push(refsMd)
    })
    return parts.join('\n\n')
  }

  const enterEdit = () => { setDrafts([...texts]); setTitleDrafts([...titles]); setDocTitleDraft(docTitle); setEditing(true) }
  const commitEdit = () => { setTexts([...drafts]); setTitles([...titleDrafts]); setDocTitle(docTitleDraft); setEditing(false) }
  const setDraftAt = (i: number, v: string) => setDrafts(prev => { const n = [...prev]; n[i] = v; return n })
  const setTitleDraftAt = (i: number, v: string) => setTitleDrafts(prev => { const n = [...prev]; n[i] = v; return n })

  // 취소: 수정 중이면 "수정 취소" 확인 모달, 아니면 모달 닫기
  const handleCancel = () => {
    if (editing) { setCancelConfirm(true); return }
    onClose()
  }

  // MD 다운로드 = 현재 본문(수정 중이면 초안)을 합쳐 .md 파일로 저장
  const handleDownloadMd = () => {
    if (loading) return
    const body = composeBody(curTexts, curTitles, curDocTitle)
    const name = finalizeFilename(filename) || finalizeFilename(defaultFilename) || '보고서'
    const blob = new Blob([body], { type: 'text/markdown;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = `${name}.md`
    document.body.appendChild(a)
    a.click()
    document.body.removeChild(a)
    URL.revokeObjectURL(url)
  }

  // PDF 다운로드 = 브라우저 인쇄 엔진으로 PDF 저장.
  // ⚠️ html2canvas(이미지 캡처→슬라이스)는 가로·세로 잘림 고질병 → 폐기. 숨김 iframe에 HTML+@page만 넣고 print().
  const handleDownload = () => {
    if (downloading || loading) return
    // pdfRef는 current(수정 중이면 초안) 미러링이라 innerHTML이 이미 최신 — 편집 상태는 유지(다운로드가 수정 종료시키지 않음)
    const src = pdfRef.current
    if (!src) return
    const name = finalizeFilename(filename) || finalizeFilename(defaultFilename) || '보고서'
    // ⚠️ 크롬 "PDF로 저장" 기본 파일명 = 최상위 document.title (iframe title 아님). 인쇄 동안만 교체 후 복원.
    const prevTitle = document.title
    setDownloading(true)
    try {
      document.title = name
      const iframe = document.createElement('iframe')
      iframe.setAttribute('aria-hidden', 'true')
      iframe.style.position = 'fixed'
      iframe.style.right = '0'
      iframe.style.bottom = '0'
      iframe.style.width = '0'
      iframe.style.height = '0'
      iframe.style.border = '0'
      document.body.appendChild(iframe)
      const win = iframe.contentWindow
      const doc = win?.document
      if (!win || !doc) { if (iframe.parentNode) document.body.removeChild(iframe); throw new Error('iframe document 없음') }

      let cleaned = false
      const cleanup = () => { if (!cleaned) { cleaned = true; document.title = prevTitle; if (iframe.parentNode) document.body.removeChild(iframe) } }
      win.onafterprint = cleanup

      doc.open()
      doc.write(`<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8"><title>${escapeHtml(name)}</title>
<style>
  @page { size: A4; margin: 0; }
  * { box-sizing: border-box; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
  html { margin: 0; padding: 0; }
  body { margin: 0; padding: 16mm 14mm; font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Apple SD Gothic Neo', 'Malgun Gothic', 'Noto Sans KR', sans-serif; color: #060B11; font-size: 14px; line-height: 1.75; word-break: keep-all; }
  h1 { font-size: 26px; font-weight: 700; margin: 0 0 16px; }
  h2 { font-size: 22px; font-weight: 600; margin: 28px 0 12px; }
  h3 { font-size: 18px; font-weight: 600; margin: 22px 0 10px; }
  h4 { font-size: 16px; font-weight: 600; margin: 18px 0 8px; }
  p { margin: 10px 0; }
  ul, ol { margin: 10px 0; padding-left: 22px; }
  li { margin: 6px 0; }
  strong { font-weight: 700; }
  blockquote { margin: 12px 0; padding: 8px 16px; border-left: 3px solid #d0d4da; color: #4F4F58; }
  table { border-collapse: collapse; width: 100%; margin: 12px 0; }
  th, td { border: 1px solid #DFE2E7; padding: 8px 10px; text-align: left; }
  th { background: #F8FAFD; }
  code { background: #F3F4F6; padding: 1px 5px; border-radius: 4px; font-size: 13px; }
  hr { border: 0; border-top: 1px solid #E5E7EB; margin: 20px 0; }
  h1, h2, h3, h4 { break-after: avoid; page-break-after: avoid; }
  li, p, blockquote, tr, img { break-inside: avoid; page-break-inside: avoid; }
  img { max-width: 100%; }
</style></head><body>${src.innerHTML}</body></html>`)
      doc.close()

      win.focus()
      setTimeout(() => { win.print(); setDownloading(false) }, 250)
      setTimeout(cleanup, 60_000)
    } catch {
      document.title = prevTitle
      onError?.('PDF 저장 창을 여는 중 오류가 발생했습니다.\n잠시 후 다시 시도해 주세요.')
      setDownloading(false)
    }
  }

  // 헤더 다운로드 클릭 — 수정 모드면 확인 모달(화면설계서 4-1/4-2), 보기 모드면 바로 다운로드(3-1/3-2)
  const onClickMd = () => { if (editing) setDownloadConfirm('md'); else handleDownloadMd() }
  const onClickPdf = () => { if (editing) setDownloadConfirm('pdf'); else handleDownload() }
  const confirmDownload = () => {
    const which = downloadConfirm
    setDownloadConfirm(null)
    if (which === 'md') handleDownloadMd()
    else if (which === 'pdf') handleDownload()
  }

  return (
    <>
      <div id="modal-report-preview" className={`modal-overlay${open ? ' open' : ''}`}>
        <div className="modal-window">
          <div className="modal-header">
            <div>{editing ? '보고서 수정' : '보고서 요약'}</div>
            <div className="modal-header-actions">
              <button className="btn-download" onClick={onClickMd} disabled={loading}>MD 다운로드</button>
              <button className="btn-download-pdf" onClick={onClickPdf} disabled={loading || downloading}>
                {downloading ? '다운로드 중…' : 'PDF 다운로드'}
              </button>
              <button className="btn-modal-close" onClick={onClose}>닫기</button>
            </div>
          </div>
          <div className="modal-content">
            <div className="editor-wrap">
              {loading ? (
                <div className="report-loading">
                  <div className="fixed-8bar-spinner">
                    {Array.from({ length: 8 }, (_, i) => <div key={i} className={`bar bar${i + 1}`} />)}
                  </div>
                  <div className="report-loading-text">보고서를 생성하고 있습니다…</div>
                </div>
              ) : (
                // ★ 섹션별 렌더: 각 섹션 = 제목 + 본문(보기=markdown / 수정=textarea) + 그 섹션 출처 버튼.
                //   출처가 섹션 안에 묶여 있어 편집해도 위치 안 움직임.
                <div className="ai-content report-view scroll-container">
                  {hasSections && (editing ? (
                    <input
                      className="report-title-editor"
                      value={docTitleDraft}
                      onChange={e => setDocTitleDraft(e.target.value)}
                      placeholder="보고서 제목"
                    />
                  ) : docTitle.trim() && <h1>{docTitle}</h1>)}
                  {units.map((u, i) => (
                    <section className="report-section" key={i}>
                      {editing ? (
                        <input
                          className="report-sec-title-editor"
                          value={titleDrafts[i] ?? ''}
                          onChange={e => setTitleDraftAt(i, e.target.value)}
                          placeholder="섹션 제목"
                        />
                      ) : (titles[i]?.trim() && <h2>{titles[i]}</h2>)}
                      {editing ? (
                        <textarea
                          className="report-sec-editor"
                          ref={autoSize}
                          value={drafts[i] ?? ''}
                          onChange={e => { setDraftAt(i, e.target.value); autoSize(e.target) }}
                          placeholder="이 섹션 내용을 수정해 주세요."
                        />
                      ) : (
                        <ReactMarkdown remarkPlugins={[remarkGfm]} components={buildRefComponents(u.references, u.id)}>{texts[i] ?? ''}</ReactMarkdown>
                      )}
                      {/* 참고문헌 목록 (본문 [N] 앵커 대상) — doc: 📄 title(file_name) / web: 🔗 title(링크) */}
                      {u.references.length > 0 && (
                        <div className="report-refs">
                          <div className="report-refs-label">참고문헌</div>
                          <ol>
                            {u.references.map(r => (
                              <li key={r.marker} id={`ref-${u.id}-${r.marker}`} className="report-ref-item">
                                <span className="report-ref-marker">[{r.marker}]</span>
                                {r.type === 'web' && r.url
                                  ? <a href={r.url} target="_blank" rel="noopener noreferrer">🔗 {r.title}</a>
                                  : <span>📄 {r.title}{r.file_name ? ` (${r.file_name})` : ''}</span>}
                              </li>
                            ))}
                          </ol>
                        </div>
                      )}
                      {u.tool_servers.length > 0 && (
                        <div className="report-srcs">
                          <div className="report-srcs-label">도구 실행 내역</div>
                          {u.tool_servers.map((srv, j) => (
                            <ToolServerButton key={`${srv.server}-${j}`} server={srv} />
                          ))}
                        </div>
                      )}
                    </section>
                  ))}
                </div>
              )}
            </div>
            <div className="input-group">
              <label htmlFor="report-filename" className="input-label">파일명</label>
              <div className="input-wrap">
                <input
                  id="report-filename"
                  type="text"
                  className="input-field"
                  maxLength={MAX_FILENAME}
                  value={filename}
                  disabled={loading}
                  onChange={e => setFilename(stripInvalidFilenameChars(e.target.value))}
                />
                <span className="char-count">{filename.length}/{MAX_FILENAME}</span>
              </div>
            </div>
          </div>
          <div className="modal-footer">
            {editing ? (
              <>
                <button className="btn-cancel" onClick={handleCancel}>취소</button>
                <button className="btn-edit" onClick={commitEdit} disabled={loading}>저장</button>
              </>
            ) : (
              <button className="btn-edit" onClick={enterEdit} disabled={loading || downloading}>수정</button>
            )}
          </div>
        </div>
      </div>

      {/* 인쇄용 본문 HTML 소스 — 현재 본문(수정 중이면 초안)을 합쳐 미러링. 다운로드 시 innerHTML을 인쇄 iframe으로 복사 */}
      <div ref={pdfRef} aria-hidden className="report-pdf-capture ai-content">
        <ReactMarkdown remarkPlugins={[remarkGfm]}>{composeBody(curTexts, curTitles, curDocTitle)}</ReactMarkdown>
      </div>

      {/* 수정 취소 확인 모달 — 보고서 미리보기 위에 표시 (z-index 더 높음) */}
      <div id="modal-report-edit-cancel" className={`modal-overlay${cancelConfirm ? ' open' : ''}`}>
        <div className="modal-window">
          <div className="modal-content">
            <div className="cancel-description" style={{ whiteSpace: 'pre-line' }}>
              {'수정 중인 내용은 저장되지 않습니다.\n취소하시겠습니까?'}
            </div>
          </div>
          <div className="modal-footer">
            <button className="btn-cancel" onClick={() => setCancelConfirm(false)}>취소</button>
            <button className="btn-confirm" onClick={() => { setEditing(false); setDrafts([]); setTitleDrafts([]); setDocTitleDraft(''); setCancelConfirm(false) }}>확인</button>
          </div>
        </div>
      </div>

      {/* 다운로드 확인 모달 (수정 모드 다운로드 시 — 화면설계서 4-1/4-2) */}
      <div id="modal-report-download-confirm" className={`modal-overlay${downloadConfirm ? ' open' : ''}`}>
        <div className="modal-window">
          <div className="modal-content">
            <div className="cancel-description" style={{ whiteSpace: 'pre-line' }}>
              {'현재 내용으로 보고서 파일을\n다운로드하시겠습니까?'}
            </div>
          </div>
          <div className="modal-footer">
            <button className="btn-cancel" onClick={() => setDownloadConfirm(null)}>취소</button>
            <button className="btn-confirm" onClick={confirmDownload}>확인</button>
          </div>
        </div>
      </div>
    </>
  )
}
