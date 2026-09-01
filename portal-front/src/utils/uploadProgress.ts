import type { UploadState, UploadStatus, UploadStatusFile } from './marketDocuments.ts'

export const UPLOAD_STATE_LABELS: Readonly<Record<UploadState, string>> = {
  accepted: '처리 준비 중',
  preprocessing: '파일 처리 중',
  committing: '검색 준비 중',
  ready: '완료',
  blocked: '처리할 수 없음',
  failed: '처리 실패',
  interrupted: '처리 중단',
  expired: '상태 확인 만료',
  unknown: '상태 미상',
}

const TERMINAL_UPLOAD_STATES = new Set<UploadState>([
  'ready', 'blocked', 'failed', 'interrupted', 'expired',
])
const FAILED_UPLOAD_STATES = new Set<UploadState>([
  'blocked', 'failed', 'interrupted', 'expired',
])

export interface UploadPhase {
  name: string
  state: string
  processed: number | null
  total: number | null
  unit: string
}

export interface UploadProgressFile {
  fileName: string
  state: UploadState
  message?: string | null
  queryReady?: boolean
  indexedPages?: number | null
  totalPages?: number | null
  phases?: UploadPhase[] | null
}

/** User-facing label per backend stage. Internal vocabulary (chunk, page index,
 *  embedding) never reaches the screen. */
const STAGE_LABELS: Readonly<Record<string, string>> = {
  extract: '문서 읽는 중',
  embed: '검색 색인 만드는 중',
}

export interface UploadStageView {
  label: string
  /** Percent for THIS stage only, or null when the stage publishes no counter.
   *  Never derived from elapsed time or any other estimate. */
  percent: number | null
}

/**
 * Pick the stage to show: the first one still running. A stage without both
 * integers renders as a label with no percentage -- never as 0%.
 */
export function describeUploadStage(file: UploadProgressFile): UploadStageView | null {
  const phases = file.phases
  if (!phases || phases.length === 0) return null
  const active = phases.find(phase => phase.state === 'running')
    ?? phases.find(phase => phase.state !== 'done')
  if (!active) return null
  const label = STAGE_LABELS[active.name]
  if (!label) return null
  if (active.processed === null || active.total === null || active.total <= 0) {
    return { label, percent: null }
  }
  const percent = Math.min(100, Math.max(0, Math.round((active.processed / active.total) * 100)))
  return { label, percent }
}

export interface UploadFileSummary {
  totalCount: number
  readyCount: number
  failedCount: number
}

export interface TransferUploadProgress {
  phase: 'transferring'
  fileNames: string[]
  startedAtMs: number
  loadedBytes: number
  totalBytes: number | null
  percent: number | null
}

export interface ProcessingUploadProgress {
  phase: 'processing'
  uploadId: string
  state: UploadState
  files: UploadProgressFile[]
  startedAtMs: number
}

export interface StatusUnavailableUploadProgress extends Omit<ProcessingUploadProgress, 'phase'> {
  phase: 'status-unavailable'
}

export type UploadProgress = TransferUploadProgress | ProcessingUploadProgress | StatusUnavailableUploadProgress

export function isTerminalUploadState(state: UploadState): boolean {
  return TERMINAL_UPLOAD_STATES.has(state)
}

export function isFailedUploadState(state: UploadState): boolean {
  return FAILED_UPLOAD_STATES.has(state)
}

export function calculateTransferPercent(loadedBytes: number, totalBytes: number | null): number | null {
  if (totalBytes === null || totalBytes <= 0) return null
  return Math.min(100, Math.max(0, Math.round((loadedBytes / totalBytes) * 100)))
}

export function createTransferProgress(
  fileNames: readonly string[],
  startedAtMs: number,
  loadedBytes: number,
  totalBytes: number | null,
): TransferUploadProgress {
  return {
    phase: 'transferring',
    fileNames: [...fileNames],
    startedAtMs,
    loadedBytes,
    totalBytes,
    percent: calculateTransferPercent(loadedBytes, totalBytes),
  }
}

export function createProcessingFiles(
  status: { state: UploadState; files: UploadStatusFile[] },
  fallbackNames: readonly string[],
): UploadProgressFile[] {
  if (status.files.length === 0) {
    return fallbackNames.map(fileName => ({ fileName, state: status.state }))
  }

  return status.files.map(file => ({
    fileName: file.file_name,
    state: file.state === 'unknown'
      ? 'unknown'
      : isTerminalUploadState(status.state) && !isTerminalUploadState(file.state)
      ? status.state
      : file.state,
    message: file.message,
    ...(file.query_ready !== undefined ? { queryReady: file.query_ready } : {}),
    ...(file.indexed_pages !== undefined ? { indexedPages: file.indexed_pages } : {}),
    ...(file.total_pages !== undefined ? { totalPages: file.total_pages } : {}),
    ...(file.phases !== undefined ? { phases: file.phases } : {}),
  }))
}

export function summarizeUploadFiles(files: readonly UploadProgressFile[]): UploadFileSummary {
  return files.reduce<UploadFileSummary>((summary, file) => ({
    totalCount: summary.totalCount + 1,
    readyCount: summary.readyCount + (file.state === 'ready' ? 1 : 0),
    failedCount: summary.failedCount + (isFailedUploadState(file.state) ? 1 : 0),
  }), { totalCount: 0, readyCount: 0, failedCount: 0 })
}

export function createProcessingProgress(
  status: UploadStatus,
  fallbackNames: readonly string[],
  startedAtMs: number,
): ProcessingUploadProgress {
  return {
    phase: 'processing',
    uploadId: status.uploadId,
    state: status.state,
    files: createProcessingFiles(status, fallbackNames),
    startedAtMs,
  }
}

export function createStatusUnavailableProgress(
  progress: ProcessingUploadProgress,
): StatusUnavailableUploadProgress {
  return { ...progress, phase: 'status-unavailable' }
}
