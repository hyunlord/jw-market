import { useEffect, useState } from 'react'
import { deleteMarketDocument, fetchMarketDocuments, DOC_ALERT, type MarketDoc } from '../../utils/marketDocuments'
import { attachmentListView, type AttachmentListLoadState } from '../../utils/attachmentListState'

interface Props {
  onClose: () => void
  appSessionId: string | null
  docIdByName: Map<string, number>
  onAlert?: (msg: string) => void
  // true면 전체화면 모달이 아니라 트리거 버튼 아래 탑다운 드롭다운으로 표시 (TopNavigation 첨부 버튼용)
  asDropdown?: boolean
}

function fmtKb(bytes: number): string {
  return `${Math.max(1, Math.round(bytes / 1024)).toLocaleString()} KB`
}

// 업로드 일시 → 'yyyy-mm-dd HH:mm' (로컬 시간). 파싱 실패 시 빈 문자열.
function fmtDateTime(iso: string): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return ''
  const p = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`
}

function splitName(name: string): { nm: string; ext: string } {
  const i = name.lastIndexOf('.')
  if (i <= 0) return { nm: name, ext: '' }
  return { nm: name.slice(0, i), ext: name.slice(i) }
}

export default function AttachmentListPopup({ onClose, appSessionId, docIdByName, onAlert, asDropdown = false }: Props) {
  const [loadState, setLoadState] = useState<AttachmentListLoadState>({ kind: 'loading' })
  const [deletingIdx, setDeletingIdx] = useState<number | null>(null)  // 동일 파일명 중복 대비해 인덱스로 식별
  const [loadAttempt, setLoadAttempt] = useState(0)

  useEffect(() => {
    if (!appSessionId) return undefined
    let cancelled = false
    fetchMarketDocuments(appSessionId)
      .then(documents => { if (!cancelled) setLoadState({ kind: 'ready', documents }) })
      .catch(error => { if (!cancelled) setLoadState({ kind: 'failed', error }) })
    return () => { cancelled = true }
  }, [appSessionId, loadAttempt])

  const retryLoad = () => {
    setLoadState({ kind: 'loading' })
    setLoadAttempt(attempt => attempt + 1)
  }

  const resolveId = (d: MarketDoc) => d.document_id || docIdByName.get(d.file_name) || 0

  const handleDelete = async (d: MarketDoc, idx: number) => {
    if (!appSessionId || deletingIdx !== null) return
    const id = resolveId(d)
    if (!id) { onAlert?.('이 파일의 식별자를 찾을 수 없어 삭제할 수 없습니다.'); return }
    setDeletingIdx(idx)
    const ok = await deleteMarketDocument(appSessionId, id)
    setDeletingIdx(null)
    if (ok) setLoadState(prev => prev.kind === 'ready' ? { kind: 'ready', documents: prev.documents.filter((_, i) => i !== idx) } : prev)
    else onAlert?.(DOC_ALERT.uploadFail.replace('업로드', '삭제'))
  }

  const effectiveLoadState: AttachmentListLoadState = appSessionId
    ? loadState
    : { kind: 'ready', documents: [] }
  const view = attachmentListView(effectiveLoadState)
  const list = effectiveLoadState.kind === 'ready' ? effectiveLoadState.documents : []

  return (
    <div
      className={`attach-list-pop${asDropdown ? ' attach-list-dropdown' : ''}`}
      onMouseDown={asDropdown ? undefined : e => { if (e.target === e.currentTarget) onClose() }}
    >
      <div className="attach-list-inner">
        <div className="attach-list-head">
          <span className="attach-list-title">첨부파일 목록</span>
          <button type="button" className="attach-list-close" title="닫기" onClick={onClose} />
        </div>
        <div className="attach-list-body">
          {view.kind === 'loading' ? (
            <ul className="attach-list">
              {Array.from({ length: 3 }, (_, i) => (
                <li key={i} className="attach-item is-skeleton">
                  <div className="attach-file">
                    <span className="sk sk-icon" />
                    <div className="attach-file-text">
                      <div className="sk sk-bar sk-name" />
                      <div className="sk sk-bar sk-size" />
                    </div>
                  </div>
                </li>
              ))}
            </ul>
          ) : view.kind === 'failed' ? (
            <div className="attach-list-error" role="alert">
              <div>{view.message}</div>
              <button type="button" onClick={retryLoad}>다시 시도</button>
            </div>
          ) : view.kind === 'empty' ? (
            <div className="attach-list-empty">{view.message}</div>
          ) : (
            <ul className="attach-list">
              {list.map((d, i) => {
                const { nm, ext } = splitName(d.file_name)
                return (
                  <li key={`${d.file_name}__${d.uploaded_at}__${i}`} className={`attach-item${deletingIdx === i ? ' is-deleting' : ''}`}>
                    <div className="attach-file">
                      <span className="attach-file-icon" />
                      <div className="attach-file-text">
                        <div className="attach-file-name">
                          <span className="attach-file-nm">{nm}</span>
                          {ext && <span className="attach-file-ext">{ext}</span>}
                          <span className="attach-file-size">({fmtKb(d.file_size_bytes)})</span>
                        </div>
                        {d.uploaded_at && (
                          <div className="attach-file-date">첨부일시: {fmtDateTime(d.uploaded_at)}</div>
                        )}
                      </div>
                    </div>
                    <button
                      type="button"
                      className="attach-del"
                      title="삭제"
                      disabled={deletingIdx === i}
                      onClick={() => handleDelete(d, i)}
                    />
                  </li>
                )
              })}
            </ul>
          )}
        </div>
      </div>
    </div>
  )
}
