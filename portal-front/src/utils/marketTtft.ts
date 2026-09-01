export function captureFirstTtft(
  currentTtftMs: number | undefined,
  requestStartedAtMs: number,
  firstAnswerAtMs: number,
): number {
  return currentTtftMs ?? Math.max(0, firstAnswerAtMs - requestStartedAtMs)
}

export function formatTtft(ttftMs: number): string {
  return `첫 응답 ${(ttftMs / 1_000).toFixed(1)}초`
}
