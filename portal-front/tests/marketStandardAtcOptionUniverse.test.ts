import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const filterBarSource = readFileSync(
  new URL('../src/components/main/AnalyzeFilterBar.tsx', import.meta.url),
  'utf8',
)
const dynamicMarketSource = readFileSync(
  new URL('../src/utils/dynamicMarket.ts', import.meta.url),
  'utf8',
)

const optionUniverseRequest = filterBarSource.match(
  /const \w+ = await fetchFilterOptions\(\{([\s\S]*?)\n\s*\}\)/,
)?.[1] ?? ''

test('filter options use the debounced draft ATC4 scope', () => {
  assert.ok(optionUniverseRequest, 'fetchFilterOptions request was not found')
  assert.match(
    optionUniverseRequest,
    /atc4Codes:\s*debouncedAtcKey\s*\?\s*debouncedAtcKey\.split\(','\)\s*:\s*\[\]/,
    'filter-option requests must be narrowed by the debounced draft ATC4 scope',
  )
  assert.match(
    filterBarSource,
    /filterOptionsRetry,\s*debouncedAtcKey,\s*bootstrapCtx,\s*fullAtcTree,?\s*\]/,
  )
})

test('scoped dimension responses preserve the full ATC navigation tree', () => {
  assert.match(filterBarSource, /fetchFullAtcTree\(\{/)
  assert.match(filterBarSource, /mergeFilterOptions\(filterOpts, fallbackCodes, fullAtcTree\)/)
  assert.doesNotMatch(filterBarSource, /setFullAtcTree\([^)]*merged\.atcTree/)
  assert.match(filterBarSource, /const def = filterOpts\?\.default_selections/)
  assert.match(filterBarSource, /resolveOptionLoadAtcSelection\(\{[\s\S]*?defaults: nextDefaultAtc/)
  assert.match(filterBarSource, /setDraftAtc\(nextAtc\)/)
})

test('changing product or source clears the previously preserved ATC tree', () => {
  assert.match(
    filterBarSource,
    /if \(ctxKey !== lastSelectionSync\.ctxKey\) \{[\s\S]*?setFullAtcTree\(\{\}\)/,
  )
})

test('the full ATC navigation request is independent of the draft ATC4 scope', () => {
  const fullTreeRequest = dynamicMarketSource.match(
    /export function fetchFullAtcTree[\s\S]*?export async function fetchBrandDefaultScope/,
  )?.[0] ?? ''
  assert.match(fullTreeRequest, /fetchFilterOptions\(\{/)
  assert.match(fullTreeRequest, /brand:\s*params\.brand/)
  assert.match(fullTreeRequest, /measure:\s*params\.measure/)
  assert.match(fullTreeRequest, /atc4Codes:\s*\[\]/)
  assert.doesNotMatch(fullTreeRequest, /\/api\/v1\/market\/atc\/options/)
  assert.doesNotMatch(
    fullTreeRequest,
    /atc4_codes/,
  )
})

test('an empty draft ATC4 keeps the existing unscoped option request', () => {
  assert.match(
    optionUniverseRequest,
    /atc4Codes:\s*debouncedAtcKey\s*\?[^\n]+:\s*\[\]/,
  )
  assert.match(
    dynamicMarketSource,
    /\.\.\.\(atc4\.length \? \{ atc4_codes: atc4 \} : \{\}\)/,
  )
})

test('search scope sends explicit ATC4 only when selected', () => {
  assert.match(
    filterBarSource,
    /draftAtc\.atc4\.length > 0\s*\?\s*draftAtc\.atc4/,
  )
  assert.match(
    dynamicMarketSource,
    /\.\.\.\(atc4\.length \? \{ atc4_codes: atc4 \} : \{\}\)/,
  )
})

test('the filter-option cache key isolates each ATC4 scope', () => {
  assert.match(dynamicMarketSource, /atc4\.join\(','\)/)
})

test('changing ATC4 clears stale dimension selections before search', () => {
  assert.match(
    filterBarSource,
    /const atcScopeChanged = !isSameAtc4Scope\(applied\.atc4, atc4\)/,
  )
  assert.match(
    filterBarSource,
    /const searchFilters = atcScopeChanged \? emptyAnalysisLevel\(source\) : draftFilters/,
  )
  assert.match(filterBarSource, /setDraftFilters\(cloneFilters\(searchFilters\)\)/)
  assert.match(filterBarSource, /setCommittedFilters\(cloneFilters\(searchFilters\)\)/)
  assert.match(
    filterBarSource,
    /onApply\(\{ assayMode, atc4, analysisLevel: level \}, draftAtc\)/,
  )
})
