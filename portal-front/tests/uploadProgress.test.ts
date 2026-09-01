import assert from 'node:assert/strict'
import test from 'node:test'

import {
  calculateTransferPercent,
  createProcessingProgress,
  createProcessingFiles,
  createStatusUnavailableProgress,
  createTransferProgress,
  isFailedUploadState,
  isTerminalUploadState,
  summarizeUploadFiles,
  UPLOAD_STATE_LABELS,
} from '../src/utils/uploadProgress.ts'

test('maps every backend state to an approved user-facing label', () => {
  assert.deepEqual(UPLOAD_STATE_LABELS, {
    accepted: '처리 준비 중',
    preprocessing: '파일 처리 중',
    committing: '검색 준비 중',
    ready: '완료',
    blocked: '처리할 수 없음',
    failed: '처리 실패',
    interrupted: '처리 중단',
    expired: '상태 확인 만료',
    unknown: '상태 미상',
  })
})

test('classifies every terminal state without treating active work as terminal', () => {
  for (const state of ['ready', 'blocked', 'failed', 'interrupted', 'expired'] as const) {
    assert.equal(isTerminalUploadState(state), true, state)
  }
  for (const state of ['accepted', 'preprocessing', 'committing'] as const) {
    assert.equal(isTerminalUploadState(state), false, state)
  }
  assert.equal(isTerminalUploadState('unknown' as never), false, 'unknown')
})

test('counts only ready files as complete and reports failures separately', () => {
  const summary = summarizeUploadFiles([
    { fileName: 'ready.pdf', state: 'ready' },
    { fileName: 'blocked.pdf', state: 'blocked' },
    { fileName: 'failed.pdf', state: 'failed' },
    { fileName: 'working.pdf', state: 'preprocessing' },
  ])

  assert.deepEqual(summary, { totalCount: 4, readyCount: 1, failedCount: 2 })
  assert.equal(isFailedUploadState('interrupted'), true)
  assert.equal(isFailedUploadState('expired'), true)
  assert.equal(isFailedUploadState('ready'), false)
})

test('uses the overall terminal state for files left in a stale active state', () => {
  const files = createProcessingFiles({
    state: 'expired',
    files: [{ file_name: 'report.pdf', state: 'preprocessing', message: null }],
  }, ['report.pdf'])

  assert.deepEqual(files, [{ fileName: 'report.pdf', state: 'expired', message: null }])
})

test('uses local file names until the status API returns per-file records', () => {
  const files = createProcessingFiles({ state: 'accepted', files: [] }, ['one.pdf', 'two.pdf'])

  assert.deepEqual(files, [
    { fileName: 'one.pdf', state: 'accepted' },
    { fileName: 'two.pdf', state: 'accepted' },
  ])
})

test('carries preview eligibility and the backend message into the render model', () => {
  const files = createProcessingFiles({
    state: 'preprocessing',
    files: [{
      file_name: 'long.pdf',
      state: 'preprocessing',
      message: '앞 20/270페이지는 지금 질문할 수 있습니다.',
      query_ready: true,
      indexed_pages: 20,
      total_pages: 270,
    }],
  }, ['long.pdf'])

  assert.deepEqual(files, [{
    fileName: 'long.pdf',
    state: 'preprocessing',
    message: '앞 20/270페이지는 지금 질문할 수 있습니다.',
    queryReady: true,
    indexedPages: 20,
    totalPages: 270,
  }])
})

test('calculates byte progress only when the browser supplies a usable total', () => {
  assert.equal(calculateTransferPercent(0, 100), 0)
  assert.equal(calculateTransferPercent(64, 100), 64)
  assert.equal(calculateTransferPercent(110, 100), 100)
  assert.equal(calculateTransferPercent(1, 0), null)
  assert.equal(calculateTransferPercent(1, null), null)
})

test('moves from exact transfer bytes to server-backed processing without inventing percent', () => {
  const transfer = createTransferProgress(['one.pdf', 'two.pdf'], 1_000, 64, 100)
  assert.deepEqual(transfer, {
    phase: 'transferring',
    fileNames: ['one.pdf', 'two.pdf'],
    startedAtMs: 1_000,
    loadedBytes: 64,
    totalBytes: 100,
    percent: 64,
  })

  const processing = createProcessingProgress({
    uploadId: 'upload-1', state: 'preprocessing', ready: false, files: [],
  }, ['one.pdf', 'two.pdf'], 1_000)
  assert.equal(processing.phase, 'processing')
  assert.equal('percent' in processing, false)
  assert.deepEqual(processing.files.map(file => file.state), ['preprocessing', 'preprocessing'])
})

test('keeps the upload identity when status polling becomes unavailable', () => {
  const processing = createProcessingProgress({
    uploadId: 'upload-2', state: 'preprocessing', ready: false,
    files: [{ file_name: 'report.pdf', state: 'preprocessing' }],
  }, ['report.pdf'], 2_000)

  assert.deepEqual(createStatusUnavailableProgress(processing), {
    ...processing,
    phase: 'status-unavailable',
  })
})
