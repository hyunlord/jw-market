import { useRef, useState, useEffect } from 'react'
import FilePreviewList from './FilePreviewList'
import type { PendingDoc } from '../../utils/useMarketDocuments'
import type { UploadProgress } from '../../utils/uploadProgress.ts'

export interface InputFileUpload {
  docs: PendingDoc[]
  uploadProgress: UploadProgress | null
  uploading: boolean
  onPickFiles: (files: FileList | File[]) => void
  onRetryUploadStatus: () => void
  onRemovePending: (documentId: number) => void
}

interface Props {
  isGenerating: boolean
  disabled?: boolean
  onSend: (content: string) => void
  onStop: () => void
  focusKey?: string | null
  fileUpload?: InputFileUpload
}

export default function InputArea({ isGenerating, disabled = false, onSend, onStop, focusKey, fileUpload }: Props) {
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const [inputValue, setInputValue] = useState('')

  // 마운트 + focusKey 변경(채팅 전환) 시 입력창 자동 포커스
  useEffect(() => {
    if (!disabled) textareaRef.current?.focus()
  }, [focusKey, disabled])

  const adjustHeight = () => {
    const el = textareaRef.current
    if (!el) return
    const BASE = 48, MAX = 26 * 5 + 20
    el.style.height = `${BASE}px`
    const sh = el.scrollHeight
    if (sh > BASE) {
      el.style.height = `${Math.min(sh, MAX)}px`
      el.style.overflowY = sh <= MAX ? 'hidden' : 'auto'
    } else {
      el.style.height = `${BASE}px`
      el.style.overflowY = 'hidden'
    }
  }

  const handleSend = () => {
    const content = inputValue.trim()
    if (!content) return
    if (fileUpload?.uploading) return   // 업로드 진행 중엔 전송 대기
    onSend(content)
    setInputValue('')
    // 전송 후 높이 초기화
    if (textareaRef.current) {
      textareaRef.current.style.height = '48px'
      textareaRef.current.style.overflowY = 'hidden'
    }
  }

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.nativeEvent.isComposing) return
    // Shift+Enter: 줄바꿈 / Enter 단독: 전송
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      if (!isGenerating && !disabled) handleSend()
    }
  }

  const textarea = (
    <textarea
      ref={textareaRef}
      id="main-input"
      placeholder="AI에게 질문하거나 작업을 요청하세요."
      value={inputValue}
      disabled={disabled}
      onChange={e => { setInputValue(e.target.value); adjustHeight() }}
      onKeyDown={handleKeyDown}
    />
  )
  const sendBtn = isGenerating
    ? <button className="btn-stop" onClick={onStop} />
    : <button className={`btn-send${inputValue.trim() && !disabled && !fileUpload?.uploading ? ' active' : ''}`} onClick={disabled ? undefined : handleSend} />

  return (
    <div className={`input-container${fileUpload ? ' market-file' : ''}`}>
      <div className="input-wrapper">
        {fileUpload && (
          <FilePreviewList
            docs={fileUpload.docs}
            uploadProgress={fileUpload.uploadProgress}
            onRemove={fileUpload.onRemovePending}
            onRetryStatus={fileUpload.onRetryUploadStatus}
            onRetryUpload={() => fileInputRef.current?.click()}
          />
        )}
        {fileUpload ? (
          <div className="btm-input-wrap">
            <button
              type="button"
              className="btn-file"
              onClick={() => fileInputRef.current?.click()}
              disabled={fileUpload.uploading}
              title="파일 업로드"
            />
            <input
              ref={fileInputRef}
              type="file"
              multiple
              accept=".pdf,.pptx,.docx,.xlsx"
              style={{ display: 'none' }}
              onChange={e => { if (e.target.files?.length) fileUpload.onPickFiles(e.target.files); e.target.value = '' }}
            />
            {textarea}
            <div id="ghost-div" />
            {sendBtn}
          </div>
        ) : (
          <>
            {textarea}
            <div id="ghost-div" />
            {sendBtn}
          </>
        )}
      </div>
    </div>
  )
}
