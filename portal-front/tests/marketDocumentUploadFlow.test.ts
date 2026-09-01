import assert from 'node:assert/strict'
import test from 'node:test'

import {
  DocumentListHttpError,
  documentListFailureMessage,
  resolveDocumentsBeforeUpload,
} from '../src/utils/marketDocumentListPolicy.ts'
import { installRuntimeConfig } from '../src/config/runtimeConfig.ts'
import {
  DOC_ALERT,
  fetchMarketDocuments,
  getUploadStatus,
  loadPendingUpload,
  refreshMarketDocuments,
  savePendingUpload,
  clearPendingUpload,
  uploadMarketDocuments,
  validateFiles,
  waitForUpload,
} from '../src/utils/marketDocuments.ts'

const EMPTY_DOCUMENTS: readonly unknown[] = []

test('continues from a fresh-session list 404 and reaches upload before the successful refresh', async () => {
  const events: string[] = []
  const documents = await resolveDocumentsBeforeUpload({
    isFreshSession: true,
    localDocumentCount: 0,
    loadDocuments: async () => {
      events.push('list:404')
      throw new DocumentListHttpError(404)
    },
  })

  events.push('upload:200')
  events.push('list:200')

  assert.deepEqual(documents, EMPTY_DOCUMENTS)
  assert.deepEqual(events, ['list:404', 'upload:200', 'list:200'])
})

test('does not turn an existing or foreign session list 404 into an empty list', async () => {
  await assert.rejects(
    resolveDocumentsBeforeUpload({
      isFreshSession: false,
      localDocumentCount: 0,
      loadDocuments: async () => { throw new DocumentListHttpError(404) },
    }),
    (error: unknown) => error instanceof DocumentListHttpError && error.status === 404,
  )
})

test('does not turn a fresh-session permission failure into an empty list', async () => {
  const error = new DocumentListHttpError(403)

  await assert.rejects(
    resolveDocumentsBeforeUpload({
      isFreshSession: true,
      localDocumentCount: 0,
      loadDocuments: async () => { throw error },
    }),
    (caught: unknown) => caught === error,
  )
  assert.equal(documentListFailureMessage(error), '문서 목록을 불러올 권한이 없습니다.')
})

test('keeps non-HTTP list failures visible to the user', () => {
  assert.equal(
    documentListFailureMessage(new TypeError('network down')),
    '문서 목록을 불러오지 못했습니다.',
  )
})

test('rejects unsupported extensions before any document request is needed', () => {
  const unsupported = new File(['probe'], 'r124e-probe.exe')

  assert.equal(validateFiles([unsupported], 0), DOC_ALERT.unsupported)
})

test('bypasses the cached fresh-session 404 when refreshing after upload', async () => {
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
    value: {
      getItem: () => null,
      removeItem: () => undefined,
      setItem: () => undefined,
    },
  })

  const responses = [
    new Response(JSON.stringify({ detail: 'session not found' }), { status: 404 }),
    Response.json({
      status: 'SUCCESS',
      result: {
        documents: [{
          document_id: 117472,
          file_name: 'r124e-probe-01.pdf',
          file_size_bytes: 16691,
          chunk_count: 1,
          uploaded_at: '2026-08-13T05:31:00Z',
          expires_at: '2026-08-14T05:31:00Z',
          is_expired: false,
        }],
      },
    }),
  ]
  let fetchCalls = 0
  Object.defineProperty(globalThis, 'fetch', {
    configurable: true,
    value: async () => {
      const response = responses[fetchCalls]
      fetchCalls += 1
      if (!response) throw new Error('unexpected fetch call')
      return response
    },
  })

  await assert.rejects(
    fetchMarketDocuments('00000000-0000-4000-8000-000000000001'),
    (error: unknown) => error instanceof DocumentListHttpError && error.status === 404,
  )
  const refreshed = await refreshMarketDocuments('00000000-0000-4000-8000-000000000001')

  assert.equal(fetchCalls, 2)
  assert.deepEqual(refreshed.map(document => document.file_name), ['r124e-probe-01.pdf'])
})

