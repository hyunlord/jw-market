import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import {
  isSourceSelectable,
  brandObservedSourcePeriods,
  sourceAvailabilityTitle,
  mergeMarketBrandResults,
  resolveSourceAvailability,
  shouldApplySupportedSourcesFromMeta,
} from '../src/utils/sourceAvailability.ts'

test('exact brand refresh replaces the stale default source contract', () => {
  const merged = mergeMarketBrandResults(
    [{ brand: '헴리브라', general_sources: ['UBIST', 'IQVIA'] }],
    [{ brand: '헴리브라', general_sources: ['IQVIA'] }],
  )

  assert.deepEqual(merged, [{ brand: '헴리브라', general_sources: ['IQVIA'] }])
})

test('does not let cause metadata replace populated assay navigation sources', () => {
  assert.equal(
    shouldApplySupportedSourcesFromMeta(new Set(['UBIST', 'IQVIA']), null, null),
    false,
  )
})

test('allows cause metadata fallback when every source contract is absent or empty', () => {
  assert.equal(shouldApplySupportedSourcesFromMeta(null, null, null), true)
})

test('does not use cause metadata when cache or legacy navigation already owns sources', () => {
  assert.equal(
    shouldApplySupportedSourcesFromMeta(null, new Set(['UBIST']), null),
    false,
  )
  assert.equal(
    shouldApplySupportedSourcesFromMeta(null, null, new Set(['IQVIA'])),
    false,
  )
})

test('catalog keeps stale Hemlibra UBIST selectable while unavailable sources stay disabled', () => {
  const availability = resolveSourceAvailability(
    new Set(['IQVIA']),
    ['UBIST.sales', 'IQVIA.sales'],
  )

  assert.deepEqual(availability, { UBIST: 'stale', IQVIA: 'available' })
  assert.equal(isSourceSelectable(availability.UBIST), true)
  assert.equal(isSourceSelectable(availability.IQVIA), true)
  assert.equal(sourceAvailabilityTitle('UBIST', 'stale', '2025-01'), '2025-01 이후 브랜드 데이터 없음')
})

test('exact brand refresh exposes the last observed period for stale source messaging', () => {
  const values = new Map<string, string>()
  Object.defineProperty(globalThis, 'sessionStorage', {
    configurable: true,
    value: {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => values.set(key, value),
    },
  })
  sessionStorage.setItem('marketBrandsResult', JSON.stringify([
    { brand: '헴리브라', observed_source_periods: { ubist: '2025-01' } },
  ]))

  assert.deepEqual(brandObservedSourcePeriods('헴리브라'), { ubist: '2025-01' })
})

test('analysis-level chart consumes explicit others and never renames the fifth company', () => {
  const chart = readFileSync(new URL('../src/components/main/AnalysisLevelChart.tsx', import.meta.url), 'utf8')

  assert.match(chart, /\.is_others === true/)
  assert.doesNotMatch(chart, /sorted\.slice\(5\)/)
})

test('unsupported source stays disabled when neither catalog nor response has data', () => {
  const availability = resolveSourceAvailability(new Set(['IQVIA']), [])

  assert.deepEqual(availability, { UBIST: 'unavailable', IQVIA: 'available' })
})

test('deep issue tooltip fits inside the 352px clipped panel', () => {
  const css = readFileSync(new URL('../src/styles/common.css', import.meta.url), 'utf8')

  assert.match(
    css,
    /\.deep-tooltip-icon \.chart-tooltip\s*\{[^}]*width:\s*min\(304px,\s*calc\(100vw - 64px\)\)/s,
  )
})

test('general and deep views share the catalog availability resolver', () => {
  const analyzePage = readFileSync(new URL('../src/pages/AnalyzePage.tsx', import.meta.url), 'utf8')
  const deepPage = readFileSync(new URL('../src/pages/DeepAnalyzePage.tsx', import.meta.url), 'utf8')

  assert.match(analyzePage, /resolveSourceAvailability\(\s*supportedSources,/)
  assert.match(deepPage, /resolveSourceAvailability\(catalogSources, combos\)/)
  assert.match(deepPage, /analysisData\.market_meta\.source_latest_period/)
  assert.match(deepPage, /observedSourcePeriods\[s\.toLowerCase\(\)\]/)
  assert.match(analyzePage, /refreshMarketBrands\(productName\)/)
  assert.match(deepPage, /refreshMarketBrands\(productName\)/)
  assert.match(deepPage, /if \(!brandsRefreshed\) return/)
  assert.match(deepPage, /if \(activeSources && !activeSources\.has\(sourceToggle\)\) return/)
})
