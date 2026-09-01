import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { formatMarketGrowthPct } from '../src/utils/marketGrowthDisplay.ts'

const analyzeSource = readFileSync(
  new URL('../src/pages/AnalyzePage.tsx', import.meta.url),
  'utf8',
)

test('CMGR and CQGR tooltip values use two decimal places', () => {
  assert.equal(formatMarketGrowthPct(0.841), '0.84%')
  assert.equal(formatMarketGrowthPct(-1.234), '-1.23%')
  assert.equal(formatMarketGrowthPct(0), '0.00%')
  assert.equal(formatMarketGrowthPct(12.341), '12.34%')
})

test('market growth geometry keeps the unrounded API series', () => {
  assert.match(analyzeSource, /data:\s*yoyData/)
  assert.doesNotMatch(analyzeSource, /data:\s*yoyData\.map\([^)]*toFixed/)
})
