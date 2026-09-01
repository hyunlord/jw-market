import { useState } from 'react'

import type { JsonValue } from '../../utils/answerInspection'
import { displayBackendText, displayParameterLabel } from '../../utils/portalDisplayLabels'
import {
  emptyEvidencePopoverViewState,
  setEvidenceDetail,
  setEvidenceDetailExpanded,
  setEvidenceLongFieldExpanded,
  type EvidencePopoverViewState,
} from '../../utils/evidencePopoverViewState'
import { fetchMarketDetail, type MarketDetailLookup, type MarketDetailResponse } from '../../utils/marketDetail'
import { StructuredValueTree } from './StructuredValueTree'

interface MarketDetailViewProps {
  detail: MarketDetailResponse
  expandedLongFields?: ReadonlySet<string>
  onLongFieldExpandedChange?: (path: string, expanded: boolean) => void
}

interface LazyMarketDetailProps {
  lookup?: MarketDetailLookup
  itemKey?: string
  viewState?: EvidencePopoverViewState
  onViewStateChange?: (state: EvidencePopoverViewState) => void
}

function objectValue(value: JsonValue | undefined): Record<string, JsonValue> | undefined {
  return value !== undefined && value !== null && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, JsonValue>
    : undefined
}

function isEmptyValue(value: JsonValue): boolean {
  return value === null || value === '' || (Array.isArray(value) && value.length === 0)
}

function textValue(value: JsonValue): string {
  if (isEmptyValue(value)) return '-'
  if (typeof value === 'string') return displayBackendText(value)
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  return JSON.stringify(value, null, 2)
}

function fieldLabel(path: string): { primary: string; source: string } {
  const segment = path.split('.').at(-1) ?? path
  const match = segment.match(/^(.*?)(?:\[(\d+)\])?$/)
  const key = match?.[1] || segment
  const index = match?.[2] === undefined ? undefined : Number(match[2]) + 1
  const translated = displayParameterLabel(key)
  const suffix = index === undefined ? '' : ` ${index}`
  const humanizedPath = path.replace(/\[(\d+)\]/g, (_value, rawIndex: string) => ` ${Number(rawIndex) + 1}`)
  return {
    primary: translated === key ? humanizedPath : `${translated}${suffix}`,
    source: humanizedPath,
  }
}

function missingReason(detail: MarketDetailResponse, path: string, value: JsonValue): string | undefined {
  return detail.fieldMetadata.missingFields[path]
    ?? detail.fieldMetadata.missingFields[`detail.${path}`]
    ?? detail.fieldMetadata.missingFields[`record.${path}`]
    ?? (isEmptyValue(value) ? '원천 응답에 값이 없습니다.' : undefined)
}

const RECORD_CONTAINER = /(?:^|_)record$/i

