import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'

const filterBarSource = readFileSync(
  new URL('../src/components/main/AnalyzeFilterBar.tsx', import.meta.url),
  'utf8',
)
const causeStoreSource = readFileSync(
  new URL('../src/utils/causeStore.ts', import.meta.url),
  'utf8',
)
const analyzePageSource = readFileSync(
  new URL('../src/pages/AnalyzePage.tsx', import.meta.url),
  'utf8',
)
const dynamicMarketSource = readFileSync(
  new URL('../src/utils/dynamicMarket.ts', import.meta.url),
  'utf8',
)

test('Market Standard leaves an empty ATC4 selection for the focus-brand fallback', () => {
  assert.match(
    filterBarSource,
    /const atc4 = assayMode === 'jw'[\s\S]*?: draftAtc\.atc4/,
  )
  assert.doesNotMatch(
    filterBarSource,
    /: \(atcTree\.atc4\?\.map\(i => i\.key\) \?\? fallbackAtc4\)/,
  )
  assert.match(
    filterBarSource,
    /data-market-standard-atc-leaf-fallback="focus-brand"/,
  )
})

test('the existing request builder uses focus_brand_key when Market Standard ATC4 is empty', () => {
  assert.match(
    dynamicMarketSource,
    /body\.filters = filters\.atc4\.length > 0[\s\S]*?: \{ focus_brand_key: brand, \.\.\.analysisLevel \}/,
  )
})

test('a selected leaf remains explicit and does not use the fallback', () => {
  assert.match(
    dynamicMarketSource,
    /\? \{ atc4: filters\.atc4, \.\.\.analysisLevel \}/,
  )
})

test('cause request failures are propagated instead of converted to null', () => {
  assert.doesNotMatch(causeStoreSource, /\.catch\(\(\) => \{[\s\S]*?return null[\s\S]*?\}\)/)
  assert.match(causeStoreSource, /\.finally\(\(\) => inflight\.delete\(reqKey\)\)/)
})

test('AnalyzePage reports both rejected and unsuccessful cause requests', () => {
  assert.match(analyzePageSource, /const DYNAMIC_MARKET_ERROR_MESSAGE =/)
  assert.match(analyzePageSource, /if \(!result\) \{[\s\S]*?setAlertMessage/)
  assert.match(analyzePageSource, /\.catch\(\(\) => \{[\s\S]*?setAlertMessage/)
})
