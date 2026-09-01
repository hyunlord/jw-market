import { createElement, type ReactElement } from 'react'
import { isTerminalUploadState, type UploadProgressFile } from '../../utils/uploadProgress.ts'

interface Props {
  file: UploadProgressFile
}

export default function UploadPreviewNotice({ file }: Props): ReactElement | null {
  const hasQueryablePreview = file.queryReady === true
    && file.indexedPages !== null
    && file.indexedPages !== undefined
    && file.totalPages !== null
    && file.totalPages !== undefined
    && !isTerminalUploadState(file.state)

  if (!hasQueryablePreview || !file.message) return null

  return createElement('span', { className: 'upload-progress-preview-notice' }, file.message)
}
