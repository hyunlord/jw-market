import assert from 'node:assert/strict'
import test from 'node:test'

import {
  DynamicMarketRequestError,
  dynamicMarketErrorMessage,
  parseDynamicMarketError,
} from '../src/utils/dynamicMarketError.ts'

test('only the 3000-row scope error tells users to narrow ATC', () => {
  const error = parseDynamicMarketError(400, {
    detail: { error: 'dynamic_scope_too_broad', message: 'internal detail' },
  })

  assert.equal(error.code, 'dynamic_scope_too_broad')
  assert.match(dynamicMarketErrorMessage(error), /ATC 범위를 좁히거나/)
})

test('parent and leaf request errors keep a distinct safe action', () => {
  const error = parseDynamicMarketError(400, {
    detail: { error: 'invalid_dynamic_market_request', message: 'parent is not a leaf' },
  })

  assert.equal(error.code, 'invalid_dynamic_market_request')
  assert.match(dynamicMarketErrorMessage(error), /하위 ATC 항목/)
  assert.doesNotMatch(dynamicMarketErrorMessage(error), /parent|leaf|dynamic|scope/i)
  assert.doesNotMatch(dynamicMarketErrorMessage(error), /ATC 범위를 좁히거나/)
})

test('unknown server errors do not masquerade as the 3000-row limit', () => {
  const error = new DynamicMarketRequestError(500, 'server_error')

  assert.match(dynamicMarketErrorMessage(error), /잠시 후 다시 조회/)
  assert.doesNotMatch(dynamicMarketErrorMessage(error), /ATC 범위를 좁히거나/)
})

test('source-not-available preserves available contexts and explains the strategic mismatch', () => {
  const error = parseDynamicMarketError(422, {
    detail: {
      error: 'source_not_available',
      available_contexts: [
        { view_kind: 'strategic_ml', market_id: 'ml_008', source: 'iqvia' },
      ],
    },
  })

  assert.equal(error.code, 'source_not_available')
  assert.deepEqual(error.availableContexts, [
    { view_kind: 'strategic_ml', market_id: 'ml_008', source: 'iqvia' },
  ])
  assert.match(dynamicMarketErrorMessage(error), /선택한 전략뷰.*UBIST|사용 가능한 원천.*IQVIA/s)
})
