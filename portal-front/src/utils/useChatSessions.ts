import { useEffect, useState, useRef } from 'react'
import { apiFetch } from './apiFetch'
import { isRndOk, RND_ALERT } from './rndApi'
import { useToast } from '../context/ToastContext'

// R&D 사이드바 채팅 세션 목록 + 관리(고정/해제/이름변경/삭제/선택삭제)의 단일 소스.
// MainPage·McpServerPage 두 페이지가 공용으로 사용 (이전엔 MainPage 인라인 + 훅 중복이었음 → 통합).

export interface ChatItem {
  uid: string
  title: string
  date: string
  pinned?: boolean
  sortTs?: number
}

interface SessionRaw {
  uid: string
  title: string
  first_user_message?: string
  last_user_request?: string
}

interface SessionListResult {
  code: number
  message: string
  data: { list: unknown[]; has_next?: boolean }
}

const PAGE_SIZE = 20

interface SessionListApiResponse {
  result: SessionListResult
  status: string
}

function extractSessions(result: SessionListResult): SessionRaw[] {
  const list = result.data?.list
  if (!Array.isArray(list)) return []
  return list.filter((item): item is SessionRaw =>
    typeof item === 'object' && item !== null && 'uid' in item && 'title' in item
  )
}

// ⚠️ 타임존 없는 시각의 기준이 백엔드마다 다름:
//   R&D(/rnd/chat/session)=KST(+09:00), Market(/market/chat/session)=UTC(Z). assumeUtc로 분기.
function parseServerDate(raw: string, assumeUtc: boolean): Date {
  const s = raw.trim().replace(' ', 'T')
  const hasTz = /([zZ]|[+-]\d{2}:?\d{2})$/.test(s)
  return new Date(hasTz ? s : `${s}${assumeUtc ? 'Z' : '+09:00'}`)
}

// 사이드바 표시 날짜 — 오늘/N일 전/날짜
function formatSessionDate(session: SessionRaw, assumeUtc: boolean): string {
  const raw = session.last_user_request
  if (!raw) return ''
  const d = parseServerDate(raw, assumeUtc)
  if (isNaN(d.getTime())) return ''
  const today = new Date()
  const todayMidnight = new Date(today.getFullYear(), today.getMonth(), today.getDate())
  const dMidnight = new Date(d.getFullYear(), d.getMonth(), d.getDate())
  const diffDays = Math.round((todayMidnight.getTime() - dMidnight.getTime()) / 86_400_000)
  if (diffDays === 0) {
    const hours = d.getHours()
    const ampm = hours < 12 ? '오전' : '오후'
    const displayHour = hours % 12 === 0 ? 12 : hours % 12
    const minutes = String(d.getMinutes()).padStart(2, '0')
    return `오늘, ${ampm} ${displayHour}:${minutes}`
  }
  if (diffDays <= 5) return `${diffDays}일 전`
  return `${d.getFullYear()}.${String(d.getMonth() + 1).padStart(2, '0')}.${String(d.getDate()).padStart(2, '0')}`
}

// title 우선, 없으면 first_user_message (백엔드 title이 임의 길이로 잘려 ellipsis 안 걸리는 케이스 회피)
function pickDisplayTitle(s: SessionRaw): string {
  return (s.title ?? '').trim() || (s.first_user_message ?? '').trim() || ''
}

function sessionSortTs(s: SessionRaw, assumeUtc: boolean): number {
  const raw = s.last_user_request
  if (!raw) return 0
  const d = parseServerDate(raw, assumeUtc)
  return isNaN(d.getTime()) ? 0 : d.getTime()
}

interface UseChatSessionsOptions {
  // 목록/CRUD 실패 시 알림 메시지 — 각 페이지의 알림 모달 상태로 라우팅 (기본 noop)
  onError?: (msg: string) => void
  // ★ 목록 엔드포인트 주입 — Market 채팅은 목록만 /market/chat/session 사용 (고정 목록·관리는 R&D 재활용).
  listEndpoint?: string
  pinnedEndpoint?: string
  serverDatesUtc?: boolean
}

export interface UseChatSessions {
  pinnedList: ChatItem[]
  normalList: ChatItem[]
  /** 새 세션을 일반 목록 맨 앞에 추가 (첫 메시지로 세션 발급 시) */
  prependSession: (item: ChatItem) => void
  /** 일반 목록에 다음 페이지가 더 있는지 (무한 스크롤용) */
  normalHasNext: boolean
  /** 다음 페이지 로딩 중 (하단 스피너용) */
  loadingMore: boolean
  /** 일반 목록 다음 페이지 이어 붙이기 (사이드바 스크롤 바닥 근처에서 호출) */
  loadMore: () => void
  pinChat: (uid: string) => Promise<void>
  unpinChat: (uid: string) => Promise<void>
  renameChat: (uid: string, title: string) => Promise<boolean>
  deleteChat: (uid: string) => Promise<boolean>
  bulkDelete: (uids: string[]) => Promise<boolean>
}

