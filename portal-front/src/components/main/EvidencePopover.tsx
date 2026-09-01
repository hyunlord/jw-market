import { useEffect, useId, useState } from 'react'

import type { EvidenceDisplayCatalog, EvidenceGroup } from '../../utils/answerSections'
import { displayBackendText, displaySourceLabel } from '../../utils/portalDisplayLabels'
import { evidencePopoverRecord, evidenceSourceKeyForRecord, evidenceSourceTabIndex, groupEvidenceSources } from '../../utils/evidencePopover'
import type { MarketDetailLookup } from '../../utils/marketDetail'
import type { EvidencePopoverViewState } from '../../utils/evidencePopoverViewState'
import { evidenceSummaryRows, evidenceSummarySourceKey } from '../../utils/evidenceSummaryFields'
import { LazyMarketDetail } from './MarketDetailView'
import { StructuredValueTree } from './StructuredValueTree'
import EvidenceSummaryRecordList from './EvidenceSummaryRecordList'

interface EvidenceReference {
  evidenceId: string
  label: string
}

interface Props {
  evidenceId: string
  evidence: readonly EvidenceReference[]
  catalog?: EvidenceDisplayCatalog
  group?: EvidenceGroup
  detailLookup?: MarketDetailLookup
  viewState?: EvidencePopoverViewState
  onViewStateChange?: (state: EvidencePopoverViewState) => void
  onClose: () => void
  onSelectEvidence: (evidenceId: string) => void
}

