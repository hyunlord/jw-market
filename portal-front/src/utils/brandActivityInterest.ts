export type StrategicInterestViewKind = 'strategic_ml' | 'strategic_cd'

export type StrategicInterestMarket = {
  readonly viewKind: StrategicInterestViewKind
  readonly marketId: string
  readonly marketName: string
}

type StorageReader = {
  readonly getItem: (key: string) => string | null
}

type InterestSeriesRequestInput = {
  readonly selectedBrand: string
  readonly market: StrategicInterestMarket | null
  readonly visit?: string
  readonly specialty?: string
  readonly csdMarket?: string
}

export type InterestDisplayItem = {
  readonly key: string
  readonly is_selected?: boolean
}

export function selectInterestDisplayItem<TItem extends InterestDisplayItem>(
  items: readonly TItem[],
  requestedKey: string,
): TItem | null {
  return items.find(item => item.key === requestedKey)
    ?? items.find(item => item.is_selected)
    ?? items[0]
    ?? null
}

export type InterestSeriesRequest = {
  readonly view: StrategicInterestViewKind
  readonly market_id: string
  readonly selected_brand: string
  readonly visit_location: readonly string[]
  readonly specialty: readonly string[]
  readonly csd_market?: string
  readonly filters: {
    readonly atc: { readonly atc4: readonly string[] }
    readonly channel: {
      readonly visit_location: readonly string[]
      readonly specialty: readonly string[]
    }
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function nonEmptyString(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() !== '' ? value.trim() : undefined
}

export function readStrategicInterestMarkets(
  brand: string,
  viewKind: StrategicInterestViewKind,
  fallback: StrategicInterestMarket | null,
  storage: StorageReader = sessionStorage,
): readonly StrategicInterestMarket[] {
  const unique = new Map<string, StrategicInterestMarket>()
  if (fallback) unique.set(fallback.marketId, fallback)

  const raw = storage.getItem('marketBrandsResult')
  if (!raw) return [...unique.values()]

  try {
    const parsed: unknown = JSON.parse(raw)
    if (!Array.isArray(parsed)) return [...unique.values()]

    for (const entry of parsed) {
      if (!isRecord(entry) || entry.brand !== brand || !Array.isArray(entry.contexts)) continue
      for (const context of entry.contexts) {
        if (!isRecord(context) || context.view_kind !== viewKind || context.has_market_data === false) continue
        const marketId = nonEmptyString(context.market_id)
        const marketName = nonEmptyString(context.market_name)
        if (marketId && marketName) unique.set(marketId, { viewKind, marketId, marketName })
      }
    }
  } catch (error) {
    console.warn('brand_activity_market_catalog_parse_failed', error)
    return [...unique.values()]
  }

  return [...unique.values()]
}

export class StrategicInterestMarketRequiredError extends Error {
  constructor() {
    super('시장 정보를 불러오지 못했습니다.')
    this.name = 'StrategicInterestMarketRequiredError'
  }
}

export class StrategicInterestMarketIdentityError extends Error {
  constructor(market: StrategicInterestMarket) {
    super(`market identity does not match catalog view kind: ${market.viewKind}/${market.marketId}`)
    this.name = 'StrategicInterestMarketIdentityError'
  }
}

function catalogViewForMarket(market: StrategicInterestMarket): StrategicInterestViewKind {
  if (!market.viewKind) throw new StrategicInterestMarketIdentityError(market)
  return market.viewKind
}

export function buildInterestSeriesRequest(input: InterestSeriesRequestInput): InterestSeriesRequest {
  if (!input.market) throw new StrategicInterestMarketRequiredError()

  const visitLocation = input.visit && input.visit !== '전체' ? [input.visit] : []
  const specialty = input.specialty && input.specialty !== '전체' ? [input.specialty] : []
  return {
    view: catalogViewForMarket(input.market),
    market_id: input.market.marketId,
    selected_brand: input.selectedBrand,
    visit_location: visitLocation,
    specialty,
    ...(input.csdMarket && input.csdMarket !== 'all' ? { csd_market: input.csdMarket } : {}),
    filters: {
      atc: { atc4: [] },
      channel: { visit_location: visitLocation, specialty },
    },
  }
}

function boundedReason(value: string): string {
  const printable = [...value]
    .map(character => {
      const code = character.charCodeAt(0)
      return code <= 31 || code === 127 ? ' ' : character
    })
    .join('')
  return printable.replace(/\s+/g, ' ').trim().slice(0, 300)
}

function payloadReason(payload: unknown): string | undefined {
  const queue: unknown[] = [payload]
  while (queue.length > 0) {
    const value = queue.shift()
    if (typeof value === 'string') {
      const reason = boundedReason(value)
      if (reason) return reason
      continue
    }
    if (!isRecord(value)) continue
    for (const key of ['message', 'detail', 'error', 'reason'] as const) {
      const candidate = value[key]
      if (typeof candidate === 'string') {
        const reason = boundedReason(candidate)
        if (reason) return reason
      }
    }
    queue.push(value.detail, value.result, value.data)
  }
  return undefined
}

export function interestErrorReason(status: number, body: string): string {
  let payload: unknown = body
  try {
    payload = JSON.parse(body) as unknown
  } catch {
    // Plain-text error bodies are valid BFF responses and remain bounded below.
  }
  return payloadReason(payload) ?? `HTTP ${status}`
}
