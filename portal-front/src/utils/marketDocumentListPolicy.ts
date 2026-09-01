export class DocumentListHttpError extends Error {
  readonly status: number

  constructor(status: number) {
    super(`Document list request failed with HTTP ${status}`)
    this.name = 'DocumentListHttpError'
    this.status = status
  }
}

interface ResolveDocumentsBeforeUploadOptions<TDocument> {
  readonly isFreshSession: boolean
  readonly localDocumentCount: number
  readonly loadDocuments: () => Promise<readonly TDocument[]>
}

export async function resolveDocumentsBeforeUpload<TDocument>({
  isFreshSession,
  localDocumentCount,
  loadDocuments,
}: ResolveDocumentsBeforeUploadOptions<TDocument>): Promise<readonly TDocument[]> {
  try {
    return await loadDocuments()
  } catch (error) {
    const isUnregisteredFreshSession = isFreshSession
      && localDocumentCount === 0
      && error instanceof DocumentListHttpError
      && error.status === 404

    if (isUnregisteredFreshSession) return []
    throw error
  }
}

export function documentListFailureMessage(error: unknown): string {
  if (error instanceof DocumentListHttpError && error.status === 403) {
    return '문서 목록을 불러올 권한이 없습니다.'
  }
  return '문서 목록을 불러오지 못했습니다.'
}
