import { createContext, useCallback, useContext, useRef, useState } from 'react'
import Toast from '../components/main/Toast'

interface ToastContextValue {
  showToast: (message: string, durationMs?: number) => void
}

const ToastContext = createContext<ToastContextValue | null>(null)

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [toast, setToast] = useState({ message: '', visible: false })
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null)

  const showToast = useCallback((message: string, durationMs = 3000) => {
    if (timerRef.current) clearTimeout(timerRef.current)
    setToast({ message, visible: true })
    timerRef.current = setTimeout(() => setToast({ message: '', visible: false }), durationMs)
  }, [])

  return (
    <ToastContext.Provider value={{ showToast }}>
      {children}
      {/* App-level 전역 Toast — 페이지 unmount되어도 사라지지 않음 */}
      <Toast message={toast.message} visible={toast.visible} />
    </ToastContext.Provider>
  )
}

export function useToast(): ToastContextValue {
  const ctx = useContext(ToastContext)
  if (!ctx) throw new Error('useToast must be used inside <ToastProvider>')
  return ctx
}
