import { clearPagePerms } from './pagePermission.ts'
import { resolveApiUrl, resolvePortalPath } from '../config/runtimeConfig.ts'

// 동시 401 발생 시 refresh가 한 번만 돌도록 singleton으로 관리
let refreshPromise: Promise<boolean> | null = null

function currentPortalBundle(): string {
  if (typeof document === 'undefined') return 'non-browser'
  const entry = Array.from(document.querySelectorAll<HTMLScriptElement>('script[type="module"][src]'))
    .map(script => script.src.match(/\/assets\/([^/?#]+\.js)/)?.[1])
    .find(Boolean)
  return entry ?? 'development'
}

function authHeaders(): Record<string, string> {
  const portalToken = localStorage.getItem('portalToken')
  const accessToken = localStorage.getItem('accessToken')
  return {
    ...(portalToken ? { Authorization: `Bearer ${portalToken}` } : {}),
    ...(accessToken ? { 'Authorization-Access-Token': accessToken } : {}),
    'X-Portal-Bundle': currentPortalBundle(),
  }
}

async function refreshAccessToken(): Promise<boolean> {
  if (refreshPromise) return refreshPromise

  refreshPromise = (async () => {
    const refreshToken = localStorage.getItem('refreshToken')
    if (!refreshToken) return false
    try {
      const res = await fetch(resolveApiUrl('/api/v1/auth/genos/refresh'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ refresh_token: refreshToken }),
      })
      if (!res.ok) return false
      const data = await res.json() as { code: number; data: { access_token: string } }
      if (data.code !== 0) return false
      localStorage.setItem('accessToken', data.data.access_token)
      return true
    } catch {
      return false
    } finally {
      refreshPromise = null
    }
  })()

  return refreshPromise
}

// 동시에 여러 API가 401 받아도 한 번만 실행되도록 가드 (localStorage/replace는 idempotent지만
// console 도배·불필요한 작업 방지)
let redirecting = false

// 서버 세션(토큰) 폐기 요청 — 사용자 로그아웃 시 best-effort 호출 (AuthContext.logout).

export function serverLogout(): void {
  const accessToken = localStorage.getItem('accessToken')
  if (!accessToken) return
  const portalToken = localStorage.getItem('portalToken')
  try {
    void fetch(resolveApiUrl('/api/v1/auth/logout'), {
      method: 'GET',
      keepalive: true,
      headers: {
        ...(portalToken ? { Authorization: `Bearer ${portalToken}` } : {}),
        'Authorization-Access-Token': accessToken,
      },
    }).catch(() => {})
  } catch {
    // best-effort — 실패해도 아래 로컬 정리/리다이렉트는 그대로 진행
  }
}

// 외부에서 명시적 호출용 (AuthContext.logout이 호출)
export function clearAuthAndRedirect() {
  if (redirecting) return
  redirecting = true
  localStorage.removeItem('portalToken')
  localStorage.removeItem('accessToken')
  localStorage.removeItem('refreshToken')
  localStorage.removeItem('user')
  clearPagePerms()
  window.location.replace(resolvePortalPath('/login'))
}

export async function apiFetch(url: string, options: RequestInit = {}, _retried = false): Promise<Response> {
  const isFormData = options.body instanceof FormData

  const res = await fetch(resolveApiUrl(url), {
    ...options,
    headers: {
      ...(isFormData ? {} : { 'Content-Type': 'application/json' }),
      ...authHeaders(),
      ...options.headers,
    },
  })

  if (res.status === 401) {
    if (!_retried) {
      const refreshed = await refreshAccessToken()
      if (refreshed) return apiFetch(url, options, true)
    }
    // refresh도 실패 = 토큰 완전 만료 (다음날 등) → 더는 복구 불가. 자동 로그아웃.
    // (일반 401은 위 refresh 단계에서 retry로 복구되므로 여기까진 안 옴 — 진짜 만료만 해당)
    console.warn('[apiFetch] 401 + refresh 실패 → 자동 로그아웃:', url)
    clearAuthAndRedirect()
    return res
  }

  return res
}

export interface ApiUploadProgress {
  loadedBytes: number
  totalBytes: number | null
}

interface ApiUploadOptions {
  timeoutMs: number
  onProgress?: (progress: ApiUploadProgress) => void
}

function xhrResponseHeaders(xhr: XMLHttpRequest): Headers {
  const headers = new Headers()
  for (const line of xhr.getAllResponseHeaders().trim().split(/[\r\n]+/)) {
    if (!line) continue
    const separator = line.indexOf(':')
    if (separator < 0) continue
    headers.append(line.slice(0, separator).trim(), line.slice(separator + 1).trim())
  }
  return headers
}

function sendUploadRequest(url: string, body: FormData, options: ApiUploadOptions): Promise<Response> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('POST', resolveApiUrl(url))
    xhr.timeout = options.timeoutMs
    for (const [name, value] of Object.entries(authHeaders())) xhr.setRequestHeader(name, value)
    xhr.upload.onprogress = event => options.onProgress?.({
      loadedBytes: event.loaded,
      totalBytes: event.lengthComputable ? event.total : null,
    })
    xhr.onerror = () => reject(new TypeError('Upload request failed'))
    xhr.ontimeout = () => reject(new DOMException('Upload request timed out', 'TimeoutError'))
    xhr.onload = () => resolve(new Response(xhr.responseText, {
      status: xhr.status,
      statusText: xhr.statusText,
      headers: xhrResponseHeaders(xhr),
    }))
    xhr.send(body)
  })
}

export async function apiUploadFormData(
  url: string,
  body: FormData,
  options: ApiUploadOptions,
  retried = false,
): Promise<Response> {
  const response = await sendUploadRequest(url, body, options)
  if (response.status !== 401) return response

  if (!retried && await refreshAccessToken()) {
    return apiUploadFormData(url, body, options, true)
  }
  console.warn('[apiUploadFormData] 401 + refresh 실패 → 자동 로그아웃:', url)
  clearAuthAndRedirect()
  return response
}
