import type { StrategicInterestMarket, StrategicInterestViewKind } from './brandActivityInterest'

export type GeneralBrandActivityScope = {
  readonly view: 'general'
}

export type StrategicBrandActivityScope = {
  readonly view: StrategicInterestViewKind
  readonly marketId: string
}

export type BrandActivityScope = GeneralBrandActivityScope | StrategicBrandActivityScope

export const GENERAL_BRAND_ACTIVITY_SCOPE: GeneralBrandActivityScope = { view: 'general' }

export function brandActivityScopeKey(scope: BrandActivityScope): string {
  return scope.view === 'general' ? scope.view : `${scope.view}:${scope.marketId}`
}

export function resolveBrandActivityScope(
  market: StrategicInterestMarket | undefined,
): BrandActivityScope {
  if (!market) return GENERAL_BRAND_ACTIVITY_SCOPE
  return { view: market.viewKind, marketId: market.marketId }
}

type GeneralFilters = Readonly<Record<string, unknown>>

type GeneralScopeRequest<TFilters extends GeneralFilters> = {
  readonly view: 'general'
  readonly filters: TFilters
}

type StrategicScopeRequest = {
  readonly view: StrategicInterestViewKind
  readonly market_id: string
  readonly filters: Readonly<Record<string, never>>
}

export function buildBrandActivityScopeRequest<TFilters extends GeneralFilters>(
  scope: BrandActivityScope,
  generalFilters: TFilters,
): GeneralScopeRequest<TFilters> | StrategicScopeRequest {
  if (scope.view === 'general') {
    return { view: scope.view, filters: generalFilters }
  }
  return {
    view: scope.view,
    market_id: scope.marketId,
    filters: {},
  }
}
