export interface UserFileChip {
  documentId: number
  fileName: string   // 확장자 포함 전체 (예: "JW 시장분석.pdf")
}

interface Props {
  content: string
  files?: UserFileChip[]
  onRemoveFile?: (documentId: number) => void
  onCopy?: () => void
}

export default function ChatMessageUser({ content, files, onRemoveFile, onCopy }: Props) {
  const handleCopy = () => {
    navigator.clipboard.writeText(content)
    onCopy?.()
  }
  const hasFiles = (files?.length ?? 0) > 0

  return (
    <div className={`chat-message-user${hasFiles ? ' has-files' : ''}`}>
      {files?.map(f => {
        const ext = (f.fileName.split('.').pop() ?? '').toUpperCase()
        return (
          <div className="user-bubble file-bubble" key={f.documentId}>
            <div className="ty-file">
              {ext && <span className="file-ext-badge">{ext}</span>}
              <span className="file-name-text">{f.fileName}</span>
            </div>
            <div className="btn-close-file-name" onClick={() => onRemoveFile?.(f.documentId)} />
          </div>
        )
      })}
      {content && (
        <div className="user-bubble">
          {content}
          <div className="icon-wrap">
            <button className="btn-clipboard" onClick={handleCopy} />
          </div>
        </div>
      )}
    </div>
  )
}
