import assert from 'node:assert/strict'
import test from 'node:test'

import { captureFirstTtft, formatTtft } from '../src/utils/marketTtft.ts'

test('captures only the first non-empty answer arrival', () => {
  const first = captureFirstTtft(undefined, 1_000, 2_234)
  const later = captureFirstTtft(first, 1_000, 4_500)

  assert.equal(first, 1_234)
  assert.equal(later, 1_234)
})

test('clamps a negative measured duration to zero', () => {
  assert.equal(captureFirstTtft(undefined, 2_000, 1_500), 0)
})

test('formats TTFT in seconds with one decimal place', () => {
  assert.equal(formatTtft(1_234), '첫 응답 1.2초')
})
