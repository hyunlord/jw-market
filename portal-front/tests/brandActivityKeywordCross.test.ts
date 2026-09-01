import assert from 'node:assert/strict'
import test from 'node:test'

import { keywordCrossDomain } from '../src/utils/brandActivityKeywordCross.ts'
import type { MatrixData } from '../src/types/market.ts'

const matrix: MatrixData = {
  scope: { view: 'general', market_id: 'C10A1', selected_brand: '리바로젯' },
  levels: {
    interest: ['VERY USEFUL', 'SOMEWHAT USEFUL', 'NOT AT ALL'],
    rx_frequency: [],
    prescription_evolution: ['increase', 'remain unchanged', 'decrease'],
  },
  brands: [{
    brand_name: '리바로젯',
    is_jw: true,
    is_selected: true,
    event_count: 10,
    interest_distribution: { 'VERY USEFUL': 4, 'SOMEWHAT USEFUL': 6 },
    rx_frequency_distribution: {},
    prescription_evolution_distribution: { increase: 7, decrease: 3 },
    interest_score: 1,
    rx_frequency_score: 1,
    prescription_evolution_score: 1,
  }],
  market_average: {
    interest_distribution: { 'NOT AT ALL': 0, 'VERY USEFUL': 4 },
    rx_frequency_distribution: {},
    prescription_evolution_distribution: { 'remain unchanged': 0, increase: 7 },
    interest_score: 1,
    rx_frequency_score: 1,
    prescription_evolution_score: 1,
  },
}

test('derives keyword cross domains from the backend response without a frontend axis list', () => {
  assert.deepEqual(keywordCrossDomain(matrix, 'interest'), [
    'VERY USEFUL',
    'SOMEWHAT USEFUL',
    'NOT AT ALL',
  ])
  assert.deepEqual(keywordCrossDomain(matrix, 'prescription_evolution'), [
    'increase',
    'remain unchanged',
    'decrease',
  ])
})

test('fails closed when the backend response has no values for the selected mode', () => {
  assert.throws(
    () => keywordCrossDomain(null, 'interest'),
    /keyword cross domain is unavailable/,
  )
})
