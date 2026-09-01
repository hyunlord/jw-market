import assert from 'node:assert/strict'
import test from 'node:test'

import { selectRankTrackerKeys } from '../src/utils/chartHelpers.ts'

test('rank tracker preserves the backend selected brand plus five competitors', () => {
  const competitors = ['경쟁1', '경쟁2', '경쟁3', '경쟁4', '경쟁5']
  const rank = new Map([
    ['경쟁1', 1],
    ['선택', 2],
    ['경쟁2', 3],
    ['경쟁3', 4],
    ['경쟁4', 5],
    ['경쟁5', 6],
  ])

  const shown = selectRankTrackerKeys(competitors, '선택', key => rank.get(key) ?? 999)

  assert.deepEqual(shown, ['경쟁1', '선택', '경쟁2', '경쟁3', '경쟁4', '경쟁5'])
})

test('showing the sixth brand moves no value into others and preserves the denominator', () => {
  const brandShares = [20, 18, 15, 12, 10, 5]
  const backendOthers = 20
  const shown = selectRankTrackerKeys(
    ['경쟁1', '경쟁2', '경쟁3', '경쟁4', '경쟁5'],
    '선택',
    key => ['경쟁1', '선택', '경쟁2', '경쟁3', '경쟁4', '경쟁5'].indexOf(key) + 1,
  )

  assert.equal(shown.length, 6)
  assert.equal(backendOthers, 20)
  assert.equal(brandShares.reduce((sum, value) => sum + value, backendOthers), 100)
})
