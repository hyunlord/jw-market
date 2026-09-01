import assert from 'node:assert/strict'
import test from 'node:test'

import { buildActivitySeriesRequest, buildCsdMarketOptions, normalizeCsdMarketScope } from '../src/utils/brandActivityCsdMarket.ts'

test('preserves backend CSD market sheets in normalized activity scope', () => {
  const result = normalizeCsdMarketScope({
    csd_market: 'GUARDLET',
    csd_markets: ['GUARDLET', 'GANAKHAN'],
  })

  assert.deepEqual(result.csd_markets, ['GUARDLET', 'GANAKHAN'])
  assert.equal(result.csd_market, 'GUARDLET')
})

test('keeps ATC4 scope and sends selected CSD market independently', () => {
  const request = buildActivitySeriesRequest('가드메트', ['A10N1', 'A10N3'], { view: 'general' }, {
    csdMarket: 'GANAKHAN',
  })

  assert.deepEqual(request.filters.atc4, ['A10N1', 'A10N3'])
  assert.equal(request.csd_market, 'GANAKHAN')
})

test('omits CSD market when the all-market option is selected', () => {
  const request = buildActivitySeriesRequest('가드메트', ['A10N1'], { view: 'general' }, { csdMarket: 'all' })

  assert.equal('csd_market' in request, false)
})

test('builds selector options from backend CSD markets in source order', () => {
  assert.deepEqual(buildCsdMarketOptions(['GUARDLET', 'GANAKHAN']), [
    { value: 'all', label: '시장 전체' },
    { value: 'GUARDLET', label: 'GUARDLET' },
    { value: 'GANAKHAN', label: 'GANAKHAN' },
  ])
})
