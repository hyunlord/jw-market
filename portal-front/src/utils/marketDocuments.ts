import { apiFetch, apiUploadFormData, type ApiUploadProgress } from './apiFetch.ts'
import { createSingleFlight } from './marketSessionLoad.ts'
import { getRuntimeConfig } from '../config/runtimeConfig.ts'
import { DocumentListHttpError } from './marketDocumentListPolicy.ts'
import { isTerminalUploadState } from './uploadProgress.ts'

const DOC_API = {
  list: '/api/v1/market/chat/document',
  upload: '/api/v1/market/chat/document/upload',
  uploadStatus: '/api/v1/market/chat/document/upload/status',
  delete: '/api/v1/market/chat/document/delete',
  health: '/api/v1/market/chat/document/health',
} as const

const VDB_ID = 139

export const ALLOWED_EXT = ['pdf', 'pptx', 'docx', 'xlsx'] as const
export const MAX_SESSION_MB = 100
export const UPLOAD_TIMEOUT_MS = 180_000 

export const DOC_ALERT = {
  unsupported: '지원하지 않는 파일 유형이 포함되어 있습니다.\n확인 후 다시 시도해 주세요.\n(지원 파일 유형 : pdf, pptx, docx, xlsx)',
  overSize: '파일은 최대 100MB까지\n업로드 할 수 있습니다.\n확인 후 다시 시도해 주세요.',
  uploadFail: '파일 업로드 중 문제가 발생했습니다.\n잠시 후 다시 시도해 주세요.',
  uploadCompleteListFail: '파일 업로드는 완료되었지만 문서 목록을 불러오지 못했습니다.',
} as const

// §9-B documents[i] — 화면 표시 + 삭제(document_id)에 쓰는 필드만 (전체 실측 필드는 §9-B 참조)
// ✅ 2026-07-14 §9-B가 document_id(113xxx) 제공 시작 (별도 temp_document_id도 옴 — 삭제는 document_id 사용).
//    구버전 호환 위해 optional 유지 + 없을 땐 log text 파싱(docIdByName)으로 보강
export interface MarketDoc {
  document_id?: number
  file_name: string
  file_size_bytes: number
  chunk_count: number
  uploaded_at: string
  expires_at: string
  is_expired: boolean
}

interface DocListResponse {
  result?: { documents?: MarketDoc[] } | null
  status?: string
}
interface UploadResponse {
  result?: {
    commit?: { committed_count?: number; documents?: { file_name?: string }[] } | null
    upload_id?: string
    state?: UploadState
    ready?: boolean
    files?: UploadStatusFile[]
    blocked_uploads?: BlockedUpload[]
    errors?: string[]
  } | null
  status?: string
}
interface DeleteResponse {
  result?: { status?: string } | null   // result.status === 'deleted'가 실제 삭제 성공 신호
  status?: string
}

export function fileExt(name: string): string {
  const i = name.lastIndexOf('.')
  return i >= 0 ? name.slice(i + 1).toLowerCase() : ''
}