test('requests accepted processing and follows the status endpoint contract', async () => {
  installRuntimeConfig({
    apiBaseUrl: '/',
    googleClientId: 'test.apps.googleusercontent.com',
    genosNavigationUrl: 'https://genos.example.invalid',
    routerBasename: '/',
    marketDocumentWorkflowId: 301,
    marketAcceptedUploadEnabled: true,
  })
  let uploadedBody: FormData | null = null
  class AcceptedUploadRequest {
    readonly upload = { onprogress: null }
    timeout = 0
    status = 0
    statusText = ''
    responseText = ''
    onload: (() => void) | null = null
    onerror: (() => void) | null = null
    ontimeout: (() => void) | null = null
    open(): void {}
    setRequestHeader(): void {}
    getAllResponseHeaders(): string { return 'content-type: application/json\r\n' }
    send(body: FormData): void {
      uploadedBody = body
      this.status = 200
      this.statusText = 'OK'
      this.responseText = JSON.stringify({
        status: 'SUCCESS',
        result: { upload_id: 'upload-1', state: 'accepted', ready: false, files: [] },
      })
      this.onload?.()
    }
  }
  Object.defineProperty(globalThis, 'XMLHttpRequest', {
    configurable: true,
    value: AcceptedUploadRequest,
  })
  const calls: string[] = []
  Object.defineProperty(globalThis, 'fetch', {
    configurable: true,
    value: async (input: string | URL | Request) => {
      calls.push(String(input))
      return Response.json({
        status: 'SUCCESS',
        result: {
          upload_id: 'upload-1', state: 'ready', ready: true,
          files: [{
            file_name: 'report.pdf',
            state: 'ready',
            query_ready: true,
            message: '완료',
            indexed_pages: 20,
            total_pages: 270,
            card: { size_bytes: 1024 },
            phases: [{ name: 'embed', state: 'done', processed: 20, total: 20, unit: 'chunks' }],
          }],
        },
      })
    },
  })

  const accepted = await uploadMarketDocuments('session-a', [new File(['pdf'], 'report.pdf')])
  assert.equal(uploadedBody?.get('return_when'), 'accepted')
  assert.equal(accepted?.state, 'accepted')
  assert.equal(accepted?.uploadId, 'upload-1')
  const status = await getUploadStatus('session-a', 'upload-1')
  assert.equal(status.state, 'ready')
  assert.deepEqual(status.files[0], {
    file_name: 'report.pdf',
    state: 'ready',
    query_ready: true,
    message: '완료',
    indexed_pages: 20,
    total_pages: 270,
    card: { size_bytes: 1024 },
    phases: [{ name: 'embed', state: 'done', processed: 20, total: 20, unit: 'chunks' }],
  })
  assert.match(calls[0], /document\/upload\/status/)
  assert.match(calls[0], /upload_id=upload-1/)
})

test('polls accepted work through processing to ready', async () => {
  const states = ['accepted', 'preprocessing', 'ready'] as const
  let index = 0
  Object.defineProperty(globalThis, 'fetch', {
    configurable: true,
    value: async () => {
      const state = states[index++]
      return Response.json({
        status: 'SUCCESS',
        result: { upload_id: 'upload-2', state, ready: state === 'ready', files: [] },
      })
    },
  })
  const observed: string[] = []
  const terminal = await waitForUpload('session-a', 'upload-2', status => observed.push(status.state), {
    intervalMs: 0,
    sleep: async () => undefined,
  })

  assert.equal(terminal.state, 'ready')
  assert.deepEqual(observed, ['accepted', 'preprocessing', 'ready'])
})

test('F4 canonicalizes unknown top-level and file states without exposing raw values', async () => {
  Object.defineProperty(globalThis, 'fetch', {
    configurable: true,
    value: async () => Response.json({
      status: 'SUCCESS',
      result: {
        upload_id: 'upload-unknown',
        state: 'future_internal_state',
        ready: true,
        files: [{ file_name: 'future.pdf', state: 'future_file_state' }],
      },
    }),
  })

  const status = await getUploadStatus('session-a', 'upload-unknown')

  assert.equal(status.state, 'unknown')
  assert.equal(status.ready, false)
  assert.equal(status.files[0]?.state, 'unknown')
  assert.doesNotMatch(JSON.stringify(status), /future_internal_state|future_file_state/)
})

test('F5 keeps an unknown state non-terminal and stops at the existing polling cap', async () => {
  Object.defineProperty(globalThis, 'fetch', {
    configurable: true,
    value: async () => Response.json({
      status: 'SUCCESS',
      result: {
        upload_id: 'upload-unknown-timeout',
        state: 'future_internal_state',
        ready: true,
        files: [{ file_name: 'future.pdf', state: 'future_file_state' }],
      },
    }),
  })
  const observed: string[] = []

  await assert.rejects(
    waitForUpload(
      'session-a',
      'upload-unknown-timeout',
      status => observed.push(status.state),
      { intervalMs: 0, timeoutMs: -1, sleep: async () => undefined },
    ),
    /timed out/i,
  )
  assert.deepEqual(observed, ['unknown'])
})

test('persists an accepted upload so a returning tab can resume it', () => {
  const values = new Map<string, string>()
  Object.defineProperty(globalThis, 'sessionStorage', {
    configurable: true,
    value: {
      getItem: (key: string) => values.get(key) ?? null,
      setItem: (key: string, value: string) => values.set(key, value),
      removeItem: (key: string) => values.delete(key),
    },
  })

  savePendingUpload({ appSessionId: 'session-a', uploadId: 'upload-3', fileNames: ['report.pdf'], startedAtMs: 1234 })
  assert.deepEqual(loadPendingUpload('session-a'), {
    appSessionId: 'session-a', uploadId: 'upload-3', fileNames: ['report.pdf'], startedAtMs: 1234,
  })
  assert.equal(loadPendingUpload('session-b'), null)
  clearPendingUpload('upload-3')
  assert.equal(loadPendingUpload('session-a'), null)
})
