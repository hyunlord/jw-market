import { useEffect, useState } from 'react'
import {
  describeUploadStage,
  isFailedUploadState,
  summarizeUploadFiles,
  UPLOAD_STATE_LABELS,
  type UploadProgress,
  type UploadProgressFile,
} from '../../utils/uploadProgress.ts'
import UploadPreviewNotice from './UploadPreviewNotice.ts'

interface Props {
  progress: UploadProgress
  onRetryStatus: () => void
  onRetryUpload: () => void
}

function formatElapsed(startedAtMs: number, nowMs: number): string {
  const seconds = Math.max(0, Math.floor((nowMs - startedAtMs) / 1_000))
  const minutes = Math.floor(seconds / 60)
  const remainder = seconds % 60
  return minutes > 0 ? `${minutes}분 ${remainder}초` : `${remainder}초`
}

function UploadStageLine({ file }: { file: UploadProgressFile }) {
  const stage = describeUploadStage(file)
  if (!stage) return null
  return (
    <span className="upload-progress-file-stage">
      {stage.percent === null ? stage.label : `${stage.label} ${stage.percent}%`}
    </span>
  )
}

export default function UploadProgressList({ progress, onRetryStatus, onRetryUpload }: Props) {
  const [nowMs, setNowMs] = useState(() => Date.now())

  useEffect(() => {
    if (progress.phase === 'transferring') return
    const timer = window.setInterval(() => setNowMs(Date.now()), 1_000)
    return () => window.clearInterval(timer)
  }, [progress.phase])

  if (progress.phase === 'transferring') {
    const label = progress.percent === null ? '파일 전송 중' : `파일 전송 중 ${progress.percent}%`
    return (
      <section className="upload-progress" aria-label="파일 업로드 진행 상황">
        <div className="upload-progress-summary">
          <strong>{label}</strong>
          <span>{progress.fileNames.length}개 파일</span>
        </div>
        <div
          className={`upload-transfer-track${progress.percent === null ? ' is-indeterminate' : ''}`}
          role="progressbar"
          aria-label="파일 전송률"
          aria-valuemin={0}
          aria-valuemax={100}
          aria-valuenow={progress.percent ?? undefined}
        >
          {progress.percent !== null && <span style={{ width: `${progress.percent}%` }} />}
        </div>
      </section>
    )
  }

  const summary = summarizeUploadFiles(progress.files)
  const statusUnavailable = progress.phase === 'status-unavailable'
  return (
    <section className="upload-progress" aria-label="파일 처리 진행 상황">
      <div className="upload-progress-summary">
        <div>
          <strong>{statusUnavailable ? '상태를 확인하지 못했습니다.' : UPLOAD_STATE_LABELS[progress.state]}</strong>
          <span>
            {summary.readyCount}/{summary.totalCount} 완료
            {summary.failedCount > 0 ? ` · ${summary.failedCount}개 실패` : ''}
          </span>
        </div>
        <span className="upload-elapsed">{formatElapsed(progress.startedAtMs, nowMs)}</span>
      </div>
      {statusUnavailable && (
        <button type="button" className="upload-progress-action" onClick={onRetryStatus}>다시 확인</button>
      )}
      <ul className="upload-progress-files">
        {progress.files.slice(0, 10).map(file => (
          <li key={file.fileName} className={isFailedUploadState(file.state) ? 'is-failed' : ''}>
            <div className="upload-progress-file-copy">
              <span className="upload-progress-file-name">{file.fileName}</span>
              <span className="upload-progress-file-state">{UPLOAD_STATE_LABELS[file.state]}</span>
              <UploadStageLine file={file} />
              {isFailedUploadState(file.state) && file.message && (
                <span className="upload-progress-file-message">{file.message}</span>
              )}
              <UploadPreviewNotice file={file} />
            </div>
            {isFailedUploadState(file.state) && (
              <button type="button" className="upload-progress-action" onClick={onRetryUpload}>다시 업로드</button>
            )}
          </li>
        ))}
      </ul>
    </section>
  )
}
