import { useEffect, useRef, useState, type KeyboardEvent, type PointerEvent } from 'react'

import type { AnswerInspectionDetail, InspectionCall, JsonValue, LaneExecutionMap } from '../../utils/answerInspection'
import {
  matchInspectionCallsToTrace,
  tracePreservationNotice,
  traceSourceForInspectionLabel,
  type TraceMatch,
  type TraceToolResult,
  type UnnarratedRecord,
} from '../../utils/traceToolResults'
import {
  DEFAULT_INSPECTION_PANEL_WIDTH,
  INSPECTION_PANEL_WIDTH_STORAGE_KEY,
  MAX_INSPECTION_PANEL_WIDTH,
  MIN_INSPECTION_PANEL_WIDTH,
  clampInspectionPanelWidth,
  readInspectionPanelWidth,
} from '../../utils/inspectionPanelPreferences'
import {
  displayBackendText,
  displayInspectionStatus,
  displayParameterLabel,
  displaySourceLabel,
  inspectionCountLabels,
  sortedJsonEntries,
} from '../../utils/portalDisplayLabels'
import TracePayloadView from './TracePayloadView'
import DocumentToolDetail from './DocumentToolDetail'
import { evidenceAnchorId, inspectionCallContainsEvidence, inspectionCallEvidenceId, jsonEvidenceId } from '../../utils/evidenceAnchors'
import { EVIDENCE_NAVIGATION_TIMEOUT_MS, evidenceNavigationFailureMessage } from '../../utils/evidenceNavigation'
import type { MarketDetailLookup } from '../../utils/marketDetail'
import { LazyMarketDetail } from './MarketDetailView'

interface AnswerInspectionPanelProps {
  open: boolean
  answerLabel: string
  detail?: AnswerInspectionDetail
  laneExecutions?: LaneExecutionMap
  toolResults?: readonly TraceToolResult[]
  unnarratedRecords?: readonly UnnarratedRecord[]
  initiallyExpandedSequences?: readonly number[]
  focusLaneKey?: string
  focusEvidenceId?: string
  focusRequestId?: number
  detailLookup?: MarketDetailLookup
  onClose: () => void
}

interface ResizeSession {
  startX: number
  startWidth: number
}

interface CallGroup {
  key: string
  targetKey?: string
  label: string
  calls: InspectionCall[]
}

const HIDDEN_KEYS = new Set([
  'api_key', 'authorization', 'function_name', 'internal_url', 'query_sql', 'safe_url', 'sql', 'token', 'tool_name',
])

function displayJson(value: JsonValue): JsonValue {
  if (Array.isArray(value)) return value.map(displayJson)
  if (value !== null && typeof value === 'object') {
    return Object.fromEntries(sortedJsonEntries(value)
      .filter(([key]) => !HIDDEN_KEYS.has(key))
      .map(([key, item]) => [displayParameterLabel(key), displayJson(item)]))
  }
  if (typeof value === 'string') {
    if (/(?:\.svc(?:\.cluster\.local)?|localhost|127\.0\.0\.1)/i.test(value)) return '내부 주소 비공개'
    return displayBackendText(value).replace(/\[([^\]]+)]\((https?:\/\/[^)\s]+)\)/g, '$1 — $2')
  }
  return value
}

function jsonText(value: JsonValue): string {
  return JSON.stringify(displayJson(value), null, 2)
}

function outputCount(output: JsonValue | undefined, key: string): number | undefined {
  if (output === undefined || output === null || Array.isArray(output) || typeof output !== 'object') return undefined
  const value = output[key]
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : undefined
}

function groupCalls(calls: readonly InspectionCall[]): CallGroup[] {
  const groups = new Map<string, CallGroup>()
  for (const call of calls) {
    const targetKey = traceSourceForInspectionLabel(call.source_label)
    const key = targetKey ?? `label:${call.source_label}`
    const group = groups.get(key)
    if (group) group.calls.push(call)
    else groups.set(key, { key, targetKey, label: displaySourceLabel(call.source_label), calls: [call] })
  }
  for (const group of groups.values()) {
    group.calls.sort((left, right) => (left.trace_sequence ?? left.sequence) - (right.trace_sequence ?? right.sequence))
  }
  return [...groups.values()]
}

