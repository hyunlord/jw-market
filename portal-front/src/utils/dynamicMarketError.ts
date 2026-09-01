export class DynamicMarketRequestError extends Error {
  readonly status: number
  readonly code: string
  readonly availableContexts: readonly Record<string, unknown>[]

  constructor(
    status: number,
    code: string,
    availableContexts: readonly Record<string, unknown>[] = [],
  ) {
    super(`Dynamic market request failed (${status}, ${code})`)
    this.name = 'DynamicMarketRequestError'
    this.status = status
    this.code = code
    this.availableContexts = availableContexts
  }
}

function errorDetail(payload: unknown): Record<string, unknown> {
  if (typeof payload !== 'object' || payload === null) return {}
  const record = payload as Record<string, unknown>
  return typeof record.detail === 'object' && record.detail !== null
    ? record.detail as Record<string, unknown>
    : record
}

export function parseDynamicMarketError(status: number, payload: unknown): DynamicMarketRequestError {
  const detail = errorDetail(payload)
  const code = typeof detail.error === 'string' ? detail.error : 'unknown_error'
  const availableContexts = Array.isArray(detail.available_contexts)
    ? detail.available_contexts.filter(
      (item): item is Record<string, unknown> => typeof item === 'object' && item !== null,
    )
    : []
  return new DynamicMarketRequestError(status, code, availableContexts)
}

function availableSourceLabels(contexts: readonly Record<string, unknown>[]): string[] {
  const labels = contexts
    .map(context => typeof context.source === 'string' ? context.source.trim().toLowerCase() : '')
    .filter(Boolean)
    .map(source => source === 'iqvia' || source === 'iqvia_nsa' ? 'IQVIA' : source.toUpperCase())
  return [...new Set(labels)]
}

export function dynamicMarketErrorMessage(error: unknown): string {
  if (error instanceof DynamicMarketRequestError) {
    if (error.code === 'dynamic_scope_too_broad') {
      return '시장 데이터가 너무 많습니다.\nATC 범위를 좁히거나 다시 조회해 주세요.'
    }
    if (error.code === 'invalid_dynamic_market_request') {
      return '선택한 ATC 조합을 조회할 수 없습니다.\n하위 ATC 항목을 다시 선택해 주세요.'
    }
    if (error.code === 'source_not_available') {
      const sources = availableSourceLabels(error.availableContexts)
      const suffix = sources.length > 0 ? `\n사용 가능한 원천: ${sources.join(', ')}` : ''
      return `선택한 전략뷰에서는 요청한 원천을 사용할 수 없습니다.${suffix}`
    }
  }
  return '시장 데이터를 불러오지 못했습니다.\n잠시 후 다시 조회해 주세요.'
}
