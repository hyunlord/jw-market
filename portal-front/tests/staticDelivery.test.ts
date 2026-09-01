import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

test('revalidates the HTML shell while keeping hashed assets immutable', () => {
  const nginx = readFileSync(new URL('../deploy/nginx.conf', import.meta.url), 'utf8')
  assert.match(nginx, /location = \/index\.html[\s\S]*no-cache[\s\S]*must-revalidate/)
  assert.match(nginx, /location \/assets\/[\s\S]*max-age=31536000, immutable/)
})