export function MarketDetailView({ detail }: MarketDetailViewProps) {
  const detailObject = objectValue(detail.detail)
  const recordEntry = detailObject
    ? Object.entries(detailObject).find(([key, value]) => RECORD_CONTAINER.test(key) && value !== null && typeof value === 'object')
    : undefined
  const recordValue = recordEntry ? recordEntry[1] : detail.detail
  const metadataValue = recordEntry && detailObject
    ? Object.fromEntries(Object.entries(detailObject).filter(([key]) => key !== recordEntry[0]))
    : undefined
  const labelFor = (_key: string, path: string) => fieldLabel(path)
  const missingReasonFor = (path: string, value: JsonValue) => missingReason(detail, path, value)

  return <div className="market-detail-view">
    <section><h4>인풋</h4><dl className="market-detail-summary">
      <div><dt>질의어</dt><dd>{detail.input?.query || '-'}</dd></div>
      <div><dt>호출 파라미터</dt><dd><pre>{textValue(detail.input?.requestParameters ?? null)}</pre></dd></div>
      <div><dt>확장 등급</dt><dd>{detail.input?.expansionGrade || '-'}</dd></div>
    </dl></section>
    <section><h4>아웃풋</h4><dl className="market-detail-summary">
      <div><dt>수신 건수</dt><dd>{detail.output?.receivedCount === undefined ? '-' : `${detail.output.receivedCount}건`}</dd></div>
      <div><dt>직접 관련 건수</dt><dd>{detail.output?.directlyRelevantCount === undefined ? '-' : `${detail.output.directlyRelevantCount}건`}</dd></div>
      <div><dt>응답 요지</dt><dd><pre>{textValue(detail.output?.summary ?? null)}</pre></dd></div>
      <div><dt>호출 시각</dt><dd>{detail.output?.calledAt || '-'}</dd></div>
      <div><dt>소요</dt><dd>{detail.output?.elapsedMs === undefined ? '-' : `${detail.output.elapsedMs.toLocaleString('ko-KR')}ms`}</dd></div>
    </dl></section>
    <section><h4>원천 레코드 전 필드</h4><div className="trace-output-view market-detail-tree"><StructuredValueTree value={recordValue} labelFor={labelFor} showEveryArrayItem missingReasonFor={missingReasonFor} /></div></section>
    {metadataValue && Object.keys(metadataValue).length > 0 && <details className="market-detail-internal-fields">
      <summary>조회 메타데이터 {Object.keys(metadataValue).length}개</summary>
      <div className="trace-output-view market-detail-tree"><StructuredValueTree value={metadataValue} labelFor={labelFor} showEveryArrayItem missingReasonFor={missingReasonFor} /></div>
    </details>}
    <p className="market-detail-field-count">공개 필드 {detail.fieldMetadata.publicFieldCount ?? '-'}개 · 내부 필드 {detail.fieldMetadata.hiddenFieldCount}개 비표시</p>
    {detail.fieldMetadata.hiddenFieldNotice && <p className="market-detail-notice">{detail.fieldMetadata.hiddenFieldNotice}</p>}
    {detail.partial && <p className="market-detail-notice" role="status">응답 크기 제한으로 일부 필드만 제공되었습니다.</p>}
  </div>
}

export function LazyMarketDetail({ lookup, itemKey, viewState, onViewStateChange }: LazyMarketDetailProps) {
  const available = Boolean(lookup && itemKey && lookup.itemKeys.has(itemKey))
  const [localViewState, setLocalViewState] = useState<EvidencePopoverViewState>(emptyEvidencePopoverViewState)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const currentViewState = viewState ?? localViewState
  const updateViewState = onViewStateChange ?? setLocalViewState

  if (!available || !lookup || !itemKey) return null
  const load = async (): Promise<void> => {
    updateViewState(setEvidenceDetailExpanded(currentViewState, true))
    setLoading(true)
    setError(null)
    try {
      updateViewState(setEvidenceDetail(currentViewState, await fetchMarketDetail(lookup, itemKey)))
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '상세 원문을 불러오지 못했습니다.')
    } finally {
      setLoading(false)
    }
  }
  const toggle = (): void => {
    if (currentViewState.detailExpanded) {
      updateViewState(setEvidenceDetailExpanded(currentViewState, false))
      return
    }
    if (currentViewState.detail) {
      updateViewState(setEvidenceDetailExpanded(currentViewState, true))
      return
    }
    void load()
  }

  return <section className="market-detail-lazy" aria-live="polite">
    <button type="button" className="market-detail-toggle" aria-expanded={currentViewState.detailExpanded} onClick={toggle}>{currentViewState.detailExpanded ? '전체 상세 접기' : '전체 상세 펼치기'}</button>
    {currentViewState.detailExpanded && <div className="market-detail-lazy-body">
      {loading && <p role="status">상세 원문을 불러오는 중입니다.</p>}
      {error && <div className="market-detail-error" role="alert"><p>{error}</p><button type="button" onClick={() => { void load() }}>다시 시도</button></div>}
      {currentViewState.detail && <MarketDetailView
        detail={currentViewState.detail}
        expandedLongFields={currentViewState.expandedLongFields}
        onLongFieldExpandedChange={(path, expanded) => updateViewState(setEvidenceLongFieldExpanded(currentViewState, path, expanded))}
      />}
    </div>}
  </section>
}
