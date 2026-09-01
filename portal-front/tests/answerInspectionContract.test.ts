import assert from 'node:assert/strict'
import test from 'node:test'
import {
  inspectionDetailFromChatLogData,
  laneExecutionsFromChatLogData,
  parseInspectionDetail,
  parseLaneExecutions,
  type AnswerInspectionDetail,
} from '../src/utils/answerInspection.ts'

const exactPayload = {
  schema: 'r12.5.inspect.v1',
  question: '리바로젯 특허현황',
  expansion: {
    original_question: '리바로젯 특허현황',
    expanded_queries: ['리바로젯 조성물 특허'],
  },
  calls: [{
    sequence: 7,
    source_label: '식품의약품안전처 의약품 특허목록',
    status: '완료',
    elapsed_seconds: 28.118,
    request_parameters: {
      query: '리바로젯 조성물 특허',
      calls: [
        { item_name: '리바로젯', limit: '500' },
        { ingr_name: 'Ezetimibe AND Pitavastatin' },
      ],
    },
    counts: {
      returned: 280,
      parsed: 280,
      envelope: 280,
      rendered: 4,
      narrated: 4,
    },
    unused_count: 276,
    dropped_count: 0,
    output: {
      document_count: 4,
      evidence_ids: ['MFDS-001', 'MFDS-002'],
    },
    drop_reasons: [{
      stage: 'render',
      count: 276,
      reason: '현재 답변 표면에 배치되지 않음',
      record_ids: ['MFDS-277', 'MFDS-278'],
    }],
  }],
}

test('accepts the exact r12.5 inspection contract without changing backend values', () => {
  const parsed = parseInspectionDetail(exactPayload)

  assert.deepEqual(parsed, exactPayload satisfies AnswerInspectionDetail)
  assert.equal(parsed?.calls[0]?.status, '완료')
  assert.equal(parsed?.calls[0]?.counts.returned, 280)
  assert.equal(parsed?.calls[0]?.unused_count, 276)
  assert.deepEqual(parsed?.calls[0]?.output, {
    document_count: 4,
    evidence_ids: ['MFDS-001', 'MFDS-002'],
  })
  assert.deepEqual(parsed?.calls[0]?.drop_reasons[0]?.record_ids, ['MFDS-277', 'MFDS-278'])
})

test('distinguishes a missing count from an explicit zero', () => {
  const parsed = parseInspectionDetail({
    ...exactPayload,
    calls: [{
      ...exactPayload.calls[0],
      counts: { returned: 0, parsed: 0, rendered: 0, narrated: 0 },
    }],
  })

  assert.equal(parsed?.calls[0]?.counts.returned, 0)
  assert.equal(parsed?.calls[0]?.counts.envelope, undefined)
})

test('preserves absent call output and absent drop record ids as undefined', () => {
  const parsed = parseInspectionDetail({
    ...exactPayload,
    calls: [{
      ...exactPayload.calls[0],
      output: undefined,
      drop_reasons: [{
        stage: 'render',
        count: 276,
        reason: '현재 답변 표면에 배치되지 않음',
      }],
    }],
  })

  assert.equal(parsed?.calls[0]?.output, undefined)
  assert.equal(parsed?.calls[0]?.drop_reasons[0]?.record_ids, undefined)
})

test('rejects unknown schemas and malformed calls instead of rendering invented detail', () => {
  assert.equal(parseInspectionDetail({ ...exactPayload, schema: 'r12.5.inspect.v2' }), undefined)
  assert.equal(parseInspectionDetail({ ...exactPayload, calls: [{ sequence: '7' }] }), undefined)
  assert.equal(parseInspectionDetail(null), undefined)
})

test('restores exact inspection detail from the persisted GenOS chat-log projection', () => {
  const detail = inspectionDetailFromChatLogData({
    text: '저장된 답변',
    genos_persist: {
      chat_agent_answer: {
        trace: {
          inspection_detail: exactPayload,
        },
      },
    },
  })

  assert.deepEqual(detail, exactPayload satisfies AnswerInspectionDetail)
})

test('does not invent inspection detail when the persisted trace is absent', () => {
  assert.equal(inspectionDetailFromChatLogData({ text: '이전 형식 답변' }), undefined)
  assert.equal(inspectionDetailFromChatLogData(null), undefined)
})

test('preserves file lane execution status without inventing absent lanes', () => {
  const executions = parseLaneExecutions({
    document_sql: { source: 'document_sql', planned: true, state: 'executed_success', reason_code: null },
    malformed: { source: 'document_rag', planned: 'yes', state: 'unplanned', reason_code: 'not_planned' },
  })

  assert.deepEqual(executions, {
    document_sql: { source: 'document_sql', planned: true, state: 'executed_success', reason_code: null },
  })
})

test('restores lane execution status from the persisted GenOS trace', () => {
  const laneExecution = {
    document_rag: { source: 'document_rag', planned: false, state: 'unplanned', reason_code: 'not_planned' },
  }
  const restored = laneExecutionsFromChatLogData({
    genos_persist: { chat_agent_answer: { trace: { lane_execution: laneExecution } } },
  })

  assert.deepEqual(restored, laneExecution)
  assert.deepEqual(laneExecutionsFromChatLogData(null), {})
})
