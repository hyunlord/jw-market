import assert from 'node:assert/strict'
import test from 'node:test'

import type { AnswerInspectionDetail } from '../src/utils/answerInspection.ts'
import { displayParameterLabel, PATENT_FIELD_ORDER } from '../src/utils/portalDisplayLabels.ts'
import {
  parseUnnarratedRecords,
  matchInspectionCallsToTrace,
  parseSelectionPolicy,
  parseTraceToolResults,
  projectTracePayload,
  selectionNotice,
  selectionPolicyFromChatLogData,
  traceSourceForInspectionLabel,
  traceToolResultsFromChatLogData,
  unnarratedRecordsFromChatLogData,
} from '../src/utils/traceToolResults.ts'

test('public source labels resolve to the same inspection lane keys as backend source labels', () => {
  assert.equal(traceSourceForInspectionLabel('HIRA'), 'hira')
  assert.equal(traceSourceForInspectionLabel('건강보험심사평가원'), 'hira')
  assert.equal(traceSourceForInspectionLabel('특허 자료'), 'patent')
  assert.equal(traceSourceForInspectionLabel('식품의약품안전처 의약품 특허목록'), 'patent')
  assert.equal(traceSourceForInspectionLabel('알 수 없는 출처'), undefined)
})

const inspection: AnswerInspectionDetail = {
  schema: 'r12.5.inspect.v1',
  question: '당뇨병 환자수 알려줘',
  expansion: null,
  calls: [{
    sequence: 3,
    source_label: '건강보험심사평가원',
    status: '완료',
    elapsed_seconds: 1.2,
    request_parameters: { query: 'E10 환자수' },
    counts: { returned: 2, parsed: 2, envelope: 2, rendered: 1, narrated: 1 },
    unused_count: 1,
    dropped_count: 0,
    drop_reasons: [],
  }],
}

test('parses trace tool results without inventing malformed entries', () => {
  const parsed = parseTraceToolResults([{
    source: 'hira',
    query: 'E10 환자수',
    status: 'success',
    elapsed_ms: 1200,
    payload: { calls: [{ render_data: { message: '2건' } }] },
  }, { source: 'hira', query: 10, payload: {} }])

  assert.equal(parsed.length, 1)
  assert.equal(parsed[0]?.source, 'hira')
  assert.deepEqual(parsed[0]?.payload, { calls: [{ render_data: { message: '2건' } }] })
})

test('matches only an exact and unique public lane plus query pair', () => {
  const trace = parseTraceToolResults([{
    source: 'hira', query: 'E10 환자수', status: 'success', elapsed_ms: 1200,
    payload: { calls: [{ render_data: { message: '2건' } }] },
  }])
  const matches = matchInspectionCallsToTrace(inspection.calls, trace)

  assert.equal(matches.get(3)?.kind, 'matched')
  assert.equal(matches.get(3)?.result?.query, 'E10 환자수')
})

test('does not pair duplicated inspection and trace rows when the backend supplies no correlation sequence', () => {
  const duplicateInspection = { ...inspection, calls: [inspection.calls[0]!, { ...inspection.calls[0]!, sequence: 8 }] }
  const trace = parseTraceToolResults([
    { source: 'hira', query: 'E10 환자수', status: 'success', elapsed_ms: 1200, payload: {} },
    { source: 'hira', query: 'E10 환자수', status: 'success', elapsed_ms: 1200, payload: {} },
  ])
  const matches = matchInspectionCallsToTrace(duplicateInspection.calls, trace)

  assert.equal(matches.get(3)?.kind, 'ambiguous')
  assert.equal(matches.get(8)?.kind, 'ambiguous')
})

test('pairs duplicated source and query rows by the backend supplied trace sequence', () => {
  const duplicateInspection = {
    ...inspection,
    calls: [
      { ...inspection.calls[0]!, sequence: 3, trace_sequence: 41 },
      { ...inspection.calls[0]!, sequence: 8, trace_sequence: 42 },
    ],
  }
  const trace = parseTraceToolResults([
    { sequence: 42, source: 'hira', query: 'E10 환자수', status: 'success', payload: { value: 'second' } },
    { sequence: 41, source: 'hira', query: 'E10 환자수', status: 'success', payload: { value: 'first' } },
  ])
  const matches = matchInspectionCallsToTrace(duplicateInspection.calls, trace)

  assert.equal(matches.get(3)?.kind, 'matched')
  assert.deepEqual(matches.get(3)?.result.payload, { value: 'first' })
  assert.equal(matches.get(8)?.kind, 'matched')
  assert.deepEqual(matches.get(8)?.result.payload, { value: 'second' })
})

test('projects only lane whitelist fields and reports hidden fields without values', () => {
  const projected = projectTracePayload('hira', {
    calls: [{
      source: 'internal_mcp_lane',
      tool: 'internal-hira-tool',
      safe_url: 'http://service.llmops.svc/private',
      render_data: {
        request: { sickCd: 'E10', secret: 'must-not-render' },
        payload: { rows: [{ sickCd: 'E10', patient_count: 123 }] },
        message: '조회 완료',
        mcp: { content_text: 'private body' },
      },
    }],
    private_sql: 'SELECT * FROM secret',
  })

  const serialized = JSON.stringify(projected.value)
  assert.match(serialized, /E10/)
  assert.match(serialized, /123/)
  assert.doesNotMatch(serialized, /internal_mcp_lane|internal-hira-tool|service\.llmops|private body|SELECT|must-not-render/)
  assert.ok(projected.hiddenFieldCount >= 4)
})

