// 입력창 상단 파일 첨부 프리뷰 리스트 (퍼블 file-add-wrap) — 챗봇·전체화면 공용.
import type { PendingDoc } from '../../utils/useMarketDocuments'
import type { UploadProgress } from '../../utils/uploadProgress.ts'
import UploadProgressList from './UploadProgressList'

interface Props {
  docs: PendingDoc[]
  uploadProgress: UploadProgress | null
  onRemove: (documentId: number) => void
  onRetryStatus: () => void
  onRetryUpload: () => void
}

export default function FilePreviewList({ docs, uploadProgress, onRemove, onRetryStatus, onRetryUpload }: Props) {
  if (docs.length === 0 && uploadProgress === null) return null
  return (
    <div className="file-add-wrap">
      {docs.map(doc => {
        const dotExt = doc.ext ? `.${doc.ext}` : ''
        const base = dotExt && doc.fileName.toLowerCase().endsWith(dotExt.toLowerCase())
          ? doc.fileName.slice(0, -dotExt.length)
          : doc.fileName
        return (
          <div className="bx-file-name" key={doc.documentId}>
            <div className="tx-file-name">{base}</div>
            {dotExt && <div className="tx-file-type">{dotExt}</div>}
            <div className="btn-close-file-name" onClick={() => onRemove(doc.documentId)} />
          </div>
        )
      })}
      {uploadProgress && (
        <UploadProgressList
          progress={uploadProgress}
          onRetryStatus={onRetryStatus}
          onRetryUpload={onRetryUpload}
        />
      )}
    </div>
  )
}
