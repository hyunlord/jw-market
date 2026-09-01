import assert from 'node:assert/strict'
import test from 'node:test'

import {
  buildDeepAnalysisRequest,
  deepAnalysisRequestKey,
  formatDeepAnalysisError,
  parseDeepAnalysisError,
  resolveDeepAnalysisMarketId,
} from '../src/utils/deepAnalysisRequest.ts'

test('keeps the legacy analysis request unchanged without formal context', () => {
  assert.deepEqual(
    buildDeepAnalysisRequest('리바로젯', 'general'),
    { brandName: '리바로젯', view: 'general' },
  )
})

test('sends the additive formal contract without mixing the legacy view', () => {
  assert.deepEqual(
    buildDeepAnalysisRequest('리바로젯', 'general', {
      viewKind: 'general',
      marketId: 'C10C',
      source: 'UBIST',
    }),
    {
      brandName: '리바로젯',
      view_kind: 'general',
      market_id: 'C10C',
      source: 'ubist',
    },
  )
})

test('makes source part of the analysis request identity', () => {
  const ubist = deepAnalysisRequestKey('리바로젯', 'general', 'UBIST', 'C10C')
  const iqvia = deepAnalysisRequestKey('리바로젯', 'general', 'IQVIA', 'C10C')

  assert.notEqual(ubist, iqvia)
})

test('prefers a unique view-specific catalog context for market_id', () => {
  const catalog = [{
    brand: '리바로젯',
    market_id: 'strategy_006',
    atc_codes: ['C10A1', 'C10C'],
    contexts: [
      { view_kind: 'general', market_id: 'C10C' },
      { view_kind: 'strategic_ml', market_id: 'ml_006' },
    ],
  }]

  assert.equal(resolveDeepAnalysisMarketId(catalog, '리바로젯', 'general'), 'C10C')
  assert.equal(resolveDeepAnalysisMarketId(catalog, '리바로젯', 'strategic_ml'), 'ml_006')
})

test('selects the source-specific general market_id when catalog contexts differ', () => {
  const catalog = [{
    brand: '리바로젯',
    contexts: [
      { view_kind: 'general', market_id: 'C10C', source: 'ubist' },
      { view_kind: 'general', market_id: 'C10C0', source: 'iqvia' },
    ],
  }]

  assert.equal(resolveDeepAnalysisMarketId(catalog, '리바로젯', 'general', 'UBIST'), 'C10C')
  assert.equal(resolveDeepAnalysisMarketId(catalog, '리바로젯', 'general', 'IQVIA'), 'C10C0')
})

test('does not reuse legacy market ids for formal general or strategic requests', () => {
  const catalog = [{
    brand: '리바로젯',
    market_id: 'strategy_006',
    atc_codes: ['C10A1', 'C10C'],
  }]

  assert.equal(resolveDeepAnalysisMarketId(catalog, '리바로젯', 'general'), undefined)
  assert.equal(resolveDeepAnalysisMarketId(catalog, '리바로젯', 'strategic_ml'), undefined)
})

test('turns source_not_available into an available-context guidance message', () => {
  const error = parseDeepAnalysisError(422, {
    detail: {
      error: 'source_not_available',
      message: 'source is not available for the requested market',
      available_contexts: [
        { view_kind: 'strategic_ml', market_id: 'ml_006', source: 'ubist' },
      ],
    },
  })

  assert.equal(error.code, 'source_not_available')
  assert.equal(formatDeepAnalysisError(error), '이 브랜드는 UBIST만 제공됩니다.')
})

test('distinguishes an ambiguous 409 from loading and names the available sources', () => {
  const error = parseDeepAnalysisError(409, {
    result: {
      detail: {
        error: 'ambiguous_market_context',
        available_contexts: [
          { view_kind: 'general', market_id: 'A01AA', source: 'ubist' },
          { view_kind: 'general', market_id: 'A01AB', source: 'iqvia' },
        ],
      },
    },
  })

  assert.equal(error.status, 409)
  assert.equal(
    formatDeepAnalysisError(error),
    '분석할 시장을 하나로 정할 수 없습니다. 선택 가능한 시장: UBIST A01AA, IQVIA A01AB',
  )
})

test('keeps distinct market ids visible when ambiguous contexts share a source', () => {
  const error = parseDeepAnalysisError(409, {
    detail: {
      error: 'ambiguous_market_context',
      available_contexts: [
        { view_kind: 'general', market_id: 'C10A1', source: 'ubist' },
        { view_kind: 'general', market_id: 'C10C', source: 'ubist' },
      ],
    },
  })

  assert.equal(
    formatDeepAnalysisError(error),
    '분석할 시장을 하나로 정할 수 없습니다. 선택 가능한 시장: UBIST C10A1, UBIST C10C',
  )
})
