import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { stripTypeScriptTypes } from 'node:module'
import test from 'node:test'

const analyzeSource = readFileSync(
  new URL('../src/pages/AnalyzePage.tsx', import.meta.url),
  'utf8',
)

type KpiPayloadState = {
  readonly loading: boolean
  readonly noData: boolean
}

function resolveState(kpi: object | null | undefined, isCauseLoading: boolean): KpiPayloadState {
  const match = analyzeSource.match(
    /const resolveKpiPayloadState = ([\s\S]*?)\n\nconst ASSAY_OPTIONS/,
  )
  assert.ok(match?.[1], 'AnalyzePage must expose the KPI payload-state contract')
  const source = stripTypeScriptTypes(`const resolveKpiPayloadState = ${match[1]}`)
  const create = new Function(`${source}; return resolveKpiPayloadState;`)
  return create()(kpi, isCauseLoading) as KpiPayloadState
}

test('treats a successful empty KPI object as no-data instead of loading', () => {
  // Given: the API completed successfully with no KPI fields
  const kpi = {}

  // When: the card display state is resolved
  const state = resolveState(kpi, false)

  // Then: rendering uses the local no-data branch and not a skeleton
  assert.deepEqual(state, { loading: false, noData: true })
})

test('keeps a complete KPI payload on the normal value-rendering path', () => {
  // Given: a representative complete KPI payload
  const kpi = {
    market_size_recent: 100_000_000,
    target_brand_sales: 20_000_000,
    target_share_pct: 20,
    direct_competition_count: 4,
  }

  // When: the card display state is resolved
  const state = resolveState(kpi, false)

  // Then: the existing value-rendering path remains active
  assert.deepEqual(state, { loading: false, noData: false })
})

test('keeps an absent KPI payload in loading only while the request is unresolved', () => {
  const state = resolveState(undefined, true)

  // Then: existing skeleton behavior is preserved
  assert.deepEqual(state, { loading: true, noData: false })
})

test('renders completed null data as no-data instead of retaining the skeleton', () => {
  const state = resolveState(undefined, false)

  assert.deepEqual(state, { loading: false, noData: true })
})

test('guards every KPI card from an empty payload without adding page-level error UI', () => {
  // Given: the KPI card source block
  const match = analyzeSource.match(/\{\/\* KPI 카드 \*\/\}([\s\S]*?)\{\/\* ===== 차트 1:/)
  assert.ok(match?.[1], 'KPI card block must be present')
  const cards = match[1]

  // When/Then: empty payloads use the existing dash convention and no non-null assertion remains
  assert.doesNotMatch(cards, /kpi!\./)
  assert.match(cards, /noData \? '—'/)
})
