export type DeepAnalysisViewKind = 'general' | 'strategic_ml' | 'strategic_cd'
export type DeepAnalysisSource = 'UBIST' | 'IQVIA'

type DeepAnalysisApiSource = Lowercase<DeepAnalysisSource>

export type DeepAnalysisFormalContext = {
  readonly viewKind: DeepAnalysisViewKind
  readonly marketId?: string
  readonly source: DeepAnalysisSource
}

export type DeepAnalysisAvailableContext = {
  readonly viewKind?: string
  readonly marketId?: string
  readonly source?: DeepAnalysisApiSource
}

export type DeepAnalysisRequestError = {
  readonly status: number
  readonly code: string
  readonly message: string
  readonly availableContexts: readonly DeepAnalysisAvailableContext[]
}

export type DeepAnalysisRequestBody =
  | {
      readonly brandName: string
      readonly view: string
    }
  | {
      readonly brandName: string
      readonly view_kind: DeepAnalysisViewKind
      readonly market_id?: string
      readonly source: DeepAnalysisApiSource
    }

type StorageReader = {
  readonly getItem: (key: string) => string | null
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function nonEmptyString(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() !== '' ? value : undefined
}

function apiSource(value: DeepAnalysisSource): DeepAnalysisApiSource {
  return value === 'IQVIA' ? 'iqvia' : 'ubist'
}

function parseApiSource(value: unknown): DeepAnalysisApiSource | undefined {
  return value === 'ubist' || value === 'iqvia' ? value : undefined
}

function errorDetail(payload: unknown): Record<string, unknown> {
  const queue: unknown[] = [payload]
  while (queue.length > 0) {
    const candidate = queue.shift()
    if (!isRecord(candidate)) continue
    if (
      nonEmptyString(candidate.error)
      || nonEmptyString(candidate.code)
      || Array.isArray(candidate.available_contexts)
    ) {
      return candidate
    }
    queue.push(candidate.detail, candidate.result, candidate.data)
  }
  return {}
}

function parseAvailableContexts(value: unknown): readonly DeepAnalysisAvailableContext[] {
  if (!Array.isArray(value)) return []
  const contexts: DeepAnalysisAvailableContext[] = []
  for (const candidate of value) {
    if (!isRecord(candidate)) continue
    contexts.push({
      viewKind: nonEmptyString(candidate.view_kind),
      marketId: nonEmptyString(candidate.market_id),
      source: parseApiSource(candidate.source),
    })
  }
  return contexts
}

export function buildDeepAnalysisRequest(
  brand: string,
  view: string,
  context?: DeepAnalysisFormalContext,
): DeepAnalysisRequestBody {
  if (!context) return { brandName: brand, view }
  return {
    brandName: brand,
    view_kind: context.viewKind,
    ...(context.marketId ? { market_id: context.marketId } : {}),
    source: apiSource(context.source),
  }
}

export function deepAnalysisRequestKey(
  brand: string,
  viewKind: DeepAnalysisViewKind,
  source: DeepAnalysisSource,
  marketId?: string,
): string {
  return [brand, viewKind, source, marketId ?? 'auto'].join('|')
}

export function readDeepAnalysisCatalog(
  storage: StorageReader = sessionStorage,
): readonly unknown[] {
  const raw = storage.getItem('marketBrandsResult')
  if (!raw) return []
  try {
    const parsed: unknown = JSON.parse(raw)
    return Array.isArray(parsed) ? parsed : []
  } catch {
    return []
  }
}

export function resolveDeepAnalysisMarketId(
  catalog: readonly unknown[],
  brand: string,
  viewKind: DeepAnalysisViewKind,
  source?: DeepAnalysisSource,
): string | undefined {
  const entries = catalog.filter(
    (candidate): candidate is Record<string, unknown> =>
      isRecord(candidate) && candidate.brand === brand,
  )
  const sourceValue = source ? apiSource(source) : undefined
  const contextIds = new Set<string>()
  let hasViewContexts = false

  for (const entry of entries) {
    if (!Array.isArray(entry.contexts)) continue
    for (const candidate of entry.contexts) {
      if (!isRecord(candidate) || candidate.view_kind !== viewKind) continue
      hasViewContexts = true
      const candidateSource = parseApiSource(candidate.source)
      if (sourceValue && candidateSource && candidateSource !== sourceValue) continue
      const marketId = nonEmptyString(candidate.market_id)
      if (marketId) contextIds.add(marketId)
    }
  }

  if (contextIds.size === 1) return contextIds.values().next().value
  if (contextIds.size > 1 || hasViewContexts) return undefined

  // The catalog's top-level market_id is the legacy strategy_* identifier,
  // not the formal strategic_ml/strategic_cd id. Let the backend resolve it.
  if (viewKind !== 'general') return undefined

  const generalIds = new Set<string>()
  for (const entry of entries) {
    if (!Array.isArray(entry.atc_codes) || entry.atc_codes.length !== 1) continue
    const marketId = nonEmptyString(entry.atc_codes[0])
    if (marketId) generalIds.add(marketId)
  }
  return generalIds.size === 1 ? generalIds.values().next().value : undefined
}

export function parseDeepAnalysisError(status: number, payload: unknown): DeepAnalysisRequestError {
  const detail = errorDetail(payload)
  return {
    status,
    code: nonEmptyString(detail.error) ?? nonEmptyString(detail.code) ?? 'analysis_unavailable',
    message: nonEmptyString(detail.message) ?? '분석 데이터를 불러오지 못했습니다.',
    availableContexts: parseAvailableContexts(detail.available_contexts),
  }
}

export function formatDeepAnalysisError(error: DeepAnalysisRequestError): string {
  const availableSources = new Set(error.availableContexts.map(context => context.source).filter(Boolean))
  const labels = (['ubist', 'iqvia'] as const)
    .filter(source => availableSources.has(source))
    .map(source => source.toUpperCase())

  if (error.code === 'source_not_available' && labels.length === 1) {
    return `이 브랜드는 ${labels[0]}만 제공됩니다.`
  }
  if (error.status === 409 || error.code === 'ambiguous_market_context') {
    const contextLabels = Array.from(new Set(error.availableContexts.map(context => {
      const source = context.source?.toUpperCase()
      if (source && context.marketId) return `${source} ${context.marketId}`
      return source ?? context.marketId
    }).filter((value): value is string => Boolean(value))))
    const suffix = contextLabels.length > 0 ? ` 선택 가능한 시장: ${contextLabels.join(', ')}` : ''
    return `분석할 시장을 하나로 정할 수 없습니다.${suffix}`
  }
  return error.message
}
