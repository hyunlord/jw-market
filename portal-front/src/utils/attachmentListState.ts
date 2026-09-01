import { documentListFailureMessage } from './marketDocumentListPolicy.ts'
import type { MarketDoc } from './marketDocuments.ts'

export type AttachmentListLoadState =
  | { kind: 'loading' }
  | { kind: 'ready'; documents: MarketDoc[] }
  | { kind: 'failed'; error: unknown }

export function attachmentListView(state: AttachmentListLoadState): {
  kind: 'loading' | 'empty' | 'ready' | 'failed'
  message: string
  canRetry: boolean
} {
  if (state.kind === 'loading') return { kind: 'loading', message: '', canRetry: false }
  if (state.kind === 'failed') {
    return { kind: 'failed', message: documentListFailureMessage(state.error), canRetry: true }
  }
  if (state.documents.length === 0) {
    return { kind: 'empty', message: '첨부된 파일이 없습니다.', canRetry: false }
  }
  return { kind: 'ready', message: '', canRetry: false }
}
