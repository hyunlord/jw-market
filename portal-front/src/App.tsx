import { BrowserRouter, Routes, Route, Navigate, useLocation } from 'react-router-dom'
import type { ReactNode } from 'react'
import { AuthProvider } from './context/AuthContext'
import { ToastProvider } from './context/ToastContext'
import PrivateRoute from './components/PrivateRoute'
import ErrorBoundary from './components/ErrorBoundary'
import LoginPage from './pages/LoginPage'
import McpServerPage from './pages/McpServerPage'
import StreamPage from './pages/StreamPage'
import DashboardPage from './pages/DashboardPage'
import MarketChatPage from './pages/MarketChatPage'
import AnalyzePage from './pages/AnalyzePage'
import DeepAnalyzePage from './pages/DeepAnalyzePage'
import E502Page from './pages/E502Page'
import E503Page from './pages/E503Page'
import MaintenancePage from './pages/MaintenancePage'
import { getRuntimeConfig } from './config/runtimeConfig.ts'

// 페이지 렌더 throw가 흰 화면을 내는 것을 막는 안전망.
// key = 경로 + 브랜드(productName) → 다른 브랜드/페이지로 이동하면 boundary가 remount되어 에러 상태 자동 복구.
function PageBoundary({ children }: { children: ReactNode }) {
  const loc = useLocation()
  const productName = (loc.state as { productName?: string } | null)?.productName ?? ''
  return <ErrorBoundary key={`${loc.pathname}|${productName}`}>{children}</ErrorBoundary>
}

// productName 변경 시 강제 재마운트는 AnalyzePage/DeepAnalyzePage 자체에서 self-wrap으로 처리.
// App.tsx에는 wrapper를 두지 않음 (라우트 단순화).
export default function App() {
  const { routerBasename } = getRuntimeConfig()
  return (
    <AuthProvider>
      <ToastProvider>
      <BrowserRouter basename={routerBasename}>
        <Routes>
          <Route path="/login" element={<LoginPage />} />
          <Route path="/rnd" element={<PrivateRoute pageUrl="/rnd"><PageBoundary><StreamPage /></PageBoundary></PrivateRoute>} />
          <Route path="/rnd/mcp" element={<PrivateRoute pageUrl="/rnd"><PageBoundary><McpServerPage /></PageBoundary></PrivateRoute>} />
          <Route path="/market" element={<PrivateRoute pageUrl="/market"><PageBoundary><DashboardPage /></PageBoundary></PrivateRoute>} />
          <Route path="/market/chat" element={<PrivateRoute pageUrl="/market"><PageBoundary><MarketChatPage /></PageBoundary></PrivateRoute>} />
          <Route path="/market/analyze" element={<PrivateRoute pageUrl="/market/analyze"><PageBoundary><AnalyzePage /></PageBoundary></PrivateRoute>} />
          <Route path="/market/deep-analyze" element={<PrivateRoute pageUrl="/market/deep-analyze"><PageBoundary><DeepAnalyzePage /></PageBoundary></PrivateRoute>} />
          <Route path="/502" element={<E502Page />} />
          <Route path="/503" element={<E503Page />} />
          <Route path="/maintenance" element={<MaintenancePage />} />
          <Route path="/" element={<Navigate to="/login" replace />} />
          <Route path="*" element={<E502Page />} />
        </Routes>
      </BrowserRouter>
      </ToastProvider>
    </AuthProvider>
  )
}
