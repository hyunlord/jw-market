import test from 'node:test'
import assert from 'node:assert/strict'
import { existsSync, readFileSync } from 'node:fs'

const selectorUrl = new URL('../src/utils/brandCagr.ts', import.meta.url)

test('selects the 5Y brand CAGR when both horizons are available', async () => {
  // Given: the frontend brand CAGR selector exists
  assert.equal(existsSync(selectorUrl), true, 'brand CAGR selector module should exist')
  if (!existsSync(selectorUrl)) return
  const module = await import('../src/utils/brandCagr.ts')

  // When: both exclusive backend keys are supplied
  const result = module.selectBrandCagr(3.9056, 23.3769)

  // Then: the 5Y value and label win
  assert.deepEqual(result, { label: 'CAGR (5Y)', value: 3.9056 })
})

test('keeps the Actemra 5Y brand CAGR on the 5Y label', async () => {
  const module = await import('../src/utils/brandCagr.ts')

  const result = module.selectBrandCagr(2.1052, null)

  assert.deepEqual(result, { label: 'CAGR (5Y)', value: 2.1052 })
})

test('falls back to the 3Y brand CAGR when 5Y is null', async () => {
  // Given: the frontend brand CAGR selector exists
  assert.equal(existsSync(selectorUrl), true, 'brand CAGR selector module should exist')
  if (!existsSync(selectorUrl)) return
  const module = await import('../src/utils/brandCagr.ts')

  // When: only the 3Y backend key has a value
  const result = module.selectBrandCagr(null, 23.3769)

  // Then: the value and horizon label both switch to 3Y
  assert.deepEqual(result, { label: 'CAGR (3Y)', value: 23.3769 })
})

test('renders an em dash contract when neither brand CAGR exists', async () => {
  // Given: the frontend brand CAGR selector exists
  assert.equal(existsSync(selectorUrl), true, 'brand CAGR selector module should exist')
  if (!existsSync(selectorUrl)) return
  const module = await import('../src/utils/brandCagr.ts')

  // When: both backend keys are null
  const result = module.selectBrandCagr(null, null)

  // Then: no zero or fabricated horizon is returned
  assert.deepEqual(result, { label: 'CAGR (산출 이력 부족)', value: null })
})

test('wires cause and market-status cards to brand CAGR without changing market tooltip CAGR', () => {
  // Given: the two card source files
  const analyzeSource = readFileSync(new URL('../src/pages/AnalyzePage.tsx', import.meta.url), 'utf8')
  const dashboardSource = readFileSync(new URL('../src/pages/DashboardPage.tsx', import.meta.url), 'utf8')

  // When/Then: cards use the shared brand selector
  assert.match(analyzeSource, /selectBrandCagr\(kpi\?\.brand_cagr_5y_pct, kpi\?\.brand_cagr_3y_pct\)/)
  assert.match(dashboardSource, /selectBrandCagr\(ext\?\.brand_cagr_5y_pct, ext\?\.brand_cagr_3y_pct\)/)

  // Then: the chart comparison still consumes the market CAGR key
  assert.match(analyzeSource, /const mkt = kpi\?\.market_cagr_5y_pct \?\? 0/)
})
