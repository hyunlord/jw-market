import { useId, useRef, useState } from 'react'

import type { JsonValue } from '../../utils/answerInspection'
import { displayBackendText } from '../../utils/portalDisplayLabels'
import { recordHeaderFor, shouldShowRecordIndex } from '../../utils/structuredRecordTree'

export const LONG_STRUCTURED_VALUE_THRESHOLD = 160

export interface StructuredValueLabel {
  readonly primary: string
  readonly source?: string
}

function PrimitiveValue({ value }: { value: null | boolean | number | string }) {
  const [copyStatus, setCopyStatus] = useState<'idle' | 'copied' | 'failed'>('idle')
  if (value === null) return <span>-</span>
  if (typeof value !== 'string') return <span>{String(value)}</span>
  const displayed = displayBackendText(value)
  if (displayed.length <= LONG_STRUCTURED_VALUE_THRESHOLD) return <span className="trace-output-value-text">{displayed}</span>
  const copyValue = async (): Promise<void> => {
    if (typeof navigator === 'undefined' || navigator.clipboard === undefined) { setCopyStatus('failed'); return }
    try { await navigator.clipboard.writeText(displayed); setCopyStatus('copied') } catch { setCopyStatus('failed') }
  }
  return <div className="trace-output-long-value" data-full-value-length={displayed.length}>
    <details><summary>긴 값 펼치기 ({displayed.length}자)</summary><span className="trace-output-value-text">{displayed}</span></details>
    <button type="button" onClick={() => void copyValue()}>{copyStatus === 'copied' ? '복사됨' : copyStatus === 'failed' ? '복사 실패' : '복사'}</button>
  </div>
}

export function StructuredValueTree({
  value,
  labelFor,
  entriesFor = Object.entries,
  depth = 0,
  path = '',
  showEveryArrayItem = false,
  initialOpenRecordCount = 0,
  recordSource,
  missingReasonFor,
}: {
  value: JsonValue
  labelFor: (key: string, path: string) => StructuredValueLabel
  entriesFor?: (value: { [key: string]: JsonValue }) => readonly (readonly [string, JsonValue])[]
  depth?: number
  path?: string
  showEveryArrayItem?: boolean
  initialOpenRecordCount?: number
  recordSource?: string
  missingReasonFor?: (path: string, value: JsonValue) => string | undefined
}) {
  const treeId = useId().replace(/:/g, '')
  const recordRefs = useRef(new Map<number, HTMLDetailsElement>())
  const [openRecords, setOpenRecords] = useState<ReadonlySet<number>>(() => new Set(
    Array.from({ length: Math.min(initialOpenRecordCount, Array.isArray(value) ? value.length : 0) }, (_, index) => index),
  ))
  if (value === null || typeof value !== 'object') return <><PrimitiveValue value={value} />{missingReasonFor?.(path, value) && <small className="market-detail-missing-reason">{missingReasonFor(path, value)}</small>}</>
  if (Array.isArray(value)) {
    const arrayKey = path.split('.').at(-1)?.replace(/\[\d+\]$/, '') || 'item'
    const arrayLabel = labelFor(arrayKey, path).primary
    const headers = value.map((item, index) => recordHeaderFor(item, index, arrayLabel, recordSource))
    const setEveryRecord = (open: boolean): void => {
      setOpenRecords(open ? new Set(value.map((_, index) => index)) : new Set())
    }
    const openAndMoveTo = (index: number): void => {
      setOpenRecords(current => new Set([...current, index]))
      requestAnimationFrame(() => recordRefs.current.get(index)?.scrollIntoView({ block: 'start', behavior: 'smooth' }))
    }
    return <section className="trace-output-array" data-total-count={value.length}>
      <header className="trace-record-toolbar">
        <strong>총 {value.length}건</strong>
        <span><button type="button" data-tree-action="expand" onClick={() => setEveryRecord(true)}>전체 펼치기</button><button type="button" data-tree-action="collapse" onClick={() => setEveryRecord(false)}>전체 접기</button></span>
      </header>
      {shouldShowRecordIndex(value.length) && <nav className="trace-record-index" aria-label={`${arrayLabel} 식별자 목록`}>
        <strong>식별자 목록</strong>
        <ol>{headers.map((header, index) => <li key={`${header.identifier}-${index}`}><button type="button" data-record-target={`${treeId}-${index}`} onClick={() => openAndMoveTo(index)}><span>{header.ordinal}</span>{header.identifier}</button></li>)}</ol>
      </nav>}
      <div className="trace-record-list">
        {value.map((item, index) => {
          const header = headers[index]
          return <details
            className="trace-record-block"
            id={`${treeId}-${index}`}
            key={`${header.identifier}-${index}`}
            open={openRecords.has(index)}
            ref={node => { if (node) recordRefs.current.set(index, node); else recordRefs.current.delete(index) }}
            onToggle={event => {
              const isOpen = event.currentTarget.open
              setOpenRecords(current => {
                if (current.has(index) === isOpen) return current
                const next = new Set(current)
                if (isOpen) next.add(index); else next.delete(index)
                return next
              })
            }}
          >
            <summary data-record-identifier={header.identifier}><span className="trace-record-ordinal">{header.ordinal}</span><strong>{header.identifier}</strong><span>{header.summary}</span></summary>
            <div className="trace-record-content"><StructuredValueTree value={item} labelFor={labelFor} entriesFor={entriesFor} depth={depth + 1} path={`${path}[${index}]`} showEveryArrayItem={showEveryArrayItem} recordSource={recordSource} missingReasonFor={missingReasonFor} /></div>
          </details>
        })}
      </div>
    </section>
  }
  return <div className={`trace-output-object trace-output-depth-${Math.min(depth, 3)}`}>
    {entriesFor(value).map(([key, item]) => {
      const itemPath = path ? `${path}.${key}` : key
      const label = labelFor(key, itemPath)
      const nested = item !== null && typeof item === 'object'
      return <section className={nested ? 'trace-output-branch' : 'trace-output-scalar'} key={key}>
        <div className="trace-output-field-label" title={label.source}><span className="market-detail-field-primary">{label.primary}</span>{label.source && label.source !== label.primary && <small className="market-detail-field-source">{label.source}</small>}</div>
        <div className="trace-output-field-value"><StructuredValueTree value={item} labelFor={labelFor} entriesFor={entriesFor} depth={depth + 1} path={itemPath} showEveryArrayItem={showEveryArrayItem} recordSource={recordSource} missingReasonFor={missingReasonFor} /></div>
      </section>
    })}
  </div>
}
