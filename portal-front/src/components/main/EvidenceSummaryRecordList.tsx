import { useState } from 'react'

import type { EvidenceDisplayRecord } from '../../utils/answerSections'
import {
  evidenceSummaryRows,
  evidenceSummarySourceKey,
  evidenceSummaryValue,
  recordSummaryForSource,
} from '../../utils/evidenceSummaryFields'

export interface EvidenceSummaryRecordItem {
  readonly evidenceId: string
  readonly record: EvidenceDisplayRecord
}

export default function EvidenceSummaryRecordList({ records, onSelectEvidence }: {
  records: readonly EvidenceSummaryRecordItem[]
  onSelectEvidence: (evidenceId: string) => void
}) {
  const [openRecords, setOpenRecords] = useState<ReadonlySet<string>>(() => {
    const initiallyOpen = records.length <= 2 ? records : records.slice(0, 1)
    return new Set(initiallyOpen.map(item => item.evidenceId))
  })
  const setEveryRecord = (open: boolean): void => setOpenRecords(open ? new Set(records.map(item => item.evidenceId)) : new Set())

  return <section className="trace-output-array answer-evidence-summary-records" data-total-count={records.length}>
    <header className="trace-record-toolbar">
      <strong>총 {records.length}건</strong>
      <span><button type="button" data-tree-action="expand" onClick={() => setEveryRecord(true)}>전체 펼치기</button><button type="button" data-tree-action="collapse" onClick={() => setEveryRecord(false)}>전체 접기</button></span>
    </header>
    <div className="trace-record-list">
      {records.map((item, index) => {
        const sourceKey = evidenceSummarySourceKey(item.record.source_name, item.evidenceId, item.record.record)
        const rows = evidenceSummaryRows(sourceKey, item.record.record)
        const summary = recordSummaryForSource(sourceKey, item.record.record)
        const identifier = summary.identifier ?? item.record.identifier
        return <details
          className="trace-record-block"
          key={item.evidenceId}
          open={openRecords.has(item.evidenceId)}
          onToggle={event => {
            const isOpen = event.currentTarget.open
            setOpenRecords(current => {
              if (current.has(item.evidenceId) === isOpen) return current
              const next = new Set(current)
              if (isOpen) next.add(item.evidenceId); else next.delete(item.evidenceId)
              return next
            })
            if (isOpen) onSelectEvidence(item.evidenceId)
          }}
        >
          <summary data-record-identifier={identifier}><span className="trace-record-ordinal">#{index + 1}</span><strong>{identifier}</strong>{summary.summary && <span>{summary.summary}</span>}</summary>
          <div className="trace-record-content">
            <table className="answer-evidence-summary-table"><tbody>{rows.map(row => <tr key={row.label} data-source-field={row.key} data-missing={row.missing || undefined}><th scope="row">{row.label}</th><td>{evidenceSummaryValue(row.value)}</td></tr>)}</tbody></table>
          </div>
        </details>
      })}
    </div>
  </section>
}
