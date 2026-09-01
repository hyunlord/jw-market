// ============ 페이지 권한 조회 (BACK_AUTH_API.md §7) ============
// GET /api/v1/page?pageUrl=... → portalToken JWT의 pageAuthorities claim 검증
// 가이드 권장: portalToken만 사용 (Authorization-Access-Token 헤더 불필요).
// ⚠️ apiFetch 사용 금지 — 401 시 자동 clearAuth + /login redirect로 전체 화면이 튕김.
//    여기선 fetch 직접 호출 + 401/오류 모두 false 반환만 (라우터 가드가 fallback 처리)

// 백엔드 Page enum에 정의된 URL만 호출 (대소문자·trailing slash 등은 백엔드 false → 호출 의미 없음)
import {apiFetch} from "./apiFetch.ts";
import {resolveApiUrl} from "../config/runtimeConfig.ts";

export const PAGE_URLS = ['/rnd', '/market', '/market/analyze', '/market/deep-analyze'] as const
export type PageUrl = typeof PAGE_URLS[number]

export type PagePerms = Record<string, boolean>

const CACHE_KEY = 'pagePerms'
// 캐시가 어느 portalToken으로 채워졌는지 같이 저장 — 토큰 바뀌면 자동 무효화 (다른 사용자 권한 잔존 방지)
const CACHE_TOKEN_KEY = 'pagePermsToken'

// URL → 필요한 Role 매핑 (가이드 §7)
const URL_TO_ROLE: Record<string, 'RND' | 'MARKET'> = {
  '/rnd': 'RND',
  '/market': 'MARKET',
  '/market/analyze': 'MARKET',
  '/market/deep-analyze': 'MARKET',
}

// JWT payload 디코딩 (base64url) — 외부 라이브러리 없이 atob로 처리
function decodeJwtPayload<T>(token: string): T | null {
  try {
    const parts = token.split('.')
    if (parts.length !== 3) return null
    const base64 = parts[1].replace(/-/g, '+').replace(/_/g, '/')
    const padded = base64 + '='.repeat((4 - base64.length % 4) % 4)
    return JSON.parse(atob(padded)) as T
  } catch {
    return null
  }
}

// portalToken의 pageAuthorities claim 기반 클라이언트 측 권한 검증
// ['ALL']이면 모든 URL 통과, 그 외엔 URL_TO_ROLE 매핑된 권한이 배열에 있어야 통과
export function hasClientPagePermission(pageUrl: string): boolean {
  const token = localStorage.getItem('portalToken')
  if (!token) return false
  const claims = decodeJwtPayload<{ pageAuthorities?: string[] }>(token)
  const auths = claims?.pageAuthorities ?? []
  if (auths.includes('ALL')) return true
  const required = URL_TO_ROLE[pageUrl]
  if (!required) return false
  return auths.includes(required)
}

// 단일 URL 권한 조회 — 클라이언트 JWT 검증 + 서버 page API 검증 AND
// (둘 다 통과해야 true. 가이드 §7의 두 방식 모두 사용해 안전망 강화)
async function fetchOne(pageUrl: string): Promise<boolean> {
  if (!pageUrl) return false
  // 1. 클라이언트 JWT 검증 — 실패하면 네트워크 호출 없이 즉시 false
  if (!hasClientPagePermission(pageUrl)) return false
  // 2. 서버 page API 검증
  const portalToken = localStorage.getItem('portalToken')
  if (!portalToken) return false
  try {
    const res = await fetch(
      resolveApiUrl(`/api/v1/page?pageUrl=${encodeURIComponent(pageUrl)}`),
      {
        method: 'GET',
        headers: { Authorization: `Bearer ${portalToken}` },
      },
    )
    if (!res.ok) return false  // 401/500 등 모두 안전 차단 (강제 redirect 없음)
    const json = await res.json() as { status: string; result: boolean }
    return json.status === 'SUCCESS' && json.result === true
  } catch {
    return false  // 네트워크 오류
  }
}

// 현재 portalToken 기준으로 캐시가 유효한지 — 아니면 무효화
// 캐시는 "방문한 페이지만" 부분적으로 채워지는 맵 (lazy 게이트라 전체 prefetch 안 함)
function getValidCachedPerms(): PagePerms | null {
  const currentToken = localStorage.getItem('portalToken') ?? ''
  const cachedToken = sessionStorage.getItem(CACHE_TOKEN_KEY) ?? ''
  if (currentToken !== cachedToken) {
    sessionStorage.removeItem(CACHE_KEY)
    sessionStorage.removeItem(CACHE_TOKEN_KEY)
    return null
  }
  const raw = sessionStorage.getItem(CACHE_KEY)
  if (!raw) return null
  try { return JSON.parse(raw) as PagePerms } catch { return null }
}

// 동시 호출(StrictMode 더블 마운트 등) 시 같은 URL 중복 fetch 방지
const inflightByUrl = new Map<string, Promise<boolean>>()

//   캐시 hit이면 네트워크 0, miss면 그 URL 1콜. 결과는 토큰 페어로 부분 캐싱.
export async function getPagePermission(pageUrl: string): Promise<boolean> {
  const cached = getValidCachedPerms()
  if (cached && pageUrl in cached) return cached[pageUrl]!

  const existing = inflightByUrl.get(pageUrl)
  if (existing) return existing

  const p = (async () => {
    const tokenAtStart = localStorage.getItem('portalToken') ?? ''
    const result = await fetchOne(pageUrl)
    // 동시 다른 URL 캐시 쓰기와 충돌 방지 — 최신 캐시 재조회 후 머지
    const latest = getValidCachedPerms() ?? {}
    latest[pageUrl] = result
    sessionStorage.setItem(CACHE_KEY, JSON.stringify(latest))
    sessionStorage.setItem(CACHE_TOKEN_KEY, tokenAtStart)
    return result
  })().finally(() => { inflightByUrl.delete(pageUrl) })

  inflightByUrl.set(pageUrl, p)
  return p
}

// 캐시 안에서 허용된 첫 페이지 (권한 거부 시 fallback redirect용)
export function firstAllowedPage(perms: PagePerms): string | null {
  for (const url of PAGE_URLS) {
    if (perms[url]) return url
  }
  return null
}

export async function pageAuthorityPreRegistration() {
    const token = localStorage.getItem('portalToken')
    if (!token) return false

    const claims = decodeJwtPayload<{ pageAuthorities?: string[] }>(token)
    if (!(claims?.pageAuthorities ?? []).includes('MARKET')) return false

    const marketStatus = await apiFetch('/api/v1/market/status', {
        method: 'POST',
        body: JSON.stringify({ marketId: '' })
    });

    const marketBrands = await apiFetch('/api/v1/market/brands', {
        method: 'POST',
        body: JSON.stringify({ query: '', marketId: '' }),
    })

    const marketStatusData = await marketStatus.json()
    const marketBrandsData = await marketBrands.json()

    sessionStorage.setItem('marketStatusResult', JSON.stringify(marketStatusData.result ?? []))
    sessionStorage.setItem('marketBrandsResult', JSON.stringify(marketBrandsData.result ?? []))
}

// 로그아웃·만료·재로그인 시 호출
export function clearPagePerms(): void {

    const sessionKeys = ['marketStatusResult', 'marketBrandsResult']

    sessionKeys.forEach(key => {
        sessionStorage.removeItem(key)
    })

    sessionStorage.removeItem(CACHE_KEY)
    sessionStorage.removeItem(CACHE_TOKEN_KEY)
}