export function fmtFileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`
  const kb = bytes / 1024
  if (kb < 1024) return `${Math.round(kb)} KB`
  return `${(kb / 1024).toFixed(1)} MB`
}

function decodeHtml(s: string): string {
  return s.replace(/&amp;/g, '&').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&quot;/g, '"').replace(/&#39;/g, "'")
}

export function parseDocIdsFromText(text: string, into: Map<string, number> = new Map()): Map<string, number> {
  const re = /업로드 파일\((.+?) \(document_id=(\d+)\)\)/g
  let m: RegExpExecArray | null
  while ((m = re.exec(text)) !== null) {
    const name = decodeHtml(m[1].trim())
    const id = Number(m[2])
    if (name && Number.isFinite(id)) into.set(name, id)
  }
  return into
}

export function validateFiles(files: File[], existingBytes: number): string | null {
  for (const f of files) {
    if (!(ALLOWED_EXT as readonly string[]).includes(fileExt(f.name))) return DOC_ALERT.unsupported
  }
  const newBytes = files.reduce((s, f) => s + f.size, 0)
  if (existingBytes + newBytes > MAX_SESSION_MB * 1024 * 1024) return DOC_ALERT.overSize
  return null
}

async function requestMarketDocuments(appSessionId: string): Promise<MarketDoc[]> {
  const res = await apiFetch(DOC_API.list, {
    method: 'POST',
    body: JSON.stringify({ app_session_id: appSessionId }),
  })
  if (!res.ok) throw new DocumentListHttpError(res.status)
  const data = (await res.json()) as DocListResponse
  if (data.status !== 'SUCCESS') throw new Error('Document list request returned a non-success status')
  return data.result?.documents ?? []
}

const fetchMarketDocumentsSingleFlight = createSingleFlight(requestMarketDocuments, { failureCooldownMs: 30_000 })

export function fetchMarketDocuments(appSessionId: string): Promise<MarketDoc[]> {
  return fetchMarketDocumentsSingleFlight(appSessionId)
}

export function refreshMarketDocuments(appSessionId: string): Promise<MarketDoc[]> {
  return requestMarketDocuments(appSessionId)
}

export interface UploadOutcome {
  committedCount: number
  committedNames: string[] 
  uploadId?: string
  state: UploadState
  ready: boolean
  files: UploadStatusFile[]
  blockedUploads: BlockedUpload[]
  errors: string[]
}

export const KNOWN_UPLOAD_STATES = [
  'accepted', 'preprocessing', 'committing', 'ready', 'blocked', 'failed', 'interrupted', 'expired',
] as const
export type KnownUploadState = typeof KNOWN_UPLOAD_STATES[number]
export type UploadState = KnownUploadState | 'unknown'
const UPLOAD_STATES = new Set<string>(KNOWN_UPLOAD_STATES)

export interface BlockedUpload {
  route: string
  file_name: string
  message?: string | null
}

const BLOCKED_UPLOAD_FALLBACKS: Readonly<Record<string, string>> = {
  blocked_oversized: '처리하지 못했습니다.',
  preprocess_failed: '처리하지 못했습니다.',
}
const KNOWN_BLOCKED_UPLOAD_ROUTES = new Set(Object.keys(BLOCKED_UPLOAD_FALLBACKS))

export class MarketDocumentUploadError extends Error {
  constructor(message: string, options?: ErrorOptions) {
    super(message, options)
    this.name = 'MarketDocumentUploadError'
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : []
}

function blockedUploads(value: unknown): BlockedUpload[] {
  if (!Array.isArray(value)) return []
  return value.flatMap(item => {
    if (!isRecord(item) || typeof item.file_name !== 'string') return []
    return [{
      file_name: item.file_name,
      route: typeof item.route === 'string' ? item.route : 'unknown',
      message: typeof item.message === 'string' ? item.message : null,
    }]
  })
}

export function formatBlockedUploadAlert(items: readonly BlockedUpload[]): string {
  return items
    .map(item => {
      const trustedMessage = KNOWN_BLOCKED_UPLOAD_ROUTES.has(item.route) ? item.message?.trim() : ''
      return `${item.file_name}: ${trustedMessage || BLOCKED_UPLOAD_FALLBACKS[item.route] || '처리하지 못했습니다.'}`
    })
    .join('\n')
}

export function isKnownUploadState(value: unknown): value is KnownUploadState {
  return typeof value === 'string' && UPLOAD_STATES.has(value)
}

function canonicalUploadState(value: unknown): UploadState {
  return isKnownUploadState(value) ? value : 'unknown'
}

export function normalizeUploadResponse(payload: unknown): UploadOutcome {
  if (!isRecord(payload) || payload.status !== 'SUCCESS' || !isRecord(payload.result)) {
    throw new MarketDocumentUploadError('Upload response did not contain a successful result')
  }

  const result = payload.result
  const commit = isRecord(result.commit) ? result.commit : null
  const documents = commit && Array.isArray(commit.documents) ? commit.documents : []
  const committedNames = documents.flatMap(document => (
    isRecord(document) && typeof document.file_name === 'string' && document.file_name
      ? [document.file_name]
      : []
  ))
  const committedCount = commit && typeof commit.committed_count === 'number'
    ? commit.committed_count
    : committedNames.length
  const rejected = blockedUploads(result.blocked_uploads)
  const errors = stringArray(result.errors)
  const uploadId = typeof result.upload_id === 'string' ? result.upload_id : undefined
  const explicitState = isKnownUploadState(result.state)
    ? result.state
    : undefined
  const files = Array.isArray(result.files) ? result.files as UploadStatusFile[] : []
  const hasAcceptedUpload = explicitState === 'accepted' && Boolean(uploadId)
  const hasCommit = commit !== null

  if (!hasAcceptedUpload && !hasCommit && rejected.length === 0) {
    const detail = errors.length > 0 ? `: ${errors.join('; ')}` : ''
    throw new MarketDocumentUploadError(`Upload response shape is not recognized${detail}`)
  }

  const hasCommittedFiles = committedCount > 0 || committedNames.length > 0
  const state = explicitState
    ?? (rejected.length > 0 && !hasCommittedFiles ? 'blocked' : 'ready')
  const ready = rejected.length > 0 && !hasCommittedFiles
    ? false
    : (typeof result.ready === 'boolean' ? result.ready : state === 'ready')

  return {
    committedCount,
    committedNames,
    uploadId,
    state,
    ready,
    files,
    blockedUploads: rejected,
    errors,
  }
}

export interface UploadStatusFile {
  file_name: string
  state: UploadState
  message?: string | null
  query_ready?: boolean
  route?: string | null
  indexed_pages?: number | null
  total_pages?: number | null
  card?: { size_bytes?: number | null } | null
  // Per-stage progress from code-serving-235. Optional so an older backend that
  // omits it keeps working; a stage without a counter carries processed: null.
  phases?: {
    name: string
    state: string
    processed: number | null
    total: number | null
    unit: string
  }[] | null
}
export interface UploadStatus {
  uploadId: string
  state: UploadState
  ready: boolean
  files: UploadStatusFile[]
  message?: string | null
}

export interface PendingUploadJob {
  appSessionId: string
  uploadId: string
  fileNames: string[]
  startedAtMs?: number
}

const PENDING_UPLOAD_KEY = 'market.pendingUpload'

export function savePendingUpload(job: PendingUploadJob): void {
  try {
    sessionStorage.setItem(PENDING_UPLOAD_KEY, JSON.stringify(job))
  } catch (error) {
    console.warn('accepted upload resume state could not be stored', error)
  }
}

export function loadPendingUpload(appSessionId: string): PendingUploadJob | null {
  let raw: string | null
  try {
    raw = sessionStorage.getItem(PENDING_UPLOAD_KEY)
  } catch (error) {
    console.warn('accepted upload resume state could not be read', error)
    return null
  }
  if (!raw) return null
  try {
    const value = JSON.parse(raw) as Partial<PendingUploadJob>
    if (value.appSessionId !== appSessionId || typeof value.uploadId !== 'string' || !Array.isArray(value.fileNames)) return null
    if (!value.fileNames.every(name => typeof name === 'string')) return null
    return {
      appSessionId,
      uploadId: value.uploadId,
      fileNames: value.fileNames,
      ...(typeof value.startedAtMs === 'number' ? { startedAtMs: value.startedAtMs } : {}),
    }
  } catch (error) {
    console.warn('accepted upload resume state is invalid', error)
    return null
  }
}

export function clearPendingUpload(uploadId: string): void {
  let raw: string | null
  try {
    raw = sessionStorage.getItem(PENDING_UPLOAD_KEY)
  } catch (error) {
    console.warn('accepted upload resume state could not be read for cleanup', error)
    return
  }
  if (!raw) return
  try {
    const value = JSON.parse(raw) as Partial<PendingUploadJob>
    if (value.uploadId === uploadId) sessionStorage.removeItem(PENDING_UPLOAD_KEY)
  } catch (error) {
    console.warn('accepted upload resume state cleanup required a reset', error)
    try {
      sessionStorage.removeItem(PENDING_UPLOAD_KEY)
    } catch (removeError) {
      console.warn('accepted upload resume state could not be cleared', removeError)
    }
  }
}

function optionalString(value: unknown): string | null | undefined {
  return typeof value === 'string' || value === null ? value : undefined
}

function optionalBoolean(value: unknown): boolean | undefined {
  return typeof value === 'boolean' ? value : undefined
}

function optionalNumber(value: unknown): number | null | undefined {
  return typeof value === 'number' || value === null ? value : undefined
}

function parseUploadPhases(value: unknown): UploadStatusFile['phases'] | undefined {
  if (value === null) return null
  if (!Array.isArray(value)) return undefined
  return value.flatMap(item => {
    if (!isRecord(item) || typeof item.name !== 'string' || typeof item.state !== 'string' || typeof item.unit !== 'string') return []
    const processed = optionalNumber(item.processed)
    const total = optionalNumber(item.total)
    if (processed === undefined || total === undefined) return []
    return [{ name: item.name, state: item.state, processed, total, unit: item.unit }]
  })
}

function parseUploadStatusFiles(value: unknown): UploadStatusFile[] {
  if (value === undefined) return []
  if (!Array.isArray(value)) throw new Error('Upload status response has invalid files')
  return value.map(item => {
    if (!isRecord(item) || typeof item.file_name !== 'string') {
      throw new Error('Upload status response has an invalid file item')
    }
    const card = isRecord(item.card)
      ? { size_bytes: optionalNumber(item.card.size_bytes) }
      : item.card === null ? null : undefined
    return {
      file_name: item.file_name,
      state: canonicalUploadState(item.state),
      ...(optionalString(item.message) !== undefined ? { message: optionalString(item.message) } : {}),
      ...(optionalBoolean(item.query_ready) !== undefined ? { query_ready: optionalBoolean(item.query_ready) } : {}),
      ...(optionalString(item.route) !== undefined ? { route: optionalString(item.route) } : {}),
      ...(optionalNumber(item.indexed_pages) !== undefined ? { indexed_pages: optionalNumber(item.indexed_pages) } : {}),
      ...(optionalNumber(item.total_pages) !== undefined ? { total_pages: optionalNumber(item.total_pages) } : {}),
      ...(card !== undefined ? { card } : {}),
      ...(parseUploadPhases(item.phases) !== undefined ? { phases: parseUploadPhases(item.phases) } : {}),
    }
  })
}

export async function uploadMarketDocuments(
  appSessionId: string,
  files: File[],
  onProgress?: (progress: ApiUploadProgress) => void,
): Promise<UploadOutcome> {
  const fd = new FormData()
  files.forEach(f => fd.append('file', f))
  fd.append('app_session_id', appSessionId)
  fd.append('workflow_id', String(getRuntimeConfig().marketDocumentWorkflowId))
  fd.append('vdb_id', String(VDB_ID))
  fd.append('return_when', getRuntimeConfig().marketAcceptedUploadEnabled ? 'accepted' : 'complete')

  try {
    const res = await apiUploadFormData(DOC_API.upload, fd, { timeoutMs: UPLOAD_TIMEOUT_MS, onProgress })
    if (!res.ok) throw new MarketDocumentUploadError(`Upload request failed with HTTP ${res.status}`)
    const data = (await res.json()) as UploadResponse
    return normalizeUploadResponse(data)
  } catch (error) {
    if (error instanceof MarketDocumentUploadError) throw error
    throw new MarketDocumentUploadError('Upload response could not be processed', { cause: error })
  }
}

export async function getUploadStatus(appSessionId: string, uploadId: string): Promise<UploadStatus> {
  const query = new URLSearchParams({
    workflow_id: String(getRuntimeConfig().marketDocumentWorkflowId),
    app_session_id: appSessionId,
    upload_id: uploadId,
  })
  const controller = new AbortController()
  const timeoutId = setTimeout(() => controller.abort(), UPLOAD_STATUS_REQUEST_TIMEOUT_MS)
  try {
    const res = await apiFetch(`${DOC_API.uploadStatus}?${query}`, { method: 'GET', signal: controller.signal })
    if (!res.ok) throw new Error(`Upload status request failed with HTTP ${res.status}`)
    const data = await res.json() as unknown
    if (!isRecord(data) || data.status !== 'SUCCESS' || !isRecord(data.result)) {
      throw new Error('Upload status response is incomplete')
    }
    const result = data.result
    if (typeof result.upload_id !== 'string' || result.state === undefined) {
      throw new Error('Upload status response is incomplete')
    }
    const state = canonicalUploadState(result.state)
    return {
      uploadId: result.upload_id,
      state,
      ready: state === 'unknown' ? false : result.ready === true,
      files: parseUploadStatusFiles(result.files),
      message: optionalString(result.message),
    }
  } finally {
    clearTimeout(timeoutId)
  }
}

export const UPLOAD_POLL_INTERVAL_MS = 2_000
export const UPLOAD_POLL_TIMEOUT_MS = 30 * 60_000
export const UPLOAD_STATUS_REQUEST_TIMEOUT_MS = 15_000
export async function waitForUpload(
  appSessionId: string,
  uploadId: string,
  onProgress: (status: UploadStatus) => void,
  options: { intervalMs?: number; timeoutMs?: number; sleep?: (ms: number) => Promise<void> } = {},
): Promise<UploadStatus> {
  const intervalMs = options.intervalMs ?? UPLOAD_POLL_INTERVAL_MS
  const timeoutMs = options.timeoutMs ?? UPLOAD_POLL_TIMEOUT_MS
  const sleep = options.sleep ?? (ms => new Promise(resolve => setTimeout(resolve, ms)))
  const started = Date.now()
  while (true) {
    const status = await getUploadStatus(appSessionId, uploadId)
    onProgress(status)
    if (isTerminalUploadState(status.state)) return status
    if (Date.now() - started >= timeoutMs) throw new Error('Upload status polling timed out')
    await sleep(intervalMs)
  }
}

export async function deleteMarketDocument(appSessionId: string, documentId: number): Promise<boolean> {
  try {
    const res = await apiFetch(DOC_API.delete, {
      method: 'PUT',
      body: JSON.stringify({ app_session_id: appSessionId, document_id: documentId }),
    })
    const data = (await res.json()) as DeleteResponse
    return data.status === 'SUCCESS' && data.result?.status === 'deleted'
  } catch {
    return false
  }
}