export default function EvidencePopover({ evidenceId, evidence, catalog, group, detailLookup, viewState, onViewStateChange, onClose, onSelectEvidence }: Props) {
  const record = evidencePopoverRecord(catalog, evidenceId)
  const compactEvidenceRecord = record ? [{ identifier: record.identifier, ...record.record }] : []
  const summaryRecords = [...new Set(evidence.map(item => item.evidenceId))]
    .flatMap(itemEvidenceId => {
      const itemRecord = evidencePopoverRecord(catalog, itemEvidenceId)
      return itemRecord ? [{ evidenceId: itemEvidenceId, record: itemRecord }] : []
    })
  const hasRegisteredSummaryRecords = summaryRecords.length > 0 && summaryRecords.every(item => {
    const sourceKey = evidenceSummarySourceKey(item.record.source_name, item.evidenceId, item.record.record)
    return sourceKey !== undefined && evidenceSummaryRows(sourceKey, item.record.record).some(row => !row.missing)
  })
  const groupedSources = group ? groupEvidenceSources(group, catalog) : []
  const tabSetId = useId()
  const [sourceSelection, setSourceSelection] = useState({
    groupId: group?.groupId,
    sourceKey: evidenceSourceKeyForRecord(groupedSources, evidenceId) ?? groupedSources[0]?.sourceKey,
  })
  const selectedSourceKey = sourceSelection.groupId === group?.groupId ? sourceSelection.sourceKey : undefined
  const activeSourceKey = selectedSourceKey && groupedSources.some(source => source.sourceKey === selectedSourceKey)
    ? selectedSourceKey
    : evidenceSourceKeyForRecord(groupedSources, evidenceId) ?? groupedSources[0]?.sourceKey
  const activeSource = groupedSources.find(source => source.sourceKey === activeSourceKey) ?? groupedSources[0]
  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent): void => {
      if (event.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', closeOnEscape)
    return () => window.removeEventListener('keydown', closeOnEscape)
  }, [onClose])

  return <aside className="answer-evidence-popover" role="dialog" aria-modal="false" aria-labelledby="answer-evidence-popover-title">
    <header><div><span>근거</span><h2 id="answer-evidence-popover-title">{record ? displaySourceLabel(record.source_name) : '근거 확인 실패'}</h2></div><button type="button" aria-label="근거 닫기" onClick={onClose}>닫기</button></header>
    <div className="answer-evidence-popover-scroll">
    {record ? <div className="answer-evidence-popover-body">
      <dl className="answer-evidence-summary">
        <div><dt>실체 식별자</dt><dd>{record.identifier}</dd></div>
        <div><dt>질의어</dt><dd>{record.query || '-'}</dd></div>
        <div><dt>수신/직접 관련</dt><dd>{record.counts.received}건 / {record.counts.direct_related === null ? '직접 관련 값 미제공' : `${record.counts.direct_related}건`}</dd></div>
      </dl>
      <section><h3>{group ? '대표 근거 상세' : '해당 근거 레코드'}</h3>{hasRegisteredSummaryRecords
        ? <EvidenceSummaryRecordList records={summaryRecords} onSelectEvidence={onSelectEvidence} />
        : <StructuredValueTree value={compactEvidenceRecord} labelFor={key => ({ primary: displayBackendText(key), source: key })} showEveryArrayItem initialOpenRecordCount={compactEvidenceRecord.length <= 2 ? compactEvidenceRecord.length : 1} />}</section>
      <LazyMarketDetail lookup={detailLookup} itemKey={evidenceId} viewState={viewState} onViewStateChange={onViewStateChange} />
      {group ? <section className="answer-evidence-related"><h3>관련 출처 전체</h3>{groupedSources.length > 0 ? <>
        <div className="answer-evidence-source-tabs" role="tablist" aria-label="근거 원천">
          {groupedSources.map((source, index) => <button
            key={source.sourceKey}
            id={`${tabSetId}-tab-${index}`}
            type="button"
            role="tab"
            aria-selected={source.sourceKey === activeSource?.sourceKey}
            aria-controls={`${tabSetId}-panel-${index}`}
            tabIndex={source.sourceKey === activeSource?.sourceKey ? 0 : -1}
            onClick={() => setSourceSelection({ groupId: group.groupId, sourceKey: source.sourceKey })}
            onKeyDown={event => {
              const nextIndex = evidenceSourceTabIndex(event.key, index, groupedSources.length)
              if (nextIndex === undefined) return
              event.preventDefault()
              const nextSource = groupedSources[nextIndex]
              if (!nextSource) return
              setSourceSelection({ groupId: group.groupId, sourceKey: nextSource.sourceKey })
              event.currentTarget.parentElement?.querySelectorAll<HTMLButtonElement>('[role="tab"]')[nextIndex]?.focus()
            }}
          >{source.sourceLabel} <span>{source.count}건</span></button>)}
        </div>
        {activeSource && <div
          id={`${tabSetId}-panel-${groupedSources.indexOf(activeSource)}`}
          className="answer-evidence-source-group"
          role="tabpanel"
          aria-labelledby={`${tabSetId}-tab-${groupedSources.indexOf(activeSource)}`}
        >
          <h4>{activeSource.sourceLabel} 식별자</h4>
          {activeSource.items.length > 0 ? <ul>{activeSource.items.map(item => <li key={item.evidenceId}><button type="button" className={item.evidenceId === evidenceId ? 'active' : ''} aria-pressed={item.evidenceId === evidenceId} onClick={() => onSelectEvidence(item.evidenceId)}><span>{item.identifier}</span>{!item.available && <small>상세 데이터 없음</small>}</button></li>)}</ul> : <p>표시할 근거 항목이 없습니다.</p>}
        </div>}
      </> : <p>관련 출처 분류 데이터가 없습니다.</p>}</section>
        : evidence.length > 1 && <section><h3>이 문장의 다른 근거</h3><ul>{evidence.filter(item => item.evidenceId !== evidenceId).map(item => <li key={item.evidenceId}><button type="button" onClick={() => onSelectEvidence(item.evidenceId)}>{item.label}</button></li>)}</ul></section>}
    </div> : <div className="answer-evidence-popover-error" role="alert"><strong>해당 근거를 표시할 수 없습니다({evidenceId})</strong><dl><div><dt>원천</dt><dd>{evidence.find(item => item.evidenceId === evidenceId)?.label ?? '출처 미제공'}</dd></div><div><dt>식별자</dt><dd>{evidenceId}</dd></div><div><dt>질의어</dt><dd>-</dd></div></dl></div>}
    </div>
    <footer className="answer-evidence-popover-footer"><span>Esc 키로 닫을 수 있습니다.</span><button type="button" onClick={onClose}>닫기</button></footer>
  </aside>
}
