import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const filterBar = readFileSync(
  new URL('../src/components/main/AnalyzeFilterBar.tsx', import.meta.url),
  'utf8',
)
const dynamicMarket = readFileSync(
  new URL('../src/utils/dynamicMarket.ts', import.meta.url),
  'utf8',
)

test('brand entry bootstraps its ATC4 defaults before loading options', () => {
  assert.match(dynamicMarket, /export async function fetchBrandDefaultScope/)
  assert.match(dynamicMarket, /\/api\/v1\/market\/dynamic\/brand\/default-scope/)
  assert.match(filterBar, /fetchBrandDefaultScope\(\{/)
  assert.match(filterBar, /fetchFullAtcTree\(\{/)
  assert.match(filterBar, /expandAtcCodesToLeaves\(bootstrapCodes, navigationTree\)/)
  assert.match(filterBar, /atcLevelsFromCanonicalAtc4\(bootstrapLeaves\)/)
})

test('draft ATC4 changes trigger debounced scoped option requests', () => {
  assert.match(filterBar, /const draftAtcKey = draftAtc\.atc4\.join\(','\)/)
  assert.match(filterBar, /setTimeout\([\s\S]*?250/)
  assert.match(filterBar, /atc4Codes:\s*debouncedAtcKey\s*\?\s*debouncedAtcKey\.split\(','\)/)
  assert.match(filterBar, /filterOptionsRetry,\s*debouncedAtcKey/)
})

test('the immediate request keeps every selected ATC4 code', () => {
  assert.match(filterBar, /debouncedAtcKey\.split\(','\)/)
  assert.doesNotMatch(filterBar, /debouncedAtcKey\.split\(','\)\[0\]/)
})

test('scoped reload reconciles stale dimension selections', () => {
  assert.match(filterBar, /setCommittedFilters\(prev => reconcileAnalysisLevel/)
  assert.match(filterBar, /setDraftFilters\(prev => reconcileAnalysisLevel/)
})
