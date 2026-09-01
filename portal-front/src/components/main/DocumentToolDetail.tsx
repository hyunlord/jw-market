import { useState } from 'react'

import type { InspectionCall, JsonValue, LaneExecution } from '../../utils/answerInspection'
import { evidenceAnchorId, jsonEvidenceId } from '../../utils/evidenceAnchors'

const NOT_PROVIDED = '이 항목은 원천에서 제공되지 않았습니다'

function isObject(value: JsonValue | undefined): value is { [key: string]: JsonValue } {
  return value !== undefined && value !== null && typeof value === 'object' && !Array.isArray(value)
}

function stringValue(value: JsonValue | undefined): string | undefined {
  return typeof value === 'string' && value.trim() ? value : undefined
}

function numberValue(value: JsonValue | undefined): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined
}

function chunkScoreLabel(chunk: { [key: string]: JsonValue }): string {
  const scoreKind = stringValue(chunk.score_kind)?.toLowerCase()
  const distance = numberValue(chunk.distance)
  const score = numberValue(chunk.score)
  const similarityScore = numberValue(chunk.similarity_score)

  if (distance !== undefined) return `distance ${distance} (낮을수록 유사)`
  if (score !== undefined) return `score ${score} (높을수록 유사)`
  if (scoreKind === 'bm25' && similarityScore !== undefined) {
    return `score ${similarityScore} (높을수록 유사)`
  }
  if (similarityScore !== undefined) {
    return `similarity_score ${similarityScore} (방향 정보 미제공)`
  }
  return NOT_PROVIDED
}

function displayValue(value: JsonValue | undefined): string {
  if (value === undefined) return NOT_PROVIDED
  if (value === null) return 'null'
  if (typeof value === 'number') return value.toLocaleString('ko-KR')
  if (typeof value === 'boolean') return value ? 'true' : 'false'
  if (typeof value === 'string') return value
  return JSON.stringify(value)
}

function LaneStatus({ execution, fallbackState }: { execution?: LaneExecution; fallbackState?: string }) {
  return <dl className="document-tool-status" aria-label="파일 도구 실행 상태">
    <div><dt>planned</dt><dd>{execution === undefined ? NOT_PROVIDED : execution.planned ? 'true' : 'false'}</dd></div>
    <div><dt>state</dt><dd>{execution?.state ?? fallbackState ?? NOT_PROVIDED}</dd></div>
    <div><dt>reason_code</dt><dd>{execution === undefined ? NOT_PROVIDED : execution.reason_code ?? '사유 없음'}</dd></div>
  </dl>
}

function CopyableSql({ sql, evidenceId }: { sql?: string; evidenceId?: string }) {
  const [copyStatus, setCopyStatus] = useState<'idle' | 'copied' | 'failed'>('idle')
  const copy = async (): Promise<void> => {
    if (!sql || typeof navigator === 'undefined' || navigator.clipboard === undefined) {
      setCopyStatus('failed')
      return
    }
    try {
      await navigator.clipboard.writeText(sql)
      setCopyStatus('copied')
    } catch {
      setCopyStatus('failed')
    }
  }
  return <section className="document-tool-block" id={evidenceId ? evidenceAnchorId(evidenceId) : undefined} data-evidence-id={evidenceId}>
    <div className="document-tool-heading"><h6>실행 SQL 원문</h6>{sql && <button type="button" onClick={() => void copy()}>{copyStatus === 'copied' ? '복사됨' : copyStatus === 'failed' ? '복사 실패' : '복사'}</button>}</div>
    {sql ? <pre className="document-tool-sql"><code>{sql}</code></pre> : <p className="document-tool-missing">{NOT_PROVIDED}</p>}
  </section>
}

function TableMapping({ value }: { value: JsonValue | undefined }) {
  const rows = Array.isArray(value) ? value.filter(isObject) : []
  return <section className="document-tool-block">
    <h6>시트 → 테이블 매핑</h6>
    {rows.length === 0 ? <p className="document-tool-missing">{NOT_PROVIDED}</p> : <div className="document-tool-table-wrap"><table className="document-tool-table">
      <thead><tr><th>파일명</th><th>시트명</th><th>테이블명</th></tr></thead>
      <tbody>{rows.map((row, index) => <tr key={`${stringValue(row.logical_name) ?? index}`}><td>{displayValue(row.file_name)}</td><td>{displayValue(row.sheet_name)}</td><td><code>{displayValue(row.logical_name)}</code></td></tr>)}</tbody>
    </table></div>}
  </section>
}

