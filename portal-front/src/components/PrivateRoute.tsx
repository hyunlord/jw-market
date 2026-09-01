import { useEffect, useState } from 'react'
import { Navigate } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'
import { getPagePermission } from '../utils/pagePermission'
import Modals from './main/Modals'

const DENIED_MESSAGE = '해당 서비스에 대한 접근 권한이 없습니다.\n관리자에게 문의해 주세요.'

// pageUrl 넘기면 /api/v1/page 권한 체크 수행 (BACK_AUTH_API.md §7).
// result === true 면 통과, false 면 권한 거부 모달 표시 → 확인 클릭 시 logout.
export default function PrivateRoute({
  children,
  pageUrl,
}: {
  children: React.ReactNode
  pageUrl?: string
}) {
  const { portalToken, logout } = useAuth()
  const [allowed, setAllowed] = useState<boolean | null>(pageUrl ? null : true)
  const [denied, setDenied] = useState(false)

  useEffect(() => {
    if (!pageUrl || !portalToken) return
    let cancelled = false
    // 진입하는 이 페이지 1개만 서버 게이트 호출 (lazy) — 캐시 hit이면 네트워크 0
    getPagePermission(pageUrl).then(isAllowed => {
      if (cancelled) return
      if (!isAllowed) {
        setDenied(true)   // 모달 표시 (확인 클릭 시 logout)
        return
      }
      setAllowed(true)
    })
    return () => { cancelled = true }
  }, [pageUrl, portalToken])

  if (!portalToken) return <Navigate to="/login" replace />
  if (denied) {
    // 권한 거부 — 페이지 콘텐츠 노출 없이 모달만 표시. 확인 클릭 시 logout (토큰/캐시 정리 + /login)
    return (
      <Modals
        alertMessage={DENIED_MESSAGE}
        onCloseAlert={() => logout()}
      />
    )
  }
  if (pageUrl && allowed === null) return null   // 권한 체크 중 — 빈 화면
  return <>{children}</>
}
