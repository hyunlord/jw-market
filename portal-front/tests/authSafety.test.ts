import assert from 'node:assert/strict'
import test from 'node:test'

import { assertSafePortalAuthEnv } from '../scripts/assert-safe-auth-env.ts'
import {
  getPagePermission,
  hasClientPagePermission,
} from '../src/utils/pagePermission.ts'
import { installRuntimeConfig } from '../src/config/runtimeConfig.ts'

function memoryStorage(): Storage {
  const values = new Map<string, string>()
  return {
    get length() { return values.size },
    clear: () => values.clear(),
    getItem: key => values.get(key) ?? null,
    key: index => [...values.keys()][index] ?? null,
    removeItem: key => { values.delete(key) },
    setItem: (key, value) => { values.set(key, value) },
  }
}

function portalToken(authorities: string[]): string {
  const payload = Buffer.from(JSON.stringify({
    pageAuthorities: authorities,
  })).toString('base64url')
  return `header.${payload}.signature`
}

test('safe auth gate accepts an unset or false bypass', () => {
  assert.doesNotThrow(() => assertSafePortalAuthEnv({}))
  assert.doesNotThrow(() => assertSafePortalAuthEnv({
    TEST_LOGIN_BYPASS: 'false',
  }))
})

test('safe auth gate rejects attempts to reactivate the test login bypass', () => {
  assert.throws(
    () => assertSafePortalAuthEnv({ TEST_LOGIN_BYPASS: 'true' }),
    /TEST_LOGIN_BYPASS=true is forbidden/,
  )
})

test('client and server checks remain an AND gate', async () => {
  installRuntimeConfig({
    apiBaseUrl: '/',
    googleClientId: 'test.apps.googleusercontent.com',
    genosNavigationUrl: 'https://genos.example.invalid',
    routerBasename: '/',
    marketDocumentWorkflowId: 301,
    marketAcceptedUploadEnabled: true,
  })
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    value: memoryStorage(),
  })
  Object.defineProperty(globalThis, 'sessionStorage', {
    configurable: true,
    value: memoryStorage(),
  })

  let fetchCalls = 0
  Object.defineProperty(globalThis, 'fetch', {
    configurable: true,
    value: async () => {
      fetchCalls += 1
      return new Response(
        JSON.stringify({ status: 'SUCCESS', result: false }),
        { status: 200 },
      )
    },
  })

  localStorage.setItem('portalToken', portalToken(['RND']))
  assert.equal(hasClientPagePermission('/market'), false)
  assert.equal(await getPagePermission('/market'), false)
  assert.equal(fetchCalls, 0)

  localStorage.setItem('portalToken', portalToken(['MARKET']))
  sessionStorage.clear()
  assert.equal(hasClientPagePermission('/market'), true)
  assert.equal(await getPagePermission('/market'), false)
  assert.equal(fetchCalls, 1)
})
