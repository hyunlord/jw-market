export const EVIDENCE_NAVIGATION_TIMEOUT_MS = 1600

export function evidenceNavigationFailureMessage(evidenceId: string): string {
  return `해당 근거 항목을 찾을 수 없습니다(${evidenceId})`
}