function CallCard({ call, expanded, traceMatch, hasToolResults, onToggle, nested = false, laneKey, laneExecutions, detailLookup, detailItemKey }: {
  call: InspectionCall
  expanded: boolean
  traceMatch?: TraceMatch
  hasToolResults: boolean
  onToggle: () => void
  nested?: boolean
  laneKey?: string
  laneExecutions?: LaneExecutionMap
  detailLookup?: MarketDetailLookup
  detailItemKey?: string
}) {
  const status = displayInspectionStatus(call.status, call.counts.returned)
  const bodyId = `answer-inspection-call-${call.sequence}`
  const returned = call.counts.returned
  const narrated = call.counts.narrated
  const unusedDifference = returned !== undefined && narrated !== undefined && returned > narrated ? returned - narrated : undefined
  const preservationNotice = traceMatch?.kind === 'matched' ? tracePreservationNotice(traceMatch.result.source) : undefined
  const displayedRecordCount = outputCount(call.output, 'displayed_record_count')
  const duplicateRecordsCollapsed = outputCount(call.output, 'duplicate_records_collapsed')
  const isDocumentTool = call.tool === 'document_sql' || call.tool === 'document_rag'
  const outputEvidenceId = jsonEvidenceId(call.output)
  const Root = nested ? 'div' : 'article'
  return (
    <Root
      id={evidenceAnchorId(inspectionCallEvidenceId(call))}
      className={`answer-inspection-call${nested ? ' is-lane-row' : ''}${expanded ? ' is-open' : ''}`}
      data-inspection-lane={nested ? undefined : laneKey}
      data-evidence-id={inspectionCallEvidenceId(call)}
    >
      <header><button type="button" className="answer-inspection-call-toggle" aria-controls={bodyId} aria-expanded={expanded} onClick={onToggle}>
        <span className="answer-inspection-sequence">{call.sequence}</span>
        <span className="answer-inspection-call-title"><strong>{displaySourceLabel(call.source_label)}</strong><span>
          <span className="answer-inspection-status" data-status={status.kind}>{status.label}</span>
          <span>{returned === undefined ? '수신 건수 미제공' : `${returned}건`}</span><span>{call.elapsed_seconds}초</span>
        </span></span><span className="answer-inspection-chevron" aria-hidden="true" />
      </button></header>
      <div className="answer-inspection-call-body" id={bodyId} hidden={!expanded}>
        {unusedDifference !== undefined && <div className="answer-inspection-unused" role="status">불러왔지만 답변에 쓰이지 않은 {unusedDifference}건</div>}
        <div className="answer-inspection-io">
          <section className="answer-inspection-io-section answer-inspection-input">
            <h5><span>INPUT</span> 보낸 것 <small>조회 상세</small></h5>
            <h6>질의어 (검색어)</h6><pre>{jsonText(call.request_parameters.query)}</pre>
            {call.request_parameters.calls !== undefined && <details className="answer-inspection-request"><summary>호출 파라미터 (호출 목록)</summary><pre>{jsonText(call.request_parameters.calls)}</pre></details>}
          </section>
          <section className="answer-inspection-io-section answer-inspection-output">
            <h5><span>OUTPUT</span> 받은 것 <small>집계: 조회 상세 · 반환 데이터: 실행 trace</small></h5>
            <dl className="answer-inspection-counts">
              {inspectionCountLabels().map(stage => <div key={stage.key}><dt>{stage.label}</dt><dd>{call.counts[stage.key] ?? '제공되지 않음'}</dd></div>)}
              <div><dt>미반영</dt><dd>{call.unused_count}</dd></div><div><dt>제외</dt><dd>{call.dropped_count}</dd></div>
            </dl>
            {(duplicateRecordsCollapsed !== undefined && duplicateRecordsCollapsed > 0) && <p className="answer-inspection-output-note">동일 항목 {duplicateRecordsCollapsed}건</p>}
            {(displayedRecordCount !== undefined && returned !== undefined && displayedRecordCount !== returned) && <p className="answer-inspection-output-note">표시 {displayedRecordCount}건 · 반환 {returned}건</p>}
            {isDocumentTool ? <DocumentToolDetail call={call} execution={call.tool ? laneExecutions?.[call.tool] : undefined} />
              : traceMatch?.kind === 'matched' ? <>{preservationNotice && <p className="answer-inspection-preservation">{preservationNotice}</p>}<TracePayloadView source={traceMatch.result.source} payload={traceMatch.result.payload} /></>
              : hasToolResults ? <p className="answer-inspection-unpreserved">실행 trace 대응 불가</p>
                : call.output === undefined ? <p className="answer-inspection-unpreserved">백엔드 미제공 · 원문 미보존</p>
                  : <details className="answer-inspection-request" id={outputEvidenceId ? evidenceAnchorId(outputEvidenceId) : undefined} data-evidence-id={outputEvidenceId}><summary>받은 내용 펼치기</summary><pre>{jsonText(call.output)}</pre></details>}
            <LazyMarketDetail lookup={detailLookup} itemKey={detailItemKey} />
          </section>
        </div>
        {call.drop_reasons.length > 0 && <div className="answer-inspection-reasons"><h5>단계별 미반영·제외 사유</h5><ul>{call.drop_reasons.map((reason, index) => <li key={`${reason.stage}-${index}`}><strong>{reason.stage}</strong><span>{reason.count}건</span><span>{reason.reason}{reason.record_ids !== undefined && <details className="answer-inspection-request answer-inspection-record-ids"><summary>제외 항목 식별자</summary><pre>{jsonText(reason.record_ids)}</pre></details>}</span></li>)}</ul></div>}
      </div>
    </Root>
  )
}