export function useChatSessions({
  onError = () => {},
  listEndpoint = '/api/v1/rnd/chat/session',
  pinnedEndpoint = '/api/v1/rnd/chat/session/pinned',
  serverDatesUtc = false,
}: UseChatSessionsOptions = {}): UseChatSessions {
  const { showToast } = useToast()
  // 엔드포인트는 마운트 시 고정 — effect/loadMore deps 오염 방지 위해 ref 캡처 (onError 패턴과 동일)
  const listEndpointRef = useRef(listEndpoint)
  const pinnedEndpointRef = useRef(pinnedEndpoint)
  const serverUtcRef = useRef(serverDatesUtc)
  const [pinnedList, setPinnedList] = useState<ChatItem[]>([])
  const [normalList, setNormalList] = useState<ChatItem[]>([])
  const [normalHasNext, setNormalHasNext] = useState(false)  // 일반 목록 다음 페이지 존재 여부
  const [loadingMore, setLoadingMore] = useState(false)      // 다음 페이지 로딩 중
  const normalPageRef = useRef(1)          // 현재까지 로드한 일반 목록 페이지
  const loadingMoreRef = useRef(false)     // loadMore 연타/스크롤 중복 호출 가드 (동기)
  const submittingRef = useRef(false)   // 연타 가드 — delete/rename/bulk 중복 호출 방지

  // onError는 렌더마다 새 함수일 수 있어 ref로 최신값 유지 (effect deps 오염 방지)
  const onErrorRef = useRef(onError)
  useEffect(() => { onErrorRef.current = onError }, [onError])

  useEffect(() => {
    const loadSessions = async () => {
      try {
        const [pinnedRes, normalRes] = await Promise.all([
          apiFetch(pinnedEndpointRef.current, {
            method: 'POST',
            body: JSON.stringify({ page: 1, pageSize: 20, orderBy: '-reg_date' }),
          }),
          apiFetch(listEndpointRef.current, {
            method: 'POST',
            body: JSON.stringify({ page: 1, pageSize: 20, orderBy: '-last_user_request', excludePinned: true }),
          }),
        ])
        const [pinnedData, normalData]: [SessionListApiResponse, SessionListApiResponse] =
          await Promise.all([pinnedRes.json(), normalRes.json()])
        if (!isRndOk(pinnedData) || !isRndOk(normalData)) {
          onErrorRef.current(RND_ALERT.sessionList)
          return
        }
        setPinnedList(extractSessions(pinnedData.result).map(s => ({
          uid: s.uid, title: pickDisplayTitle(s), date: formatSessionDate(s, serverUtcRef.current), pinned: true, sortTs: sessionSortTs(s, serverUtcRef.current),
        })))
        setNormalList(extractSessions(normalData.result).map(s => ({
          uid: s.uid, title: pickDisplayTitle(s), date: formatSessionDate(s, serverUtcRef.current), pinned: false, sortTs: sessionSortTs(s, serverUtcRef.current),
        })))
        setNormalHasNext(!!normalData.result.data?.has_next)
      } catch { onErrorRef.current(RND_ALERT.sessionList) }
    }
    loadSessions()
  }, [])

  // 무한 스크롤 — 일반 목록 다음 페이지를 이어 붙임. 중복 호출 가드 + uid 중복 제거.
  //   ⚠️ 로드 실패는 조용히(알림 X) — hasNext를 안 내려 다음 스크롤에 자동 재시도.
  const loadMore = async () => {
    if (loadingMoreRef.current || !normalHasNext) return
    loadingMoreRef.current = true
    setLoadingMore(true)
    const nextPage = normalPageRef.current + 1
    try {
      const res = await apiFetch(listEndpointRef.current, {
        method: 'POST',
        body: JSON.stringify({ page: nextPage, pageSize: PAGE_SIZE, orderBy: '-last_user_request', excludePinned: true }),
      })
      const data = await res.json() as SessionListApiResponse
      if (!isRndOk(data)) return
      normalPageRef.current = nextPage
      setNormalHasNext(!!data.result.data?.has_next)
      const more = extractSessions(data.result).map(s => ({
        uid: s.uid, title: pickDisplayTitle(s), date: formatSessionDate(s, serverUtcRef.current), pinned: false, sortTs: sessionSortTs(s, serverUtcRef.current),
      }))
      setNormalList(prev => {
        const seen = new Set(prev.map(c => c.uid))
        return [...prev, ...more.filter(c => !seen.has(c.uid))]
      })
    } catch { /* 조용히 무시 — 다음 스크롤에 재시도 */ }
    finally { loadingMoreRef.current = false; setLoadingMore(false) }
  }

  const prependSession = (item: ChatItem) =>
    setNormalList(prev => [{ ...item, sortTs: item.sortTs ?? Date.now() }, ...prev])

  const pinChat = async (uid: string) => {
    try {
      await apiFetch('/api/v1/rnd/chat/session/pin', {
        method: 'PUT',
        body: JSON.stringify({ chat_session_uid: uid }),
      })
      const target = normalList.find(c => c.uid === uid)
      if (target) {
        setNormalList(prev => prev.filter(c => c.uid !== uid))
        setPinnedList(prev => [{ ...target, pinned: true }, ...prev])
      }
    } catch { showToast('고정 처리 중 오류가 발생했습니다.') }
  }

  const unpinChat = async (uid: string) => {
    try {
      await apiFetch('/api/v1/rnd/chat/session/unpin', {
        method: 'PUT',
        body: JSON.stringify({ chat_session_uid: uid }),
      })
      const target = pinnedList.find(c => c.uid === uid)
      if (target) {
        setPinnedList(prev => prev.filter(c => c.uid !== uid))
        setNormalList(prev => {
          const item = { ...target, pinned: false }
          const ts = item.sortTs ?? 0
          const idx = prev.findIndex(c => (c.sortTs ?? 0) < ts)
          return idx === -1 ? [...prev, item] : [...prev.slice(0, idx), item, ...prev.slice(idx)]
        })
      }
    } catch { showToast('고정 해제 중 오류가 발생했습니다.') }
  }

  const renameChat = async (uid: string, title: string): Promise<boolean> => {
    if (submittingRef.current) return false
    submittingRef.current = true
    try {
      const res = await apiFetch('/api/v1/rnd/chat/session/rename', {
        method: 'PUT',
        body: JSON.stringify({ uid, title }),
      })
      if (!isRndOk(await res.json() as unknown)) { onErrorRef.current(RND_ALERT.rename); return false }
      const update = (list: ChatItem[]) => list.map(c => c.uid === uid ? { ...c, title } : c)
      setPinnedList(update)
      setNormalList(update)
      showToast('이름이 변경되었습니다.')
      return true
    } catch { onErrorRef.current(RND_ALERT.rename); return false } finally { submittingRef.current = false }
  }

  const deleteChat = async (uid: string): Promise<boolean> => {
    if (submittingRef.current) return false
    submittingRef.current = true
    try {
      const res = await apiFetch('/api/v1/rnd/chat/session/delete', {
        method: 'PUT',
        body: JSON.stringify({ uid }),
      })
      if (!isRndOk(await res.json() as unknown)) { onErrorRef.current(RND_ALERT.delete); return false }
      setPinnedList(prev => prev.filter(c => c.uid !== uid))
      setNormalList(prev => prev.filter(c => c.uid !== uid))
      showToast('채팅 목록이 삭제되었습니다.')
      return true
    } catch { onErrorRef.current(RND_ALERT.delete); return false } finally { submittingRef.current = false }
  }

  const bulkDelete = async (uids: string[]): Promise<boolean> => {
    if (submittingRef.current) return false
    submittingRef.current = true
    try {
      const results = await Promise.all(uids.map(uid =>
        apiFetch('/api/v1/rnd/chat/session/delete', {
          method: 'PUT',
          body: JSON.stringify({ uid }),
        })
      ))
      const datas = await Promise.all(results.map(r => r.json() as Promise<unknown>))
      if (!datas.every(isRndOk)) { onErrorRef.current(RND_ALERT.delete); return false }
      setPinnedList(prev => prev.filter(c => !uids.includes(c.uid)))
      setNormalList(prev => prev.filter(c => !uids.includes(c.uid)))
      showToast('선택한 채팅이 삭제되었습니다.')
      return true
    } catch { onErrorRef.current(RND_ALERT.delete); return false } finally { submittingRef.current = false }
  }

  return { pinnedList, normalList, prependSession, normalHasNext, loadingMore, loadMore, pinChat, unpinChat, renameChat, deleteChat, bulkDelete }
}
