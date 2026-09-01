import assert from 'node:assert/strict'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import test, { after } from 'node:test'
import react from '@vitejs/plugin-react'
import { createServer } from 'vite'

import { installRuntimeConfig } from '../src/config/runtimeConfig.ts'
import {
  formatBlockedUploadAlert,
  normalizeUploadResponse,
  uploadMarketDocuments,
} from '../src/utils/marketDocuments.ts'

const vite = await createServer({
  configFile: false,
  root: new URL('..', import.meta.url).pathname,
  plugins: [react()],
  optimizeDeps: { noDiscovery: true },
  server: { middlewareMode: true, hmr: { port: 24689 } },
  appType: 'custom',
})
const { default: Modals } = await vite.ssrLoadModule('/src/components/main/Modals.tsx') as {
  default: (props: { alertMessage: string }) => ReturnType<typeof createElement>
}
after(async () => vite.close())

installRuntimeConfig({
  apiBaseUrl: '/',
  googleClientId: 'test.apps.googleusercontent.com',
  genosNavigationUrl: 'https://genos.example.invalid',
  routerBasename: '/',
  marketDocumentWorkflowId: 301,
  marketAcceptedUploadEnabled: true,
})

test('FH1 renders a blocked upload with the backend file name and message', () => {
  const outcome = normalizeUploadResponse({
    status: 'SUCCESS',
    result: {
      blocked_uploads: [{
        route: 'blocked_oversized',
        file_name: 'large.pdf',
        message: '문서 처리 기준을 초과했습니다.',
      }],
    },
  })

  assert.equal(outcome.ready, false)
  assert.equal(outcome.state, 'blocked')
  const alert = formatBlockedUploadAlert(outcome.blockedUploads)
  const markup = renderToStaticMarkup(createElement(Modals, { alertMessage: alert }))
  assert.match(markup, /large\.pdf/)
  assert.match(markup, /문서 처리 기준을 초과했습니다\./)
})

test('FH2 keeps a committed file separate from a rejected file', () => {
  const outcome = normalizeUploadResponse({
    status: 'SUCCESS',
    result: {
      commit: {
        committed_count: 1,
        documents: [{ file_name: 'accepted.pdf' }],
      },
      blocked_uploads: [{
        route: 'preprocess_failed',
        file_name: 'rejected.pdf',
        message: '문서 내용을 처리하지 못했습니다.',
      }],
    },
  })

  assert.deepEqual(outcome.committedNames, ['accepted.pdf'])
  assert.equal(outcome.committedCount, 1)
  assert.equal(outcome.ready, true)
  assert.deepEqual(outcome.blockedUploads.map(item => item.file_name), ['rejected.pdf'])
})

test('FH3 surfaces an unknown rejection route as a general rejection', () => {
  const outcome = normalizeUploadResponse({
    status: 'SUCCESS',
    result: {
      blocked_uploads: [{
        route: 'future_policy',
        file_name: 'future.pdf',
        message: 'internal-policy: sql://private-host',
      }],
    },
  })

  const alert = formatBlockedUploadAlert(outcome.blockedUploads)
  assert.equal(alert, 'future.pdf: 처리하지 못했습니다.')
  assert.doesNotMatch(alert, /future_policy|internal-policy|private-host/)
})

test('F3 keeps an unknown rejection with an empty message visible', () => {
  const outcome = normalizeUploadResponse({
    status: 'SUCCESS',
    result: {
      blocked_uploads: [{ route: 'future_policy', file_name: 'empty.pdf', message: '' }],
    },
  })

  assert.equal(formatBlockedUploadAlert(outcome.blockedUploads), 'empty.pdf: 처리하지 못했습니다.')
})

test('FH4 preserves the response parsing error instead of returning null', async () => {
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    value: {
      getItem: () => null,
      removeItem: () => undefined,
      setItem: () => undefined,
    },
  })
  class InvalidJsonUploadRequest {
    readonly upload = { onprogress: null }
    timeout = 0
    status = 200
    statusText = 'OK'
    responseText = '{invalid-json'
    onload: (() => void) | null = null
    onerror: (() => void) | null = null
    ontimeout: (() => void) | null = null
    open(): void {}
    setRequestHeader(): void {}
    getAllResponseHeaders(): string { return 'content-type: application/json\r\n' }
    send(): void { this.onload?.() }
  }
  Object.defineProperty(globalThis, 'XMLHttpRequest', {
    configurable: true,
    value: InvalidJsonUploadRequest,
  })

  await assert.rejects(
    uploadMarketDocuments('session-fh4', [new File(['pdf'], 'broken.pdf')]),
    (error: unknown) => {
      assert.equal(error instanceof Error, true)
      assert.equal((error as Error & { cause?: unknown }).cause instanceof SyntaxError, true)
      return true
    },
  )
})

test('GH7 keeps the normal synchronous commit contract ready', () => {
  const outcome = normalizeUploadResponse({
    status: 'SUCCESS',
    result: {
      commit: {
        committed_count: 1,
        documents: [{ file_name: 'normal.pdf' }],
      },
      blocked_uploads: [],
    },
  })

  assert.equal(outcome.state, 'ready')
  assert.equal(outcome.ready, true)
  assert.deepEqual(outcome.committedNames, ['normal.pdf'])
  assert.deepEqual(outcome.blockedUploads, [])
})
