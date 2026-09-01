import assert from 'node:assert/strict'
import test from 'node:test'

import { MARKET_STREAM_CLIENT_TIMEOUT_MS } from '../src/utils/marketStream.ts'

test('market stream client keeps the shared 510 second budget', () => {
  assert.equal(MARKET_STREAM_CLIENT_TIMEOUT_MS, 510_000)
})
