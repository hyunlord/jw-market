import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import {
  getRuntimeConfig,
  installRuntimeConfig,
  loadRuntimeConfig,
  resolveApiUrl,
  resolvePortalPath,
  validateRuntimeConfig,
} from '../src/config/runtimeConfig.ts'
import { apiFetch } from '../src/utils/apiFetch.ts'

const COMPLETE_CONFIG = {
  apiBaseUrl: 'https://api.example.invalid',
  googleClientId: 'public-client.apps.googleusercontent.com',
  genosNavigationUrl: 'https://genos.example.invalid',
  routerBasename: '/',
  marketDocumentWorkflowId: 301,
  marketAcceptedUploadEnabled: true,
}

test('runtime config accepts a complete explicit document', () => {
  assert.deepEqual(validateRuntimeConfig(COMPLETE_CONFIG), COMPLETE_CONFIG)
})

test('runtime config fails closed for missing, empty, null, or invalid values', () => {
  const invalidDocuments = [
    { ...COMPLETE_CONFIG, googleClientId: undefined },
    { ...COMPLETE_CONFIG, googleClientId: '' },
    { ...COMPLETE_CONFIG, googleClientId: null },
    { ...COMPLETE_CONFIG, apiBaseUrl: '' },
    { ...COMPLETE_CONFIG, genosNavigationUrl: 'not-a-url' },
    { ...COMPLETE_CONFIG, routerBasename: '' },
    { ...COMPLETE_CONFIG, routerBasename: '/dev' },
    { ...COMPLETE_CONFIG, marketDocumentWorkflowId: 0 },
  ]

  for (const document of invalidDocuments) {
    assert.throws(() => validateRuntimeConfig(document), /runtime config/i)
  }
})

test('runtime config load fails on HTTP errors and uses no-store on success', async () => {
  await assert.rejects(
    () => loadRuntimeConfig(async () => new Response('', { status: 404 })),
    /404/,
  )

  let request: RequestInfo | URL | undefined
  let init: RequestInit | undefined
  const loaded = await loadRuntimeConfig(async (input, options) => {
    request = input
    init = options
    return Response.json(COMPLETE_CONFIG)
  })

  assert.equal(request, '/config.json')
  assert.equal(init?.cache, 'no-store')
  assert.deepEqual(loaded, COMPLETE_CONFIG)
  assert.deepEqual(getRuntimeConfig(), COMPLETE_CONFIG)
})

test('the same code resolves API requests from the installed environment config', () => {
  installRuntimeConfig({ ...COMPLETE_CONFIG, apiBaseUrl: '/dev-api' })
  assert.equal(resolveApiUrl('/api/v1/market/status'), '/dev-api/api/v1/market/status')

  installRuntimeConfig({ ...COMPLETE_CONFIG, apiBaseUrl: 'https://api.prod.invalid' })
  assert.equal(resolveApiUrl('/api/v1/market/status'), 'https://api.prod.invalid/api/v1/market/status')

  installRuntimeConfig(COMPLETE_CONFIG)
  assert.equal(resolvePortalPath('/login'), '/login')
})

test('apiFetch sends the request to the configured API target', async () => {
  const values = new Map<string, string>()
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    value: {
      getItem: (key: string) => values.get(key) ?? null,
      removeItem: (key: string) => values.delete(key),
      setItem: (key: string, value: string) => values.set(key, value),
    },
  })

  let requestedUrl = ''
  Object.defineProperty(globalThis, 'fetch', {
    configurable: true,
    value: async (input: RequestInfo | URL) => {
      requestedUrl = String(input)
      return new Response('{}', { status: 200 })
    },
  })

  installRuntimeConfig({ ...COMPLETE_CONFIG, apiBaseUrl: 'https://api.dev.invalid' })
  await apiFetch('/api/v1/market/status', { method: 'POST' })
  assert.equal(requestedUrl, 'https://api.dev.invalid/api/v1/market/status')

  installRuntimeConfig({ ...COMPLETE_CONFIG, apiBaseUrl: 'https://api.prod.invalid' })
  await apiFetch('/api/v1/market/status', { method: 'POST' })
  assert.equal(requestedUrl, 'https://api.prod.invalid/api/v1/market/status')
})

test('production source has one runtime channel and no VITE consumer', () => {
  const sources = [
    '../src/main.tsx',
    '../src/App.tsx',
    '../src/pages/LoginPage.tsx',
    '../src/utils/apiFetch.ts',
    '../src/utils/pagePermission.ts',
    '../src/utils/marketDocuments.ts',
    '../src/components/main/MarketTopNav.tsx',
    '../src/components/main/TopNavigation.tsx',
    '../vite.config.ts',
  ].map(path => readFileSync(new URL(path, import.meta.url), 'utf8')).join('\n')

  assert.doesNotMatch(sources, /import\.meta\.env|\bVITE_[A-Z0-9_]+\b|loadEnv\(/)
  assert.match(sources, /loadRuntimeConfig/)
  assert.match(sources, /resolveApiUrl/)
  assert.match(sources, /marketDocumentWorkflowId/)
  assert.match(sources, /genosNavigationUrl/)
  assert.match(sources, /routerBasename/)
})

test('vite fixes the shared image asset base at the authority root', () => {
  const viteConfig = readFileSync(
    new URL('../vite.config.ts', import.meta.url),
    'utf8',
  )

  assert.match(
    viteConfig,
    /base:\s*['"]\/['"]/,
  )
})

test('container startup rejects a non-root router basename', () => {
  const result = spawnSync(
    'sh',
    [new URL('../deploy/15-runtime-config-validate.sh', import.meta.url).pathname],
    {
      encoding: 'utf8',
      env: {
        ...process.env,
        PORTAL_API_BASE_URL: '/api/v1',
        PORTAL_GOOGLE_CLIENT_ID: 'public-client.apps.googleusercontent.com',
        PORTAL_GENOS_NAVIGATION_URL: 'https://genos.example.invalid',
        PORTAL_ROUTER_BASENAME: '/dev',
        PORTAL_MARKET_DOCUMENT_WORKFLOW_ID: '301',
        PORTAL_MARKET_ACCEPTED_UPLOAD_ENABLED: 'true',
      },
    },
  )

  assert.notEqual(result.status, 0)
  assert.match(result.stderr.trim(), /PORTAL_ROUTER_BASENAME.*must be \/$/)
})

test('container template exposes every required runtime key without a build-time value', () => {
  const template = readFileSync(
    new URL('../deploy/config.json.template', import.meta.url),
    'utf8',
  )
  assert.match(template, /\$\{PORTAL_API_BASE_URL\}/)
  assert.match(template, /\$\{PORTAL_GOOGLE_CLIENT_ID\}/)
  assert.match(template, /\$\{PORTAL_GENOS_NAVIGATION_URL\}/)
  assert.match(template, /\$\{PORTAL_ROUTER_BASENAME\}/)
  assert.match(template, /\$\{PORTAL_MARKET_DOCUMENT_WORKFLOW_ID\}/)
  assert.match(template, /\$\{PORTAL_MARKET_ACCEPTED_UPLOAD_ENABLED\}/)
  assert.doesNotMatch(template, /372798032844|jwai-dev\.jwhealthcare\.com/)
})
