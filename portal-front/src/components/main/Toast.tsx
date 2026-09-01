interface Props {
  message: string
  visible: boolean
}

export default function Toast({ message, visible }: Props) {
  return (
    <div className={`toast-container${visible ? ' toast-item' : ''}`}>
      <div className="toast-description">{message}</div>
    </div>
  )
}
