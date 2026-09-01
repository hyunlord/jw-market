import assert from 'node:assert/strict'
import test from 'node:test'

import { formatAnalysisLevelTrendTooltip } from '../src/utils/analysisLevelTooltip.ts'

const valueLabel = 'Statin/EZE (매출 : 12,454백만원)'

test('UBIST monthly tooltip appends the backend M/S without normalization', () => {
  const label = formatAnalysisLevelTrendTooltip({
    valueLabel,
    source: 'UBIST',
    sourcePeriodUnit: '월',
    selectedPeriod: 'monthly',
    isOverall: false,
    sharePct: 30.3092,
  })

  assert.deepEqual(label, [valueLabel, '- M/S : 30.3%'])
})

test('IQVIA quarterly tooltip appends the backend M/S at the native grain', () => {
  const label = formatAnalysisLevelTrendTooltip({
    valueLabel,
    source: 'IQVIA',
    sourcePeriodUnit: '분기',
    selectedPeriod: 'quarterly',
    isOverall: false,
    sharePct: 55.5827,
  })

  assert.deepEqual(label, [valueLabel, '- M/S : 55.6%'])
})

test('overall tooltip keeps the existing value label without a synthetic share', () => {
  const label = formatAnalysisLevelTrendTooltip({
    valueLabel: '전체 (매출 : 21,486백만원)',
    source: 'UBIST',
    sourcePeriodUnit: '월',
    selectedPeriod: 'monthly',
    isOverall: true,
    sharePct: 100,
  })

  assert.equal(label, '전체 (매출 : 21,486백만원)')
})

test('UBIST aggregated quarterly and yearly tooltips omit the summed percentage', () => {
  for (const selectedPeriod of ['quarterly', 'yearly'] as const) {
    const label = formatAnalysisLevelTrendTooltip({
      valueLabel,
      source: 'UBIST',
      sourcePeriodUnit: '월',
      selectedPeriod,
      isOverall: false,
      sharePct: 320.36,
    })

    assert.equal(label, valueLabel)
  }
})

test('missing percentage omits the M/S row and preserves the value label', () => {
  const label = formatAnalysisLevelTrendTooltip({
    valueLabel,
    source: 'IQVIA',
    sourcePeriodUnit: '분기',
    selectedPeriod: 'quarterly',
    isOverall: false,
    sharePct: undefined,
  })

  assert.equal(label, valueLabel)
})
