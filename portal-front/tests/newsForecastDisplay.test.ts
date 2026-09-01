import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import {
  buildForecastModelExplanation,
  hasReadableNewsBody,
} from '../src/utils/newsForecastDisplay.ts'

test('blank and whitespace-only news bodies cannot open an empty detail modal', () => {
  assert.equal(hasReadableNewsBody(null), false)
  assert.equal(hasReadableNewsBody(undefined), false)
  assert.equal(hasReadableNewsBody(''), false)
  assert.equal(hasReadableNewsBody('  \n\t '), false)
  assert.equal(hasReadableNewsBody('기사 본문'), true)
})

test('forecast explanation is built from the selected combo model payload', () => {
  const text = buildForecastModelExplanation({
    source: 'IQVIA',
    historyPeriodCount: 20,
    brands: [
      { brand: '리바로', forecast_model: { name: 'HoltWinters' } },
      { brand: '리바로젯', forecast_model: { name: 'Mean' } },
    ],
  })

  assert.match(text, /IQVIA/)
  assert.match(text, /이력 20개 구간/)
  assert.match(text, /리바로.*Holt-Winters.*추세와 계절성/s)
  assert.match(text, /리바로젯.*데이터가 부족해 관측값 평균을 사용합니다/s)
  assert.doesNotMatch(text, /data_size_dispatch_v1|MAPE/)
})

test('different combo payloads produce different model descriptions without a fixed model', () => {
  const holt = buildForecastModelExplanation({
    source: 'UBIST',
    historyPeriodCount: 60,
    brands: [{ brand: 'A', forecast_model: { name: 'HoltWinters' } }],
  })
  const prophet = buildForecastModelExplanation({
    source: 'IQVIA',
    historyPeriodCount: 20,
    brands: [{ brand: 'A', forecast_model: { name: 'Prophet' } }],
  })

  assert.match(holt, /Holt-Winters/)
  assert.doesNotMatch(holt, /Prophet/)
  assert.match(prophet, /Prophet/)
  assert.doesNotMatch(prophet, /Holt-Winters/)
})

test('the deep-analysis page guards both the detail action and modal body', () => {
  const page = readFileSync(new URL('../src/pages/DeepAnalyzePage.tsx', import.meta.url), 'utf8')

  assert.match(page, /hasReadableNewsBody\(issue\.body_full\)/)
  assert.match(page, /hasReadableNewsBody\(selectedIssue\?\.body_full\)/)
  assert.match(page, /buildForecastModelExplanation/)
})
