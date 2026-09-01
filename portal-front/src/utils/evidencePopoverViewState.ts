import type { MarketDetailResponse } from './marketDetail.ts'

export interface EvidencePopoverViewState {
  detailExpanded: boolean
  detail: MarketDetailResponse | null
  expandedLongFields: ReadonlySet<string>
}

export function createEvidencePopoverViewKey({
  conversationId,
  responseId,
  itemKey,
}: {
  conversationId: string
  responseId: string
  itemKey: string
}): string {
  return `${conversationId}\u0000${responseId}\u0000${itemKey}`
}

export function emptyEvidencePopoverViewState(): EvidencePopoverViewState {
  return {
    detailExpanded: false,
    detail: null,
    expandedLongFields: new Set<string>(),
  }
}

export function setEvidenceDetailExpanded(
  state: EvidencePopoverViewState,
  detailExpanded: boolean,
): EvidencePopoverViewState {
  return { ...state, detailExpanded }
}

export function setEvidenceDetail(
  state: EvidencePopoverViewState,
  detail: MarketDetailResponse,
): EvidencePopoverViewState {
  return { ...state, detail, detailExpanded: true }
}

export function setEvidenceLongFieldExpanded(
  state: EvidencePopoverViewState,
  path: string,
  expanded: boolean,
): EvidencePopoverViewState {
  const expandedLongFields = new Set(state.expandedLongFields)
  if (expanded) expandedLongFields.add(path)
  else expandedLongFields.delete(path)
  return { ...state, expandedLongFields }
}
