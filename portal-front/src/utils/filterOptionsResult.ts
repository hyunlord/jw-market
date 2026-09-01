import type { FilterOptionsResponse } from '../types/market'

export type FilterOptionsFetchResult =
  | { ok: true; data: FilterOptionsResponse }
  | { ok: false; reason: 'http' | 'contract' | 'network'; status?: number }

export function parseFilterOptionsFetchResult(
  responseOk: boolean,
  responseStatus: number,
  payload: unknown,
): FilterOptionsFetchResult {
  if (!responseOk) {
    return { ok: false, reason: 'http', status: responseStatus }
  }
  if (
    typeof payload !== 'object'
    || payload === null
    || !('status' in payload)
    || payload.status !== 'SUCCESS'
    || !('result' in payload)
    || typeof payload.result !== 'object'
    || payload.result === null
  ) {
    return { ok: false, reason: 'contract', status: responseStatus }
  }
  return { ok: true, data: payload.result as FilterOptionsResponse }
}
