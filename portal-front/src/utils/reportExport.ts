import type { MarketChart } from './marketChartContract.ts'
import type { MarketTable, MarketTableCell, MarketTableColumn } from './marketTables.ts'

export interface ReportArtifacts {
  readonly tables: readonly MarketTable[]
  readonly charts: readonly MarketChart[]
}

function escapeCell(value: string): string {
  return value.replace(/\|/g, '\\|').replace(/\r?\n/g, '<br>')
}

function displayCell(value: MarketTableCell, column: MarketTableColumn): string {
  if (value === null) return '-'
  if (typeof value === 'boolean') return value ? '예' : '아니오'
  return `${value}${column.unit ?? ''}`
}

function markdownTable(headers: readonly string[], rows: readonly (readonly string[])[]): string {
  const header = `| ${headers.map(escapeCell).join(' | ')} |`
  const divider = `| ${headers.map(() => '---').join(' | ')} |`
  return [header, divider, ...rows.map(row => `| ${row.map(escapeCell).join(' | ')} |`)].join('\n')
}

function tableMarkdown(table: MarketTable): string {
  const columns = table.columns.filter(column => (
    column.key !== 'summary' || !table.rows.every(row => row.cells[column.key] === row.record_id)
  ))
  const rows = table.rows.map(row => columns.map(column => displayCell(row.cells[column.key] ?? null, column)))
  const omitted = table.omitted_columns.length ? `\n\n원천 미제공: ${table.omitted_columns.join(', ')}` : ''
  return `### ${table.title}\n\n출처: ${escapeCell(table.source_label)} · 전체 ${table.rows.length}건\n\n${markdownTable(columns.map(column => column.label), rows)}${omitted}`
}

function chartMarkdown(chart: MarketChart): string {
  const rows = chart.x.map((label, index) => [
    label,
    ...chart.series.map(series => series.values[index] === null ? '-' : `${series.values[index]}${chart.unit ?? ''}`),
  ])
  return `### ${chart.title}\n\n출처: ${escapeCell(chart.source_label)}\n\n> 차트는 데이터 표로 대체했습니다.\n\n${markdownTable([chart.x_label || '항목', ...chart.series.map(series => series.label)], rows)}`
}

export function reportArtifactsToMarkdown(artifacts: ReportArtifacts): string {
  return [
    ...artifacts.tables.map(tableMarkdown),
    ...artifacts.charts.map(chartMarkdown),
  ].join('\n\n')
}
