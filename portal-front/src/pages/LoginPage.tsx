import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useAuth } from '../context/AuthContext'
import Modals from '../components/main/Modals'
import {PAGE_URLS, hasClientPagePermission, pageAuthorityPreRegistration} from '../utils/pagePermission'
import { getRuntimeConfig, resolveApiUrl } from '../config/runtimeConfig'

declare global {
  interface Window {
    google: {
      accounts: {
        id: {
          initialize: (config: {
            client_id: string
            callback: (response: { credential: string }) => void
          }) => void
          renderButton: (element: HTMLElement, config: {
            type?: 'standard' | 'icon'
            theme?: 'outline' | 'filled_blue' | 'filled_black'
            size?: 'large' | 'medium' | 'small'
            text?: 'signin_with' | 'signup_with' | 'continue_with' | 'signin'
            shape?: 'rectangular' | 'pill' | 'circle' | 'square'
            width?: number
          }) => void
        }
      }
    }
  }
}

// 성공 / 실패 응답이 한 타입으로 옴 — 실패 시 portalToken 없이 result에 메시지 문자열만 채워짐
// (실패도 status:'SUCCESS' / statusCode:200으로 오므로 분기 기준은 `portalToken` 존재 여부)
interface GoogleLoginResponse {
  portalToken?: string
  accessToken?: string
  refreshToken?: string
  userId?: string
  userName?: string | { name: string; givenName: string; familyName: string }
  lastLoginTime?: string
  accessIp?: string | null
  picture?: string
  id?: string
  result?: string   // 실패 시 메시지 ("계정이 비활성 상태 입니다." 등)
}

