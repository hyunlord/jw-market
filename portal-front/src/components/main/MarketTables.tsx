import { useMemo, useState } from 'react'

import { sortMarketTableRows, type MarketTable, type MarketTableCell, type MarketTableColumn } from '../../utils/marketTables'
import { displaySourceLabel } from '../../utils/portalDisplayLabels'
import { selectionNotice, type SelectionPolicy } from '../../utils/traceToolResults'
import { evidenceAnchorId } from '../../utils/evidenceAnchors'

interface Props {
  tables: readonly MarketTable[]
  error?: string
  selectionPolicy?: SelectionPolicy
}

type SortState = {
  readonly key: string
  readonly direction: 'ascending' | 'descending'
} | undefined

function displayCell(value: MarketTableCell, column: MarketTableColumn): string {
  if (value === null) return '-'
  if (typeof value === 'boolean') return value ? '예' : '아니오'
  return `${value}${column.unit ?? ''}`
}

function StructuredTable({ table, selectionPolicy }: { table: MarketTable; selectionPolicy?: SelectionPolicy }) {
  const [sort, setSort] = useState<SortState>()
  const [showAll, setShowAll] = useState(false)
  const rows = useMemo(() => {
    if (!sort) return table.rows
    return sortMarketTableRows(table.rows, sort.key, sort.direction)
  }, [sort, table.rows])
  const visibleRows = rows.slice(0, 15)
  const extraRows = rows.slice(15)
  const columns = table.columns.filter(column => {
    if (column.key !== 'summary' && column.label !== '요약') return true
    return !rows.every(row => row.cells[column.key] === row.record_id)
  })
  const limitNotice = selectionNotice(selectionPolicy, rows.length)

  const changeSort = (key: string) => {
    setSort(current => current?.key === key
      ? { key, direction: current.direction === 'ascending' ? 'descending' : 'ascending' }
      : { key, direction: 'ascending' })
  }

  return (
    <section className="market-structured-table" aria-labelledby={`${table.table_id}-title`}>
      <header>
        <div>
          <h3 id={`${table.table_id}-title`}>{table.title}</h3>
          <p>{displaySourceLabel(table.source_label)}</p>
        </div>
        <span>{table.row_count}건</span>
      </header>
      <div className="market-structured-table-scroll">
        <table>
          <thead>
            <tr>
              {columns.map(column => (
                <th key={column.key} scope="col" aria-sort={sort?.key === column.key ? sort.direction : 'none'}>
                  <button type="button" onClick={() => changeSort(column.key)}>
                    <span>{column.label}</span>
                    {column.unit && <small>({column.unit})</small>}
                  </button>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {visibleRows.map(row => (
              <tr key={row.record_id} id={evidenceAnchorId(row.record_id)} data-record-id={row.record_id} data-evidence-id={row.record_id}>
                {columns.map(column => {
                  const value = row.cells[column.key] ?? null
                  const trend = column.type === 'change' && typeof value === 'number'
                    ? value > 0 ? ' positive' : value < 0 ? ' negative' : ''
                    : ''
                  return (
                    <td className={`align-${column.align}${trend}`} key={column.key}>
                      {displayCell(value, column)}
                    </td>
                  )
                })}
              </tr>
            ))}
          </tbody>
          {extraRows.length > 0 && (
            <tbody className="market-structured-table-extra" hidden={!showAll}>
              {extraRows.map(row => (
                <tr key={row.record_id} id={evidenceAnchorId(row.record_id)} data-record-id={row.record_id} data-evidence-id={row.record_id}>
                  {columns.map(column => {
                    const value = row.cells[column.key] ?? null
                    const trend = column.type === 'change' && typeof value === 'number'
                      ? value > 0 ? ' positive' : value < 0 ? ' negative' : ''
                      : ''
                    return <td className={`align-${column.align}${trend}`} key={column.key}>{displayCell(value, column)}</td>
                  })}
                </tr>
              ))}
            </tbody>
          )}
        </table>
      </div>
      {extraRows.length > 0 && (
        <div className="market-structured-table-limit" role="status">
          <span>전체 {rows.length}건 중 15건 표시 · 나머지는 조회 상세</span>
          <button type="button" aria-expanded={showAll} onClick={() => setShowAll(current => !current)}>
            {showAll ? '추가 행 접기' : `나머지 ${extraRows.length}건 펼치기`}
          </button>
        </div>
      )}
      {limitNotice && <p className="market-structured-table-selection">{limitNotice}</p>}
      {table.omitted_columns.length > 0 && (
        <p className="market-structured-table-omitted">
          원천 미제공: {table.omitted_columns.join(', ')}
        </p>
      )}
    </section>
  )
}

export default function MarketTables({ tables, error, selectionPolicy }: Props) {
  if (error) return <p className="market-structured-table-error" role="alert">{error}</p>
  const populatedTables = tables.filter(table => table.row_count > 0)
  if (populatedTables.length === 0) return null
  return (
    <div className="market-structured-tables" aria-label="시장 분석 표">
      {populatedTables.map(table => <StructuredTable table={table} selectionPolicy={selectionPolicy} key={table.table_id} />)}
    </div>
  )
}
