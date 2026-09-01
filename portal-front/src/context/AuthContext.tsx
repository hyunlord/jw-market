import { createContext, useContext, useState, useCallback } from 'react'
import { clearPagePerms } from '../utils/pagePermission'
import { serverLogout } from '../utils/apiFetch'
import { resolvePortalPath } from '../config/runtimeConfig'

interface User {
  userId: string
  userName: string
  lastLoginTime?: string
  picture?: string   // Google 로그인 프로필 이미지 (일반 로그인은 미포함)
}

interface AuthContextType {
  portalToken: string | null
  user: User | null
  login: (portalToken: string, accessToken: string, refreshToken: string, user: User) => void
  logout: () => void
}

const AuthContext = createContext<AuthContextType | null>(null)

function loadStoredUser(): User | null {
  try {
    const raw = localStorage.getItem('user')
    return raw ? (JSON.parse(raw) as User) : null
  } catch {
    return null
  }
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [portalToken, setPortalToken] = useState<string | null>(() => localStorage.getItem('portalToken'))
  const [user, setUser] = useState<User | null>(loadStoredUser)

  const login = useCallback((pt: string, at: string, rt: string, usr: User) => {
    setPortalToken(pt)
    setUser(usr)
    localStorage.setItem('portalToken', pt)
    localStorage.setItem('accessToken', at)
    localStorage.setItem('refreshToken', rt)
    localStorage.setItem('user', JSON.stringify(usr))
    clearPagePerms()  // 새 사용자 권한이므로 이전 캐시 무효화 (다른 권한자 잔존 방지)
  }, [])

  const logout = useCallback(() => {
    serverLogout()
    setPortalToken(null)
    setUser(null)
    localStorage.removeItem('portalToken')
    localStorage.removeItem('accessToken')
    localStorage.removeItem('refreshToken')
    localStorage.removeItem('user')
    clearPagePerms()  // 페이지 권한 캐시도 무효화
    window.location.replace(resolvePortalPath('/login'))
  }, [])

  return (
    <AuthContext.Provider value={{ portalToken, user, login, logout }}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth() {
  const ctx = useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used inside AuthProvider')
  return ctx
}
