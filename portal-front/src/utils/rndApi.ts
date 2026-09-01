// R&D 채팅 API 공통 — 응답 성공 판정 + 에러 케이스별 안내 문구 (test.md 2026-06-16 백엔드 가이드)
//
// 모든 R&D API는 HTTP 200 + status:SUCCESS 라도 result.code !== 0이면 비즈니스 실패 → 동일하게 alert.
// 인증 만료(HTTP 401)는 apiFetch가 refresh→실패 시 자동 로그아웃 처리하므로 호출부에서 별도 분기 X.
// (errorKind 4종 분기 대신 "성공 아니면 해당 케이스 alert" 한 갈래로 단순화)

/** status:SUCCESS && result.code === 0 이 아니면 실패 (비즈니스/서버 에러 모두 false) */
export function isRndOk(data: unknown): boolean {
  if (!data || typeof data !== 'object') return false
  const d = data as { status?: unknown; result?: { code?: unknown } | null }
  return d.status === 'SUCCESS' && d.result?.code === 0
}

// 에러 케이스별 안내 문구 — 호출부에서 setAlertMessage(RND_ALERT.xxx).
// \n은 알림 모달(#modal-login-alert) dt의 whiteSpace:pre-line으로 줄바꿈됨.
// 문구/줄바꿈을 바꿀 일이 있으면 이 한 곳만 고치면 됨 (화면 컴포넌트와 분리).
// 형식: [목적어]\n[동사]하는 중 문제가 발생했습니다.\n[조치 안내]. — 3줄 통일
export const RND_ALERT = {
  sessionList: '채팅 히스토리 목록을\n불러오는 중 문제가 발생했습니다.\n페이지를 새로고침해 주세요.',
  query: '답변 생성 계획을\n진행하는 중 문제가 발생했습니다.\n질문 내용을 다시 전송해 주세요.',
  reject: '답변 생성 계획 수정 요청을\n처리하는 중 문제가 발생했습니다.\n수정 내용을 다시 전송해 주세요.',
  cancel: '답변 생성 계획 취소 요청을\n처리하는 중 문제가 발생했습니다.\n잠시 후 다시 시도해 주세요.',
  proceed: 'AI 분석 결과를\n생성하는 중 문제가 발생했습니다.\n잠시 후 다시 실행해 주세요.',
  log: '채팅 내용을 불러오는 중 문제가 발생했습니다.\n잠시 후 다시 시도해 주세요.',
  delete: '채팅 목록을 삭제하는 중 문제가 발생했습니다.\n잠시 후 다시 시도해 주세요.',
  rename: '채팅 이름을 변경하는 중 문제가 발생했습니다.\n잠시 후 다시 시도해 주세요.',
} as const
