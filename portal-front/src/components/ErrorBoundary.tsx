import { Component, type ReactNode } from 'react'

interface Props {
  children: ReactNode
  fallback?: ReactNode
}
interface State {
  hasError: boolean
}

// 렌더 중 throw(주로 백엔드 응답에 기대한 필드가 통째로 빠진 경우)가 페이지 전체를 흰 화면으로
// 날리는 것을 막는 안전망. 차트별 옵셔널 체이닝 가드로 대부분 빈 차트로 degrade되지만,
// 가드가 못 잡은 예외 케이스에서도 최소한 안내 화면이 뜨도록 한다.
// 리셋은 호출부에서 key(경로+브랜드)로 remount하여 처리 — 다른 브랜드로 이동하면 자동 복구.
export default class ErrorBoundary extends Component<Props, State> {
  state: State = { hasError: false }

  static getDerivedStateFromError(): State {
    return { hasError: true }
  }

  componentDidCatch(error: unknown) {
    console.error('[ErrorBoundary] 렌더 오류:', error)
  }

  render() {
    if (!this.state.hasError) return this.props.children
    if (this.props.fallback !== undefined) return this.props.fallback
    return (
      <div
        style={{
          width: '100%', minHeight: '60vh',
          display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center',
          gap: 16, color: '#82828D',
        }}
      >
        <p style={{ fontSize: 16, fontWeight: 500 }}>일시적으로 화면을 표시할 수 없습니다.</p>
        <p style={{ fontSize: 14 }}>데이터를 불러오는 중 문제가 발생했어요. 잠시 후 다시 시도해 주세요.</p>
        <button
          type="button"
          onClick={() => window.location.reload()}
          style={{
            marginTop: 4, padding: '10px 20px', borderRadius: 8,
            background: '#1A1A1A', color: '#fff', fontSize: 14, fontWeight: 500, cursor: 'pointer',
          }}
        >
          새로고침
        </button>
      </div>
    )
  }
}
