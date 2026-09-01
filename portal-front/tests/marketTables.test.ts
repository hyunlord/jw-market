import assert from 'node:assert/strict'
import test, { after } from 'node:test'
import { createElement, type ComponentType } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import react from '@vitejs/plugin-react'
import { createServer } from 'vite'

import { parseMarketTables, sortMarketTableRows, type MarketTable } from '../src/utils/marketTables.ts'

interface MarketTablesProps {
  tables: MarketTable[]
  error?: string
  selectionPolicy?: { readonly rule?: string; readonly ranked?: boolean }
}

const exactTable = {
  table_id: 'v4-2741d71603c9ca78',
  title: '단일 임상시험 상세',
  source_label: 'ClinicalTrials.gov',
  columns: [
    { key: 'trial', label: '시험', type: 'string', unit: null, align: 'left' },
    { key: 'enrollment', label: '대상자 수', type: 'number', unit: '명', align: 'right' },
  ],
  rows: [
    { cells: { trial: 'NCT05151731', enrollment: 120 }, record_id: 'ct:NCT05151731' },
    { cells: { trial: 'NCT07523971', enrollment: 64 }, record_id: 'ct:NCT07523971' },
  ],
  row_count: 2,
  omitted_columns: ['상세 설명'],
}

const vite = await createServer({
  configFile: false,
  root: new URL('..', import.meta.url).pathname,
  plugins: [react()],
  optimizeDeps: { noDiscovery: true },
  server: { middlewareMode: true, hmr: { port: 24683 } },
  appType: 'custom',
})
const { default: MarketTables } = await vite.ssrLoadModule(
  '/src/components/main/MarketTables.tsx',
) as { default: ComponentType<MarketTablesProps> }

after(async () => vite.close())

test('accepts the exact backend table contract without changing cells', () => {
  const parsed = parseMarketTables([exactTable])

  assert.deepEqual(parsed, [exactTable] satisfies MarketTable[])
  assert.equal(parsed?.[0]?.rows[1]?.cells.enrollment, 64)
})

test('rejects row count mismatch and ragged rows instead of hiding data', () => {
  assert.equal(parseMarketTables([{ ...exactTable, row_count: 3 }]), undefined)
  assert.equal(parseMarketTables([{
    ...exactTable,
    rows: [{ cells: { trial: 'NCT05151731' }, record_id: 'ct:NCT05151731' }],
    row_count: 1,
  }]), undefined)
  assert.equal(parseMarketTables([{
    ...exactTable,
    rows: [{ cells: { ...exactTable.rows[0].cells, unexpected: 'lost' }, record_id: 'ct:NCT05151731' }],
    row_count: 1,
  }]), undefined)
})

test('renders every backend row and identifies omitted source columns', () => {
  const tables = parseMarketTables([exactTable])
  assert.ok(tables)

  const markup = renderToStaticMarkup(createElement(MarketTables, { tables }))

  assert.match(markup, /시장 분석 표/)
  assert.match(markup, /단일 임상시험 상세/)
  assert.match(markup, /ClinicalTrials\.gov/)
  assert.match(markup, /NCT05151731/)
  assert.match(markup, /NCT07523971/)
  assert.match(markup, /120명/)
  assert.match(markup, /64명/)
  assert.match(markup, /원천 미제공: 상세 설명/)
  assert.doesNotMatch(markup, /pagination|다음 페이지|더보기/)
  assert.equal((markup.match(/<tr/g) ?? []).length, 3)
})

test('an absent table contract renders no invented empty state', () => {
  const markup = renderToStaticMarkup(createElement(MarketTables, { tables: [] }))
  assert.equal(markup, '')
})

test('sorts rows without dropping records or changing backend cells', () => {
  const rows = sortMarketTableRows(exactTable.rows, 'enrollment', 'ascending')
  const descending = sortMarketTableRows(exactTable.rows, 'enrollment', 'descending')

  assert.deepEqual(rows.map(row => row.record_id), ['ct:NCT07523971', 'ct:NCT05151731'])
  assert.deepEqual(descending.map(row => row.record_id), ['ct:NCT05151731', 'ct:NCT07523971'])
  assert.equal(rows.length, exactTable.row_count)
  assert.deepEqual(exactTable.rows[0]?.cells, { trial: 'NCT05151731', enrollment: 120 })
})

test('a malformed table contract renders an explicit error instead of disappearing', () => {
  const markup = renderToStaticMarkup(createElement(MarketTables, {
    tables: [],
    error: '표 데이터를 표시할 수 없습니다.',
  }))

  assert.match(markup, /role="alert"/)
  assert.match(markup, /표 데이터를 표시할 수 없습니다\./)
})

test('hides a zero-row table including its header', () => {
  const empty = { ...exactTable, rows: [], row_count: 0 }
  const tables = parseMarketTables([empty])
  assert.ok(tables)

  const markup = renderToStaticMarkup(createElement(MarketTables, { tables }))
  assert.equal(markup, '')
})

test('keeps every row in the DOM while showing only fifteen initially', () => {
  const rows = Array.from({ length: 20 }, (_, index) => ({
    cells: { trial: `NCT${String(index).padStart(8, '0')}`, enrollment: index },
    record_id: `ct:${index}`,
  }))
  const table = { ...exactTable, rows, row_count: rows.length }
  const tables = parseMarketTables([table])
  assert.ok(tables)

  const markup = renderToStaticMarkup(createElement(MarketTables, { tables }))
  assert.match(markup, /전체 20건 중 15건 표시 · 나머지는 조회 상세/)
  assert.match(markup, /나머지 5건 펼치기/)
  assert.match(markup, /<tbody class="market-structured-table-extra" hidden="">/)
  assert.equal((markup.match(/data-record-id=/g) ?? []).length, 20)
})

test('discloses a missing selection flag without guessing a ranking rule', () => {
  const rows = Array.from({ length: 40 }, (_, index) => ({
    cells: { trial: `시험 ${index}`, enrollment: index },
    record_id: `row:${index}`,
  }))
  const tables = parseMarketTables([{ ...exactTable, rows, row_count: rows.length }])
  assert.ok(tables)
  const markup = renderToStaticMarkup(createElement(MarketTables, { tables }))

  assert.match(markup, /정렬 플래그가 제공되지 않아/)
})

test('uses backend selection flags instead of assuming every 40-row table is unranked', () => {
  const rows = Array.from({ length: 40 }, (_, index) => ({
    cells: { trial: `NCT${index}`, enrollment: index }, record_id: `ct:${index}`,
  }))
  const tables = parseMarketTables([{ ...exactTable, rows, row_count: rows.length }])!

  const unranked = renderToStaticMarkup(createElement(MarketTables, {
    tables,
    selectionPolicy: { rule: 'leading_records_in_upstream_order', ranked: false },
  }))
  const ranked = renderToStaticMarkup(createElement(MarketTables, {
    tables,
    selectionPolicy: { rule: 'score_desc', ranked: true },
  }))
  const missing = renderToStaticMarkup(createElement(MarketTables, { tables }))

  assert.match(unranked, /임의 40건/)
  assert.match(ranked, /score_desc/)
  assert.match(missing, /정렬 플래그가 제공되지 않아/)
})