test('keeps supplied HIRA patient and notice fields while hiding internal adapter metadata', () => {
  const projected = projectTracePayload('hira', {
    calls: [{
      source: 'hira-internal',
      tool: 'hira_private_tool',
      safe_url: 'http://hira.llmops.svc/private',
      summary_text: 'hira_private_tool MCP returned totalCount=2',
      render_data: {
        items: [{ sickCd: 'E10', ptntCnt: 55228, inpatOpat: '외래' }],
        notice_number: '제2021-245호',
        source_notice_id: '20211001-5-0001',
        source_date: '2021-10-01',
      },
    }],
  })

  const serialized = JSON.stringify(projected.value)
  assert.match(serialized, /55228|제2021-245호|20211001-5-0001|2021-10-01/)
  assert.doesNotMatch(serialized, /hira-internal|hira_private_tool|llmops\.svc|summary_text|safe_url/)
})

test('projects every exact MFDS patent field while still hiding unknown and sensitive fields', () => {
  const patentRecord = {
    INGR_ENG_NAME: 'Pitavastatin Calcium',
    INGR_NAME: '피타바스타틴칼슘',
    ITEM_ENG_NAME: 'LIVALO',
    ITEM_NAME: '리바로정2밀리그램',
    ENTP_NAME: '제이더블유중외제약(주)',
    SHAPE: 'Pill',
    CONT_QY: '2.205mg/125.205mg',
    CLASS_NO: '218',
    PMS_END_DATE: '-',
    DOMESTIC_LWST_YN: '',
    ITEM_SEQ: '200500288',
    PAGE_GB_NM: '기타특허',
    PATENT_GB_CODE: '제법',
    DOMESTIC_INVN_NM: 'PROCESS FOR PRODUCING OPTICALLY ACTIVE MATERIAL',
    PATENTEE: '권리자',
    DOMESTIC_PATENT_NO: '10-0777553',
    DOMESTIC_PATENT_STATUS: '소멸',
    DOMESTIC_END_DATE: '2010-11-12',
    UNKNOWN_BACKEND_FIELD: 'must-not-render',
    source_url: 'http://service.llmops.svc/private',
    private_sql: 'SELECT * FROM secret',
  }

  const projected = projectTracePayload('patent', { items: [patentRecord] })
  const record = (projected.value as { items: Record<string, unknown>[] }).items[0]

  assert.deepEqual(Object.keys(record), Object.keys(patentRecord).slice(0, 18))
  assert.doesNotMatch(JSON.stringify(projected.value), /must-not-render|llmops\.svc|SELECT/)
  assert.equal(projected.hiddenFieldCount, 3)
})

test('maps every MFDS patent field to a stable public Korean label', () => {
  const labels = PATENT_FIELD_ORDER.map(displayParameterLabel)

  assert.equal(PATENT_FIELD_ORDER.length, 18)
  assert.equal(new Set(labels).size, 18)
  assert.ok(labels.every(label => !/^[A-Z][A-Z_]+$/.test(label)))
  assert.deepEqual(labels.slice(10, 14), ['품목 일련번호', '특허 목록 구분', '특허 구분', '발명의 명칭'])
})

test('reads ranked selection flags and distinguishes false, true, and missing', () => {
  const unranked = parseSelectionPolicy({ selection_rule: 'leading_records_in_upstream_order', selection_is_ranked: false })
  const ranked = parseSelectionPolicy({ selection_rule: 'score_desc', selection_is_ranked: true })

  assert.match(selectionNotice(unranked, 40) ?? '', /임의 40건/)
  assert.match(selectionNotice(ranked, 40) ?? '', /score_desc/)
  assert.match(selectionNotice(undefined, 40) ?? '', /정렬 플래그가 제공되지 않아/)
  assert.equal(selectionNotice(unranked, 39), undefined)
})

test('restores trace outputs and selection flags from persisted chat-log data', () => {
  const data = {
    genos_persist: {
      chat_agent_answer: {
        trace: {
          tool_results: [{ source: 'mart', query: '리바로 월별 매출', status: 'complete', payload: { rows: [] } }],
          selection_rule: 'leading_records_in_upstream_order',
          selection_is_ranked: false,
        },
      },
    },
  }

  assert.equal(traceToolResultsFromChatLogData(data).length, 1)
  assert.deepEqual(selectionPolicyFromChatLogData(data), {
    rule: 'leading_records_in_upstream_order',
    ranked: false,
  })
  assert.deepEqual(unnarratedRecordsFromChatLogData(data), [])
})
test('parses every trace-backed unnarrated record and rejects incomplete entries', () => {
  assert.deepEqual(parseUnnarratedRecords({
    unnarrated_records: [
      { record_id: 'ct:NCT05705804', reason_code: 'public_identifier_missing_from_final_prose' },
      { record_id: 'missing-reason' },
      { record_id: 'nedrug:1:1:2', reason_code: 'public_identifier_missing_from_final_prose' },
    ],
  }), [
    { record_id: 'ct:NCT05705804', reason_code: 'public_identifier_missing_from_final_prose' },
    { record_id: 'nedrug:1:1:2', reason_code: 'public_identifier_missing_from_final_prose' },
  ])
})

test('restores the complete unnarrated record ledger from persisted chat history', () => {
  const records = [
    { record_id: 'ct:NCT05705804', reason_code: 'public_identifier_missing_from_final_prose' },
    { record_id: 'mart:1:2:40', reason_code: 'public_identifier_missing_from_final_prose' },
  ]
  const data = { genos_persist: { chat_agent_answer: { trace: { lossless_spine: { unnarrated_records: records } } } } }
  assert.deepEqual(unnarratedRecordsFromChatLogData(data), records)
})
