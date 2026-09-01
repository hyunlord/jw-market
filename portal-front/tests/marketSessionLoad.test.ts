import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import {
  createSingleFlight,
  loadSessionLogFirst,
  sessionHasDocumentReferences,
} from '../src/utils/marketSessionLoad.ts'

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => { resolve = done })
  return { promise, resolve }
}

test('publishes chat history before a slow document lookup completes', async () => {
  const documents = deferred<string[]>()
  const renders: Array<{ log: string; documents: string[] }> = []

  await loadSessionLogFirst({
    loadLog: async () => 'historic answer',
    loadDocuments: () => documents.promise,
    publish: (log, docs) => renders.push({ log, documents: docs }),
  })

  assert.deepEqual(renders, [{ log: 'historic answer', documents: [] }])

  documents.resolve(['report.pdf'])
  await documents.promise
  await new Promise(resolve => setTimeout(resolve, 0))

  assert.deepEqual(renders, [
    { log: 'historic answer', documents: [] },
    { log: 'historic answer', documents: ['report.pdf'] },
  ])
})

test('keeps chat history visible when document lookup fails', async () => {
  const renders: Array<{ log: string; documents: string[] }> = []

  await loadSessionLogFirst({
    loadLog: async () => 'historic answer',
    loadDocuments: async () => { throw new Error('document backend timeout') },
    publish: (log, docs) => renders.push({ log, documents: docs }),
  })
  await new Promise(resolve => setTimeout(resolve, 0))

  assert.deepEqual(renders, [{ log: 'historic answer', documents: [] }])
})

test('coalesces concurrent document lookups for the same session', async () => {
  const pending = deferred<string[]>()
  let calls = 0
  const load = createSingleFlight<string, string[]>(async () => {
    calls += 1
    return pending.promise
  })

  const first = load('session-a')
  const second = load('session-a')
  assert.equal(calls, 1)

  pending.resolve(['report.pdf'])
  assert.deepEqual(await first, ['report.pdf'])
  assert.deepEqual(await second, ['report.pdf'])
})

test('skips document lookup when a restored session has no attachment references', async () => {
  let documentCalls = 0
  const renders: Array<{ log: string; documents: string[] }> = []

  await loadSessionLogFirst({
    loadLog: async () => 'attachment-free answer',
    loadDocuments: async () => {
      documentCalls += 1
      return ['unexpected.pdf']
    },
    shouldLoadDocuments: () => false,
    publish: (log, documents) => renders.push({ log, documents }),
  })

  assert.equal(documentCalls, 0)
  assert.deepEqual(renders, [{ log: 'attachment-free answer', documents: [] }])
})

test('starts one attachment lookup only after the chat log is published', async () => {
  const log = deferred<string>()
  const documents = deferred<string[]>()
  const events: string[] = []
  let documentCalls = 0

  const loading = loadSessionLogFirst({
    loadLog: () => log.promise,
    loadDocuments: () => {
      documentCalls += 1
      events.push('documents-started')
      return documents.promise
    },
    shouldLoadDocuments: () => true,
    publish: (_value, attached) => events.push(attached.length ? 'documents-published' : 'log-published'),
  })

  assert.equal(documentCalls, 0)
  log.resolve('historic answer')
  await loading
  assert.deepEqual(events, ['log-published', 'documents-started'])
  assert.equal(documentCalls, 1)

  documents.resolve(['report.pdf'])
  await documents.promise
  await new Promise(resolve => setTimeout(resolve, 0))
  assert.deepEqual(events, ['log-published', 'documents-started', 'documents-published'])
})

test('negative-caches a failed single-flight lookup for a bounded cooldown', async () => {
  let now = 1_000
  let calls = 0
  const load = createSingleFlight<string, string[]>(
    async () => {
      calls += 1
      throw new Error('upstream 503')
    },
    { failureCooldownMs: 30_000, now: () => now },
  )

  await assert.rejects(load('session-a'), /upstream 503/)
  await assert.rejects(load('session-a'), /upstream 503/)
  assert.equal(calls, 1)

  now += 30_001
  await assert.rejects(load('session-a'), /upstream 503/)
  assert.equal(calls, 2)
})

test('detects attachment metadata without inspecting question or answer prose', () => {
  assert.equal(sessionHasDocumentReferences([{ data: {} }]), false)
  assert.equal(sessionHasDocumentReferences([{ data: { temp_documents: [{ file_name: 'report.pdf' }] } }]), true)
  assert.equal(sessionHasDocumentReferences([{ data: { sourceDocuments: [{ metadata: { file_name: 'report.pdf' } }] } }]), true)
})

test('document lookup rejects HTTP and application failures before negative caching', () => {
  const source = readFileSync(new URL('../src/utils/marketDocuments.ts', import.meta.url), 'utf8')

  assert.match(source, /if \(!res\.ok\) throw new DocumentListHttpError/)
  assert.match(source, /if \(data\.status !== 'SUCCESS'\) throw new Error/)
  assert.match(source, /failureCooldownMs: 30_000/)
})
