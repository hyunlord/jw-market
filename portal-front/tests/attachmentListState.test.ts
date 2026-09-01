import assert from 'node:assert/strict'
import test from 'node:test'

import { attachmentListView } from '../src/utils/attachmentListState.ts'

test('keeps a successful empty list distinct from a failed request', () => {
  assert.deepEqual(attachmentListView({ kind: 'ready', documents: [] }), {
    kind: 'empty', message: '첨부된 파일이 없습니다.', canRetry: false,
  })
  assert.deepEqual(attachmentListView({ kind: 'failed', error: new Error('timeout') }), {
    kind: 'failed', message: '문서 목록을 불러오지 못했습니다.', canRetry: true,
  })
})