function displayUnnarratedReason(reason: string): string {
  if (reason === 'public_identifier_missing_from_final_prose') return '답변 본문에서 식별자를 확인할 수 없음'
  return '답변 본문에 반영되지 않음'
}

export default function AnswerInspectionPanel({ open, answerLabel, detail, laneExecutions = {}, toolResults = [], unnarratedRecords = [], initiallyExpandedSequences = [], focusLaneKey, focusEvidenceId, focusRequestId, detailLookup, onClose }: AnswerInspectionPanelProps) {
  const [expandedCalls, setExpandedCalls] = useState<ReadonlySet<number>>(() => new Set(initiallyExpandedSequences))
  const groups = groupCalls(detail?.calls ?? [])
  const detailItemKeys = new Map((detail?.calls ?? []).map((call, index) => [call.sequence, `inspection:${index}`]))
  const [expandedGroups, setExpandedGroups] = useState<ReadonlySet<string>>(() => new Set(groups.filter(group => group.calls.some(call => initiallyExpandedSequences.includes(call.sequence))).map(group => group.key)))
  const [panelWidth, setPanelWidth] = useState(() => typeof window === 'undefined'
    ? DEFAULT_INSPECTION_PANEL_WIDTH
    : readInspectionPanelWidth(window.localStorage))
  const [resizeSession, setResizeSession] = useState<ResizeSession | null>(null)
  const [fullscreen, setFullscreen] = useState(false)
  const [evidenceNavigationError, setEvidenceNavigationError] = useState<{ requestId?: number; message: string } | null>(null)
  const panelRef = useRef<HTMLElement>(null)
  useEffect(() => {
    if (!open || !focusLaneKey || typeof window === 'undefined') return undefined
    let target: HTMLElement | undefined
    let highlightTimeout: number | undefined
    const frame = window.requestAnimationFrame(() => {
      target = [...(panelRef.current?.querySelectorAll<HTMLElement>('[data-inspection-lane]') ?? [])]
        .find(element => element.dataset.inspectionLane === focusLaneKey)
      if (!target) return
      target.scrollIntoView({ behavior: 'smooth', block: 'center' })
      target.classList.remove('is-source-highlighted')
      void target.offsetWidth
      target.classList.add('is-source-highlighted')
      highlightTimeout = window.setTimeout(() => target?.classList.remove('is-source-highlighted'), 2000)
    })
    return () => {
      window.cancelAnimationFrame(frame)
      if (highlightTimeout !== undefined) window.clearTimeout(highlightTimeout)
      target?.classList.remove('is-source-highlighted')
    }
  }, [open, focusLaneKey, focusRequestId])
  useEffect(() => {
    if (!open || !focusEvidenceId || typeof window === 'undefined') return undefined
    const targetCall = detail?.calls.find(call => inspectionCallContainsEvidence(call, focusEvidenceId))
    let target: HTMLElement | undefined
    let innerFrame = 0
    let highlightTimeout: number | undefined
    let missingTimeout: number | undefined
    let observer: MutationObserver | undefined
    const revealTarget = (): boolean => {
      target = [...(panelRef.current?.querySelectorAll<HTMLElement>('[data-evidence-id]') ?? [])]
        .find(element => element.dataset.evidenceId === focusEvidenceId && element.closest('[hidden]') === null)
      if (!target) return false
      observer?.disconnect()
      if (missingTimeout !== undefined) window.clearTimeout(missingTimeout)
      target.scrollIntoView({ behavior: 'smooth', block: 'center' })
      target.classList.remove('is-source-highlighted')
      void target.offsetWidth
      target.classList.add('is-source-highlighted')
      highlightTimeout = window.setTimeout(() => target?.classList.remove('is-source-highlighted'), 2000)
      return true
    }

    const outerFrame = window.requestAnimationFrame(() => {
      if (targetCall) {
        setExpandedCalls(current => new Set([...current, targetCall.sequence]))
        const groupKey = traceSourceForInspectionLabel(targetCall.source_label) ?? `label:${targetCall.source_label}`
        setExpandedGroups(current => new Set([...current, groupKey]))
      }
      innerFrame = window.requestAnimationFrame(() => {
        if (revealTarget()) return
        const panel = panelRef.current
        if (panel) {
          observer = new MutationObserver(() => { revealTarget() })
          observer.observe(panel, { attributes: true, childList: true, subtree: true })
        }
        missingTimeout = window.setTimeout(() => {
          if (revealTarget()) return
          const message = evidenceNavigationFailureMessage(focusEvidenceId)
          setEvidenceNavigationError({ requestId: focusRequestId, message })
          console.warn(message)
          observer?.disconnect()
        }, EVIDENCE_NAVIGATION_TIMEOUT_MS)
      })
    })
    return () => {
      window.cancelAnimationFrame(outerFrame)
      window.cancelAnimationFrame(innerFrame)
      observer?.disconnect()
      if (missingTimeout !== undefined) window.clearTimeout(missingTimeout)
      if (highlightTimeout !== undefined) window.clearTimeout(highlightTimeout)
      target?.classList.remove('is-source-highlighted')
    }
  }, [detail, focusEvidenceId, focusRequestId, open])
  useEffect(() => {
    if (resizeSession === null || typeof window === 'undefined') return undefined
    const onPointerMove = (event: globalThis.PointerEvent): void => {
      setPanelWidth(clampInspectionPanelWidth(resizeSession.startWidth + resizeSession.startX - event.clientX))
    }
    const onPointerUp = (): void => {
      setPanelWidth(current => {
        window.localStorage.setItem(INSPECTION_PANEL_WIDTH_STORAGE_KEY, String(current))
        return current
      })
      setResizeSession(null)
    }
    window.addEventListener('pointermove', onPointerMove)
    window.addEventListener('pointerup', onPointerUp, { once: true })
    return () => {
      window.removeEventListener('pointermove', onPointerMove)
      window.removeEventListener('pointerup', onPointerUp)
    }
  }, [resizeSession])
  if (!open) return null
  const traceMatches = matchInspectionCallsToTrace(detail?.calls ?? [], toolResults)
  const allExpanded = detail !== undefined && detail.calls.length > 0 && detail.calls.every(call => expandedCalls.has(call.sequence))
  const toggleCall = (sequence: number): void => setExpandedCalls(current => {
    const next = new Set(current)
    if (next.has(sequence)) next.delete(sequence)
    else next.add(sequence)
    return next
  })
  const setAllExpanded = (expanded: boolean): void => {
    setExpandedCalls(expanded && detail ? new Set(detail.calls.map(call => call.sequence)) : new Set())
    setExpandedGroups(expanded ? new Set(groups.filter(group => group.calls.length > 1).map(group => group.key)) : new Set())
  }
  const toggleGroup = (key: string): void => setExpandedGroups(current => {
    const next = new Set(current)
    if (next.has(key)) next.delete(key)
    else next.add(key)
    return next
  })
  const startResize = (event: PointerEvent<HTMLDivElement>): void => {
    if (fullscreen || typeof window === 'undefined' || window.innerWidth <= 900) return
    event.preventDefault()
    setResizeSession({
      startX: event.clientX,
      startWidth: panelRef.current?.getBoundingClientRect().width ?? panelWidth,
    })
  }
  const resizeWithKeyboard = (event: KeyboardEvent<HTMLDivElement>): void => {
    if (fullscreen || (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight')) return
    event.preventDefault()
    const delta = event.key === 'ArrowLeft' ? 24 : -24
    setPanelWidth(current => {
      const next = clampInspectionPanelWidth(current + delta)
      if (typeof window !== 'undefined') window.localStorage.setItem(INSPECTION_PANEL_WIDTH_STORAGE_KEY, String(next))
      return next
    })
  }

  return <aside ref={panelRef} id="answer-inspection-panel" className={`answer-inspection-panel${fullscreen ? ' is-fullscreen' : ''}${resizeSession ? ' is-resizing' : ''}`} aria-labelledby="answer-inspection-title" style={!fullscreen ? { width: `${panelWidth}px` } : undefined}>
    <div className="answer-inspection-resize-handle" role="separator" aria-label="조회 상세 너비 조절" aria-orientation="vertical" aria-valuemin={MIN_INSPECTION_PANEL_WIDTH} aria-valuemax={MAX_INSPECTION_PANEL_WIDTH} aria-valuenow={panelWidth} tabIndex={0} onPointerDown={startResize} onKeyDown={resizeWithKeyboard} />
    <div className="answer-inspection-header"><div><h2 id="answer-inspection-title">조회 상세</h2><p>{answerLabel}</p></div><div className="answer-inspection-header-actions"><button type="button" className="answer-inspection-close" aria-label={fullscreen ? '조회 상세 전체 화면 종료' : '조회 상세 전체 화면'} onClick={() => setFullscreen(current => !current)}>{fullscreen ? '복원' : '전체 화면'}</button><button type="button" className="answer-inspection-close" aria-label="조회 상세 닫기" onClick={onClose}>닫기</button></div></div>
    {evidenceNavigationError !== null && evidenceNavigationError.requestId === focusRequestId && <div className="answer-inspection-navigation-error" role="alert">{evidenceNavigationError.message}</div>}
    <div className="answer-inspection-content">{!detail ? <div className="answer-inspection-empty" role="status"><strong>조회 상세가 제공되지 않았습니다</strong><p>이 답변에는 조회 상세 필드가 포함되어 있지 않습니다.</p></div> : <div className="answer-inspection-detail">
      {detail.expansion !== null && <section className="answer-inspection-section" aria-labelledby="answer-inspection-expansion"><h3 id="answer-inspection-expansion">질문 확장</h3><pre>{jsonText(detail.expansion)}</pre></section>}
      {unnarratedRecords.length > 0 && <section className="answer-inspection-section answer-inspection-unnarrated" aria-labelledby="answer-inspection-unnarrated"><h3 id="answer-inspection-unnarrated">본문 미반영 항목</h3><details><summary>미반영 {unnarratedRecords.length}건 보기</summary><ol>{unnarratedRecords.map((record, index) => <li key={`${record.record_id}-${index}`}><code>{record.record_id}</code><span>{displayUnnarratedReason(record.reason_code)}</span></li>)}</ol></details></section>}
      <section className="answer-inspection-section" aria-labelledby="answer-inspection-calls"><div className="answer-inspection-section-heading"><div><h3 id="answer-inspection-calls">도구 호출 {detail.calls.length}건</h3>{detail.trace_correlation && <p className="answer-inspection-correlation">실행 trace 대응 {detail.trace_correlation.matched}/{detail.trace_correlation.total}</p>}</div>{detail.calls.length > 0 && <button type="button" onClick={() => setAllExpanded(!allExpanded)}>{allExpanded ? '전체 접기' : '전체 펼치기'}</button>}</div>
        {detail.calls.length === 0 ? <p className="answer-inspection-none">도구 호출이 없습니다.</p> : groups.map(group => {
          if (group.calls.length === 1) {
            const call = group.calls[0]!
            return <CallCard key={`${call.sequence}-${call.source_label}`} call={call} laneKey={group.targetKey} laneExecutions={laneExecutions} expanded={expandedCalls.has(call.sequence)} traceMatch={traceMatches.get(call.sequence)} hasToolResults={toolResults.length > 0} onToggle={() => toggleCall(call.sequence)} detailLookup={detailLookup} detailItemKey={detailItemKeys.get(call.sequence)} />
          }
          const groupExpanded = expandedGroups.has(group.key)
          const totalReturned = group.calls.every(call => call.counts.returned !== undefined) ? group.calls.reduce((sum, call) => sum + (call.counts.returned ?? 0), 0) : undefined
          const totalElapsed = group.calls.reduce((sum, call) => sum + call.elapsed_seconds, 0)
          return <article className={`answer-inspection-lane${groupExpanded ? ' is-open' : ''}`} data-lane-call-count={group.calls.length} data-inspection-lane={group.targetKey} key={group.key}>
            <header><button type="button" className="answer-inspection-call-toggle" aria-expanded={groupExpanded} onClick={() => toggleGroup(group.key)}><span className="answer-inspection-call-title"><strong>{group.label}</strong><span><span>호출 {group.calls.length}회</span><span>{totalReturned === undefined ? '총 반환 미제공' : `총 반환 ${totalReturned}건`}</span><span>총 소요 {Number(totalElapsed.toFixed(3))}초</span></span></span><span className="answer-inspection-chevron" aria-hidden="true" /></button></header>
            <div className="answer-inspection-lane-body" hidden={!groupExpanded}>{group.calls.map(call => <CallCard key={`${call.sequence}-${call.source_label}`} call={call} nested laneExecutions={laneExecutions} expanded={expandedCalls.has(call.sequence)} traceMatch={traceMatches.get(call.sequence)} hasToolResults={toolResults.length > 0} onToggle={() => toggleCall(call.sequence)} detailLookup={detailLookup} detailItemKey={detailItemKeys.get(call.sequence)} />)}</div>
          </article>
        })}
      </section>
    </div>}</div>
  </aside>
}
