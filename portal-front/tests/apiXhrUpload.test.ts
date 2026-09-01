import assert from 'node:assert/strict'
import test from 'node:test'

import { installRuntimeConfig } from '../src/config/runtimeConfig.ts'
import { apiUploadFormData } from '../src/utils/apiFetch.ts'

interface FakeProgressEvent {
  loaded: number
  total: number
  lengthComputable: boolean
}

class FakeXmlHttpRequest {
  static instances: FakeXmlHttpRequest[] = []

  readonly headers = new Map<string, string>()
  readonly upload: { onprogress: ((event: FakeProgressEvent) => void) | null } = { onprogress: null }
  method = ''
  url = ''
  body: Document | XMLHttpRequestBodyInit | null = null
  timeout = 0
  status = 0
  statusText = ''
  responseText = ''
  onload: (() => void) | null = null
  onerror: (() => void) | null = null
  ontimeout: (() => void) | null = null

  constructor() {
    FakeXmlHttpRequest.instances.push(this)
  }

  open(method: string, url: string): void {
    this.method = method
    this.url = url
  }

  setRequestHeader(name: string, value: string): void {
    this.headers.set(name, value)
  }

  getAllResponseHeaders(): string {
    return 'content-type: application/json\r\n'
  }

  send(body: Document | XMLHttpRequestBodyInit | null): void {
    this.body = body
    this.upload.onprogress?.({ loaded: 64, total: 100, lengthComputable: true })
    this.status = 200
    this.statusText = 'OK'
    this.responseText = JSON.stringify({ status: 'SUCCESS', result: { state: 'accepted' } })
    this.onload?.()
  }
}

test('uploads multipart data with auth, bundle telemetry, timeout, and byte progress', async () => {
  installRuntimeConfig({
    apiBaseUrl: 'https://portal.example.test',
    googleClientId: 'test.apps.googleusercontent.com',
    genosNavigationUrl: 'https://genos.example.invalid',
    routerBasename: '/',
    marketDocumentWorkflowId: 301,
    marketAcceptedUploadEnabled: true,
  })
  Object.defineProperty(globalThis, 'localStorage', {
    configurable: true,
    value: {
      getItem: (key: string) => ({ portalToken: 'portal-token', accessToken: 'access-token' })[key] ?? null,
      removeItem: () => undefined,
      setItem: () => undefined,
    },
  })
  Object.defineProperty(globalThis, 'XMLHttpRequest', {
    configurable: true,
    value: FakeXmlHttpRequest,
  })

  const body = new FormData()
  body.append('file', new File(['pdf'], 'report.pdf'))
  const progress: Array<{ loadedBytes: number; totalBytes: number | null }> = []
  const response = await apiUploadFormData('/api/v1/market/chat/document/upload', body, {
    timeoutMs: 180_000,
    onProgress: value => progress.push(value),
  })

  const request = FakeXmlHttpRequest.instances.at(-1)
  assert.ok(request)
  assert.equal(request.method, 'POST')
  assert.equal(request.url, 'https://portal.example.test/api/v1/market/chat/document/upload')
  assert.equal(request.timeout, 180_000)
  assert.equal(request.body, body)
  assert.equal(request.headers.get('Authorization'), 'Bearer portal-token')
  assert.equal(request.headers.get('Authorization-Access-Token'), 'access-token')
  assert.equal(request.headers.get('X-Portal-Bundle'), 'non-browser')
  assert.equal(request.headers.has('Content-Type'), false)
  assert.deepEqual(progress, [{ loadedBytes: 64, totalBytes: 100 }])
  assert.equal(response.status, 200)
})