function SqlResult({ output }: { output: JsonValue | undefined }) {
  const object = isObject(output) ? output : undefined
  const columns = Array.isArray(object?.columns) ? object.columns.filter((item): item is string => typeof item === 'string') : []
  const rows = Array.isArray(object?.rows) ? object.rows.filter(Array.isArray).slice(0, 10) : []
  const aggregate = isObject(object?.aggregate_values) ? object.aggregate_values : undefined
  const rowEvidenceIds = Array.isArray(object?.row_evidence_ids)
    ? object.row_evidence_ids.map(item => stringValue(item))
    : []
  return <>
    <section className="document-tool-block">
      <h6>집계 요약</h6>
      {aggregate === undefined ? <p className="document-tool-missing">{NOT_PROVIDED}</p> : <dl className="document-tool-summary">
        <div><dt>total_value</dt><dd>{displayValue(aggregate.total_value)}</dd></div>
        <div><dt>applied_rows</dt><dd>{displayValue(aggregate.applied_rows)}</dd></div>
      </dl>}
    </section>
    <section className="document-tool-block">
      <h6>결과 행 표본</h6>
      {columns.length === 0 || rows.length === 0 ? <p className="document-tool-missing">{NOT_PROVIDED}</p> : <div className="document-tool-table-wrap"><table className="document-tool-table">
        <thead><tr>{columns.map(column => <th key={column}>{column}</th>)}</tr></thead>
        <tbody>{rows.map((row, rowIndex) => {
          const evidenceId = rowEvidenceIds[rowIndex]
          return <tr key={rowIndex} id={evidenceId ? evidenceAnchorId(evidenceId) : undefined} data-evidence-id={evidenceId}>{columns.map((column, columnIndex) => <td key={`${column}-${columnIndex}`}>{displayValue(row[columnIndex])}</td>)}</tr>
        })}</tbody>
      </table></div>}
      {Array.isArray(object?.rows) && object.rows.length > 10 && <p className="document-tool-caption">상위 10행을 표시합니다.</p>}
    </section>
  </>
}

function DocumentSqlDetail({ call, execution }: { call: InspectionCall; execution?: LaneExecution }) {
  return <div className="document-tool-detail document-tool-sql-detail">
    <LaneStatus execution={execution} fallbackState={call.state} />
    <CopyableSql sql={stringValue(call.request_parameters.executed_sql)} evidenceId={stringValue(call.request_parameters.sql_evidence_id)} />
    <TableMapping value={call.request_parameters.table_mapping} />
    <SqlResult output={call.output} />
  </div>
}

function ChunkList({ output }: { output: JsonValue | undefined }) {
  const object = isObject(output) ? output : undefined
  const chunks = Array.isArray(object?.chunks) ? object.chunks.filter(isObject) : []
  if (chunks.length === 0) return <p className="document-tool-missing">{NOT_PROVIDED}</p>
  return <ol className="document-chunk-list">{chunks.map((chunk, index) => {
    const excerpt = stringValue(chunk.content_excerpt)
    const suppliedText = excerpt?.slice(0, 2400)
    const preview = suppliedText?.slice(0, 300)
    const hasFullText = suppliedText !== undefined && suppliedText.length > 300
    const selected = chunk.selected === true
    const evidenceId = jsonEvidenceId(chunk)
    return <li key={stringValue(chunk.record_id) ?? index} data-selected={selected ? 'true' : 'false'} id={evidenceId ? evidenceAnchorId(evidenceId) : undefined} data-evidence-id={evidenceId}>
      <div className="document-chunk-heading"><strong>{stringValue(chunk.document_name) ?? NOT_PROVIDED}</strong><span data-selected={selected ? 'true' : 'false'}>{selected ? '답변 사용' : '답변 미사용'}</span></div>
      <dl className="document-chunk-meta">
        <div><dt>페이지</dt><dd>{numberValue(chunk.page) ?? NOT_PROVIDED}</dd></div>
        {stringValue(chunk.section) && <div><dt>절</dt><dd>{stringValue(chunk.section)}</dd></div>}
        <div><dt>유사도 점수</dt><dd>{chunkScoreLabel(chunk)}</dd></div>
      </dl>
      <details className="document-chunk-excerpt" open>
        <summary>본문 발췌</summary>
        <p>{preview ?? NOT_PROVIDED}</p>
        {hasFullText
          ? <details className="document-chunk-full"><summary>전체 보기</summary><p>{suppliedText}</p></details>
          : <p className="document-chunk-full-missing"><strong>전체 내용</strong>{NOT_PROVIDED}</p>}
      </details>
    </li>
  })}</ol>
}

function DocumentRagDetail({ call, execution }: { call: InspectionCall; execution?: LaneExecution }) {
  return <div className="document-tool-detail document-tool-rag-detail">
    <LaneStatus execution={execution} fallbackState={call.state} />
    <section className="document-tool-block"><h6>검색 질의문</h6><p className="document-tool-query">{call.request_parameters.query || NOT_PROVIDED}</p></section>
    <section className="document-tool-block"><h6>검색 청크</h6><ChunkList output={call.output} /></section>
  </div>
}

export default function DocumentToolDetail({ call, execution }: { call: InspectionCall; execution?: LaneExecution }) {
  if (call.tool === 'document_sql') return <DocumentSqlDetail call={call} execution={execution} />
  if (call.tool === 'document_rag') return <DocumentRagDetail call={call} execution={execution} />
  return null
}
