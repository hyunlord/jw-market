import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const component = readFileSync(new URL('../src/components/main/UploadProgressList.tsx', import.meta.url), 'utf8')
const hook = readFileSync(new URL('../src/utils/useMarketDocuments.ts', import.meta.url), 'utf8')

test('renders exact transfer progress and transitions to server-backed file states', () => {
  assert.match(component, /role="progressbar"/)
  assert.match(component, /aria-valuenow=/)
  assert.match(component, /파일 전송 중/)
  assert.match(component, /완료/)
  assert.match(component, /UPLOAD_STATE_LABELS/)
})

test('keeps status lookup failure distinct from processing failure', () => {
  assert.match(component, /상태를 확인하지 못했습니다\./)
  assert.match(component, /다시 확인/)
  assert.match(hook, /createStatusUnavailableProgress/)
  assert.match(hook, /retryUploadStatus/)
})

test('offers file re-selection for terminal failures without hiding successful files', () => {
  assert.match(component, /다시 업로드/)
  assert.match(component, /failedCount/)
  assert.match(hook, /setPendingDocs/)
})

test('does not expose backend states or claim an admission queue position', () => {
  for (const forbidden of ['preprocessing', 'committing', 'accepted', '대기 순번', '예상 대기']) {
    assert.equal(component.includes(forbidden), false, forbidden)
  }
})
