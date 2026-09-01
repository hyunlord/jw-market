export type MarketTableCell = null | boolean | number | string

export interface MarketTableColumn {
  readonly key: string
  readonly label: string
  readonly type: string
  readonly unit: string | null
  readonly align: 'left' | 'center' | 'right'
}

export interface MarketTableRow {
  readonly cells: Readonly<Record<string, MarketTableCell>>
  readonly record_id: string
}

export interface MarketTable {
  readonly table_id: string
  readonly title: string
  readonly source_label: string
  readonly columns: readonly MarketTableColumn[]
  readonly rows: readonly MarketTableRow[]
  readonly row_count: number
  readonly omitted_columns: readonly string[]
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function isCell(value: unknown): value is MarketTableCell {
  return value === null || ['boolean', 'number', 'string'].includes(typeof value)
}

function parseAlign(value: unknown): MarketTableColumn['align'] | undefined {
  if (value === 'left' || value === 'center' || value === 'right') return value
  return undefined
}

function parseColumn(value: unknown): MarketTableColumn | undefined {
  if (!isRecord(value)) return undefined
  const align = parseAlign(value.align)
  if (typeof value.key !== 'string'
    || value.key.length === 0
    || typeof value.label !== 'string'
    || typeof value.type !== 'string'
    || (value.unit !== null && typeof value.unit !== 'string')
    || align === undefined) return undefined

  return {
    key: value.key,
    label: value.label,
    type: value.type,
    unit: value.unit,
    align,
  }
}

function parseRow(value: unknown, columnKeys: readonly string[]): MarketTableRow | undefined {
  if (!isRecord(value) || !isRecord(value.cells) || typeof value.record_id !== 'string') return undefined
  const cellKeys = Object.keys(value.cells)
  if (cellKeys.length !== columnKeys.length) return undefined
  const cells: Record<string, MarketTableCell> = {}
  for (const key of columnKeys) {
    const cell = value.cells[key]
    if (!cellKeys.includes(key) || !isCell(cell)) return undefined
    cells[key] = cell
  }

  return {
    cells,
    record_id: value.record_id,
  }
}

function parseTable(value: unknown): MarketTable | undefined {
  if (!isRecord(value)
    || typeof value.table_id !== 'string'
    || value.table_id.length === 0
    || typeof value.title !== 'string'
    || typeof value.source_label !== 'string'
    || !Array.isArray(value.columns)
    || value.columns.length === 0
    || !Array.isArray(value.rows)
    || typeof value.row_count !== 'number'
    || !Number.isInteger(value.row_count)
    || value.row_count < 0
    || value.row_count !== value.rows.length
    || !Array.isArray(value.omitted_columns)
    || !value.omitted_columns.every(item => typeof item === 'string')) return undefined

  const parsedColumns: MarketTableColumn[] = []
  for (const column of value.columns) {
    const parsed = parseColumn(column)
    if (!parsed) return undefined
    parsedColumns.push(parsed)
  }
  const columnKeys = parsedColumns.map(column => column.key)
  if (new Set(columnKeys).size !== columnKeys.length) return undefined

  const parsedRows: MarketTableRow[] = []
  for (const row of value.rows) {
    const parsed = parseRow(row, columnKeys)
    if (!parsed) return undefined
    parsedRows.push(parsed)
  }
  return {
    table_id: value.table_id,
    title: value.title,
    source_label: value.source_label,
    columns: parsedColumns,
    rows: parsedRows,
    row_count: value.row_count,
    omitted_columns: value.omitted_columns,
  }
}

export function parseMarketTables(value: unknown): MarketTable[] | undefined {
  if (!Array.isArray(value)) return undefined
  const parsed: MarketTable[] = []
  for (const table of value) {
    const item = parseTable(table)
    if (!item) return undefined
    parsed.push(item)
  }
  if (new Set(parsed.map(table => table.table_id)).size !== parsed.length) return undefined
  return parsed
}

function compareCells(left: MarketTableCell, right: MarketTableCell): number {
  if (left === right) return 0
  if (left === null) return 1
  if (right === null) return -1
  if (typeof left === 'number' && typeof right === 'number') return left - right
  return String(left).localeCompare(String(right), 'ko')
}

export function sortMarketTableRows(
  rows: readonly MarketTableRow[],
  key: string,
  direction: 'ascending' | 'descending',
): MarketTableRow[] {
  return [...rows].sort((left, right) => {
    const order = compareCells(left.cells[key] ?? null, right.cells[key] ?? null)
    return direction === 'ascending' ? order : -order
  })
}