export default function LoginPage() {
  const navigate = useNavigate()
  const { login, logout } = useAuth()
  const googleBtnRef = useRef<HTMLDivElement>(null)
  const [alertMessage, setAlertMessage] = useState<string | null>(null)
  const [alertShouldLogout, setAlertShouldLogout] = useState(false)
  // Google credential 콜백 시작 ~ /auth/google/login 응답 도착까지 로딩 화면 표시 (퍼블 etc_access.html)
  const [isLoggingIn, setIsLoggingIn] = useState(false)
  // RND·MARKET 권한 둘 다 보유 시 이동할 에이전트 선택 모달 (한 권한만 있으면 자동 이동)
  const [agentSelect, setAgentSelect] = useState(false)

  useEffect(() => {
    const initGoogle = () => {
      window.google.accounts.id.initialize({
        client_id: getRuntimeConfig().googleClientId,
        callback: async (response) => {
          setIsLoggingIn(true)   // 응답 대기 동안 로딩 화면 표시 (3~4초)
          try {
            const res = await fetch(resolveApiUrl('/api/v1/auth/google/login'), {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ googleJwtToken: response.credential }),
            })
            const data: GoogleLoginResponse = await res.json()
            if (data.portalToken && data.accessToken && data.refreshToken && data.userId) {
              // userName은 객체({name,givenName,familyName})로 옴 — 표시용 단일 문자열 추출
              const userName = typeof data.userName === 'string'
                ? data.userName
                : (data.userName?.name ?? '')
              login(data.portalToken, data.accessToken, data.refreshToken, {
                userId: data.userId,
                userName,
                lastLoginTime: data.lastLoginTime,
                picture: data.picture,   // 프로필 이미지 (표시 코드는 추후 연결)
              })
              // 새 portalToken의 pageAuthorities 기준 가용 첫 페이지로 이동
              // (예: pageAuthorities=['MARKET']이면 /rnd 건너뛰고 /market으로)
              // login()이 이미 clearPagePerms() 호출했고 localStorage.portalToken도 저장됨 → 즉시 JWT 디코딩 가능
              // navigate 후 LoginPage 언마운트되므로 isLoggingIn 해제 불필요
              // Market 권한이 있는 경우 사전 등록
              await pageAuthorityPreRegistration()
              if (hasClientPagePermission('/rnd') && hasClientPagePermission('/market')) {
                setAgentSelect(true)
                return
              }
              const destination = PAGE_URLS.find(url => hasClientPagePermission(url))
              if (!destination) {
                //   PrivateRoute와 동일하게 권한 없음 알림 → 확인 시 logout(토큰 정리 + /login 이동).
                setIsLoggingIn(false)
                setAlertShouldLogout(true)
                setAlertMessage('접근 가능한 서비스 권한이 없습니다.\n관리자에게 문의해 주세요.')
                return
              }
              navigate(destination)
            } else if (typeof data.result === 'string') {
              // 실패 케이스: portalToken 없이 result에 메시지만 옴 (status는 'SUCCESS')
              setIsLoggingIn(false)
              setAlertMessage(data.result)
            } else {
              setIsLoggingIn(false)
            }
          } catch (err) {
            console.error('[Login] 실패:', err)
            setIsLoggingIn(false)
          }
        },
      })

      if (googleBtnRef.current) {
        // GSI 버튼 width는 박스(245px)와 일치시켜 보이지 않는 클릭 영역을 박스에 정확히 맞춤
        window.google.accounts.id.renderButton(googleBtnRef.current, {
          type: 'standard',
          theme: 'outline',
          size: 'large',
          text: 'signin_with',
          shape: 'rectangular',
          width: 245,
        })
      }
    }

    if (window.google?.accounts?.id) {
      initGoogle()
    } else {
      const interval = setInterval(() => {
        if (window.google?.accounts?.id) {
          clearInterval(interval)
          initGoogle()
        }
      }, 50)
      return () => clearInterval(interval)
    }
  }, [login, navigate])

  if (isLoggingIn) {
    // 퍼블 etc_access.html 그대로 — 응답 대기 동안 표시
    return (
      <div className="wrap">
        <div className="loading-container">
          <div className="loading-content">
            <h1 className="loading-message">
              <div className="img-logo01"><img src="/images/logo.png" alt="JW" /></div>
              <div className="img-logo02"><img src="/images/logo_text.png" alt="AI agent" /></div>
              에 접속 중입니다
            </h1>
            <div className="spinner">
              <div className="fixed-8bar-spinner">
                {Array.from({ length: 8 }, (_, i) => (
                  <div key={i} className={`bar bar${i + 1}`} />
                ))}
              </div>
            </div>
          </div>
        </div>
        {/* 에이전트 선택 모달 — 로딩 화면 위에 노출 (RND·MARKET 둘 다 보유 시) */}
        <Modals
          agentSelectModal={agentSelect}
          onSelectRnd={() => navigate('/rnd')}
          onSelectMarket={() => navigate('/market')}
        />
      </div>
    )
  }

  return (
    <div className="wrap">
      <div className="login-container">
        <div className="left-visual">
          <div className="inner-wrap">
            <div className="brand-logo">
              <div className="img-logo01">
                <img src="/images/logo.png" alt="JW" />
              </div>
              <div className="img-logo02">
                <img src="/images/logo_text.png" alt="AI agent" />
              </div>
            </div>
          </div>
        </div>
        <div className="login-content">
          <div className="login-box">
            <div className="inner-wrap">
              <div className="login-title">구글 계정으로 로그인 해주세요</div>
              <div className="login-description">
                회사 구글 계정으로 로그인을 진행해주세요.<br />
                구글 계정이 없을 경우 회원가입 페이지로 이동합니다.
              </div>
              <div className="login-actions">
                {/* 퍼블 .btn-google 디자인을 그대로 표시 + 그 아래 보이지 않는 GSI 버튼이 실제 클릭을 받음.
                    오버레이는 pointer-events:none으로 클릭을 통과시켜 ID Token 발급 흐름은 GSI가 정상 처리. */}
                <div style={{ position: 'relative', width: 245, height: 56, overflow: 'hidden', borderRadius: 'var(--radius-s)' }}>
                  <div
                    ref={googleBtnRef}
                    style={{ position: 'absolute', inset: 0, display: 'flex', justifyContent: 'center', alignItems: 'center' }}
                  />
                  <a
                    href="#"
                    onClick={e => e.preventDefault()}
                    className="btn-google"
                    style={{ position: 'absolute', inset: 0, pointerEvents: 'none', backgroundColor: '#fff' }}
                  >
                    <img src="/images/icon_google.png" alt="JW" className="img-logo" />
                    <img src="/images/text_google.png" alt="JW" className="img-text" />
                  </a>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* 공통 Modals — LoginPage 메인 화면은 알림 모달만 사용 (에이전트 선택은 로딩 화면 return에서) */}
      <Modals
        alertMessage={alertMessage}
        onCloseAlert={() => {
          if (alertShouldLogout) { logout(); return }   // 권한 0개 → 확인 시 토큰 정리 + 로그인 페이지
          setAlertMessage(null)
        }}
      />
    </div>
  )
}
