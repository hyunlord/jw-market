import { useState, useCallback, useEffect, useRef } from 'react'
import {
  fetchMarketDocuments, refreshMarketDocuments, uploadMarketDocuments, deleteMarketDocument,
  validateFiles, fileExt, DOC_ALERT, waitForUpload,
  savePendingUpload, loadPendingUpload, clearPendingUpload, formatBlockedUploadAlert,
  type MarketDoc, type PendingUploadJob, type UploadStatus,
} from './marketDocuments'
import {
  createProcessingProgress,
  createStatusUnavailableProgress,
  createTransferProgress,
  summarizeUploadFiles,
  type UploadProgress,
} from './uploadProgress.ts'
import { documentListFailureMessage, resolveDocumentsBeforeUpload } from './marketDocumentListPolicy'

export interface PendingDoc {
  documentId: number
  fileName: string
  ext: string
  sizeBytes: number
}

function toPending(d: MarketDoc): PendingDoc {
  return { documentId: d.document_id ?? 0, fileName: d.file_name, ext: fileExt(d.file_name), sizeBytes: d.file_size_bytes }
}

interface Options {
  ensureSessionId: () => string
  hasSessionId: () => boolean
  onAlert: (msg: string) => void
}

export function useMarketDocuments({ ensureSessionId, hasSessionId, onAlert }: Options) {
  const [pendingDocs, setPendingDocs] = useState<PendingDoc[]>([])
  const [uploading, setUploading] = useState(false)
  const [uploadProgress, setUploadProgressState] = useState<UploadProgress | null>(null)
  const uploadProgressRef = useRef<UploadProgress | null>(null)
  const resumedUploadId = useRef<string | null>(null)

  const setUploadProgress = useCallback((progress: UploadProgress | null) => {
    uploadProgressRef.current = progress
    setUploadProgressState(progress)
  }, [])

  const refreshUploadedDocuments = useCallback(async (sid: string): Promise<boolean> => {
    try {
      const documents = await refreshMarketDocuments(sid)
      setPendingDocs(documents.map(toPending))
      return true
    } catch {
      onAlert(DOC_ALERT.uploadCompleteListFail)
      return false
    }
  }, [onAlert])

  const monitorAcceptedUpload = useCallback(async (
    sid: string,
    job: PendingUploadJob,
    initialStatus?: UploadStatus,
  ): Promise<void> => {
    const startedAtMs = job.startedAtMs ?? Date.now()
    let current = createProcessingProgress(initialStatus ?? {
      uploadId: job.uploadId,
      state: 'accepted',
      ready: false,
      files: [],
    }, job.fileNames, startedAtMs)

    resumedUploadId.current = job.uploadId
    setUploading(true)
    setUploadProgress(current)
    try {
      const terminal = await waitForUpload(sid, job.uploadId, status => {
        current = createProcessingProgress(status, job.fileNames, startedAtMs)
        setUploadProgress(current)
      })
      clearPendingUpload(job.uploadId)
      resumedUploadId.current = null
      await refreshUploadedDocuments(sid)
      const summary = summarizeUploadFiles(current.files)
      if (terminal.ready && summary.failedCount === 0) setUploadProgress(null)
    } catch {
      setUploadProgress(createStatusUnavailableProgress(current))
    } finally {
      setUploading(uploadProgressRef.current?.phase === 'status-unavailable')
    }
  }, [refreshUploadedDocuments, setUploadProgress])

  useEffect(() => {
    if (!hasSessionId()) return
    const sid = ensureSessionId()
    const pending = loadPendingUpload(sid)
    if (!pending || resumedUploadId.current === pending.uploadId) return
    void monitorAcceptedUpload(sid, pending)
  }, [ensureSessionId, hasSessionId, monitorAcceptedUpload])

  const retryUploadStatus = useCallback(() => {
    const progress = uploadProgressRef.current
    if (!progress || progress.phase !== 'status-unavailable') return
    const job: PendingUploadJob = {
      appSessionId: ensureSessionId(),
      uploadId: progress.uploadId,
      fileNames: progress.files.map(file => file.fileName),
      startedAtMs: progress.startedAtMs,
    }
    void monitorAcceptedUpload(job.appSessionId, job, {
      uploadId: progress.uploadId,
      state: progress.state,
      ready: false,
      files: progress.files.map(file => ({
        file_name: file.fileName,
        state: file.state,
        message: file.message,
        query_ready: file.queryReady,
        indexed_pages: file.indexedPages,
        total_pages: file.totalPages,
      })),
    })
  }, [ensureSessionId, monitorAcceptedUpload])

  const pickFiles = useCallback(async (fileList: FileList | File[]) => {
    const files = Array.from(fileList)
    if (files.length === 0) return

    const localValidation = validateFiles(files, 0)
    if (localValidation) { onAlert(localValidation); return }

    const isFreshSession = !hasSessionId()
    const sid = ensureSessionId()

    let before: readonly MarketDoc[]
    try {
      before = await resolveDocumentsBeforeUpload({
        isFreshSession,
        localDocumentCount: pendingDocs.length,
        loadDocuments: () => fetchMarketDocuments(sid),
      })
    } catch (error) {
      onAlert(documentListFailureMessage(error))
      return
    }
    const existingBytes = before.reduce((sum, document) => sum + document.file_size_bytes, 0)
    const alert = validateFiles(files, existingBytes)
    if (alert) { onAlert(alert); return }

    const fileNames = files.map(file => file.name)
    const startedAtMs = Date.now()
    let acceptedUpload = false
    setUploading(true)
    setUploadProgress(createTransferProgress(fileNames, startedAtMs, 0, null))
    try {
      const outcome = await uploadMarketDocuments(sid, files, progress => {
        setUploadProgress(createTransferProgress(
          fileNames,
          startedAtMs,
          progress.loadedBytes,
          progress.totalBytes,
        ))
      })
      if (outcome.blockedUploads.length > 0) {
        onAlert(formatBlockedUploadAlert(outcome.blockedUploads))
      }
      if (outcome.state === 'accepted' && !outcome.uploadId) {
        onAlert(DOC_ALERT.uploadFail)
        setUploadProgress(null)
        return
      }
      if (outcome.state === 'accepted' && outcome.uploadId) {
        acceptedUpload = true
        const job: PendingUploadJob = { appSessionId: sid, uploadId: outcome.uploadId, fileNames, startedAtMs }
        savePendingUpload(job)
        await monitorAcceptedUpload(sid, job, {
          uploadId: outcome.uploadId,
          state: outcome.state,
          ready: outcome.ready,
          files: outcome.files,
        })
        return
      }

      if (outcome.state === 'blocked' && outcome.committedCount === 0) {
        setUploadProgress(null)
        return
      }

      const refreshed = await refreshUploadedDocuments(sid)
      if (refreshed) setUploadProgress(null)
    } catch (error) {
      console.error('market document upload failed', error)
      onAlert(DOC_ALERT.uploadFail)
      setUploadProgress(null)
    } finally {
      if (!acceptedUpload) setUploading(false)
    }
  }, [ensureSessionId, hasSessionId, monitorAcceptedUpload, onAlert, pendingDocs.length, refreshUploadedDocuments, setUploadProgress])

  const removePending = useCallback((documentId: number) => {
    const sid = ensureSessionId()
    setPendingDocs(prev => prev.filter(document => document.documentId !== documentId))
    void deleteMarketDocument(sid, documentId)
  }, [ensureSessionId])

  const clearPending = useCallback(() => setPendingDocs([]), [])

  const resetDocs = useCallback(() => {
    setPendingDocs([])
    setUploading(false)
    setUploadProgress(null)
  }, [setUploadProgress])

  return {
    pendingDocs,
    uploading,
    uploadProgress,
    pickFiles,
    retryUploadStatus,
    removePending,
    clearPending,
    resetDocs,
  }
}
