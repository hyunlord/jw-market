// 시장분석(Market) 채팅 API 공통 — 성공 판정 + 에러 안내 문구.
// BACK_MARKET_CHAT.md 기준. R&D(rndApi)와 판정 로직은 동일(status:SUCCESS && result.code===0)이나
// Market 전용 문구/실동작 엔드포인트만 다뤄서 별도 파일로 분리(페이지가 따로라 문구도 독립 관리).
//   plan(proceed/reject/cancel)·보고서·파일업로드는 백엔드 미지원이라 여기 문구도 없음.

/** status:SUCCESS && result.code === 0 이 아니면 실패 (rndApi와 동일 판정) */
export function isMarketOk(data: unknown): boolean {
  if (!data || typeof data !== 'object') return false
  const d = data as { status?: unknown; result?: { code?: unknown } | null }
  return d.status === 'SUCCESS' && d.result?.code === 0
}

export const MARKET_ALERT = {
  sessionList: '채팅 히스토리 목록을\n불러오는 중 문제가 발생했습니다.\n페이지를 새로고침해 주세요.',
  query: '답변을 생성하는 중 문제가 발생했습니다.\n질문 내용을 다시 전송해 주세요.',
  rateLimited: '요청이 너무 잦습니다.\n잠시 후 다시 시도해 주세요.',
  log: '채팅 내용을 불러오는 중 문제가 발생했습니다.\n잠시 후 다시 시도해 주세요.',
  delete: '채팅 목록을 삭제하는 중 문제가 발생했습니다.\n잠시 후 다시 시도해 주세요.',
  rename: '채팅 이름을 변경하는 중 문제가 발생했습니다.\n잠시 후 다시 시도해 주세요.',
} as const

/**
 * 스트리밍 질문이 실패했을 때 보여줄 문구.
 *
 * 429(계정별 요청 빈도 제한)는 MARKET_ALERT.query("다시 전송해 주세요")가 부정확하다 —
 * 지금 다시 보내면 또 막힌다. Retry-After 헤더가 있으면 남은 시간까지 알려준다.
 *
 * 그 외 상태코드는 기존 문구를 그대로 유지한다(거동 무변경).
 *
 * 주의: Retry-After 는 동일 출처 응답에서만 읽힌다. 교차 출처로 바뀌면
 * Access-Control-Expose-Headers 가 필요하며, 없으면 초 없는 기본 문구로 자연 강등된다.
 */
export function marketStreamFailureAlert(res: Response): string {
  if (res.status !== 429) return MARKET_ALERT.query

  const raw = res.headers.get('Retry-After')?.trim()
  const seconds = raw && /^\d+$/.test(raw) ? Number(raw) : 0
  return seconds > 0
    ? `요청이 너무 잦습니다.\n${seconds}초 후에 다시 시도해 주세요.`
    : MARKET_ALERT.rateLimited
}

// Market 채팅 엔드포인트. 관리(pin/unpin/rename/delete)는 R&D 경로 재활용이라 여기 없음.
export const MARKET_CHAT_API = {
  query: '/api/v1/market/chat/query',
  queryStream: '/api/v1/market/chat/query/stream',   // SSE 스트리밍 (§10). body: { question, conversationId }
  abort: '/api/v1/market/chat/abort',
  session: '/api/v1/market/chat/session',
  sessionPinned: '/api/v1/market/chat/session/pinned',
  log: '/api/v1/rnd/chat/log',
} as const
