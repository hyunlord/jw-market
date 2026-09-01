import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import { consumeMarketStream, marketStreamTerminationNotice } from '../src/utils/marketStream.ts'

const encoder = new TextEncoder()

function streamResponse(frames: string[]): Response {
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const frame of frames) controller.enqueue(encoder.encode(frame))
      controller.close()
    },
  })
  return new Response(body, { headers: { 'content-type': 'text/event-stream' } })
}

test('charts SSE preserves the exact R12.6 root-array contract', async () => {
  const answers: string[] = []
  const chartEvents: unknown[] = []
  const charts = [{
    chart_id: 'v4-chart-1',
    chart_type: 'line',
    title: '매출 추이',
    x: ['2026-04', '2026-05'],
    series: [{
      label: '매출',
      values: [1, 2],
      record_ids: ['mart:2026-04', 'mart:2026-05'],
    }],
    unit: '억원',
    source_label: '내부 데이터마트',
  }]
  const result = await consumeMarketStream(
    streamResponse([
      'event: step\ndata: {"name":"시장 조회"}\n\n',
      'event: delta\ndata: 답변\n\n',
      `event: charts\ndata: ${JSON.stringify(charts)}\n\n`,
      'event: done\ndata: ok\n\n',
    ]),
    {
      onAnswer: answer => answers.push(answer),
      onCharts: charts => chartEvents.push(charts),
    },
  )

  assert.deepEqual(answers, ['답변'])
  assert.equal(result.steps.length, 1)
  assert.equal(result.done, true)
  assert.deepEqual(result.charts, charts)
  assert.deepEqual(chartEvents, [charts])
})

test('five rendered steps survive a timeout error followed by done', async () => {
  const stepEvents: unknown[] = []
  const frames = Array.from({ length: 5 }, (_, index) => (
    `event: step\ndata: ${JSON.stringify({ name: `단계 ${index + 1}` })}\n\n`
  ))
  const result = await consumeMarketStream(
    streamResponse([
      ...frames,
      'event: markdown_block\ndata: {"markdown":"\\n\\n> 응답이 시간 안에 끝나지 않아 결과를 표시하지 못했습니다."}\n\n',
      'event: error\ndata: {"code":"BFF_SSE_STREAM_TIMEOUT","message":"internal detail"}\n\n',
      'event: done\ndata: {"status":"terminated"}\n\n',
    ]),
    { onAnswer: () => undefined, onSteps: steps => stepEvents.push(steps) },
  )

  assert.equal(result.steps.length, 5)
  assert.equal(stepEvents.length, 5)
  assert.equal(result.errorCode, 'BFF_SSE_STREAM_TIMEOUT')
  assert.equal(result.hadBodyBeforeTerminalNotice, false)
  assert.equal(result.done, true)
  assert.equal(marketStreamTerminationNotice(result), '응답이 시간 제한(300초)을 넘어 중단됐습니다. 결과를 받지 못했습니다.')
})

test('partial body and steps survive a stream error with a public partial-result notice', async () => {
  const result = await consumeMarketStream(
    streamResponse([
      'event: step\ndata: {"name":"환자수 조회"}\n\n',
      'event: markdown_block\ndata: {"markdown":"## 확인된 결과\\n\\n일부 본문"}\n\n',
      'event: markdown_block\ndata: {"markdown":"\\n\\n> 답변이 한 번에 전달할 수 있는 크기를 넘어 표시하지 못했습니다."}\n\n',
      'event: error\ndata: {"code":"BFF_SSE_FRAME_LIMIT"}\n\n',
      'event: done\ndata: ok\n\n',
    ]),
    { onAnswer: () => undefined },
  )

  assert.match(result.text, /^## 확인된 결과\n\n일부 본문/)
  assert.equal(result.steps.length, 1)
  assert.equal(result.hadBodyBeforeTerminalNotice, true)
  assert.equal(marketStreamTerminationNotice(result), '응답 데이터가 표시 한도를 넘어 중단됐습니다. 지금까지 받은 내용만 표시합니다.')
})

test('done without a body is distinct from a normal completed answer', async () => {
  const empty = await consumeMarketStream(
    streamResponse(['event: done\ndata: ok\n\n']),
    { onAnswer: () => undefined },
  )
  const normal = await consumeMarketStream(
    streamResponse(['event: delta\ndata: 정상 답변\n\nevent: done\ndata: ok\n\n']),
    { onAnswer: () => undefined },
  )

  assert.equal(marketStreamTerminationNotice(empty), '응답은 완료됐지만 결과를 받지 못했습니다.')
  assert.equal(marketStreamTerminationNotice(normal), undefined)
})

test('live R60 charts normalize the backend axis object without losing labels or records', async () => {
  // Given: the exact chart frame captured from L-1 on backend revision 1390.
  const liveCharts: unknown = JSON.parse(readFileSync(
    new URL('./fixtures/r60-live/l1-charts.json', import.meta.url),
    'utf8',
  ))

  // When: the portal consumes the frame through its real SSE boundary.
  const result = await consumeMarketStream(
    streamResponse([
      `event: charts\ndata: ${JSON.stringify(liveCharts)}\n\n`,
      'event: done\ndata: ok\n\n',
    ]),
    { onAnswer: () => undefined },
  )

  // Then: both charts remain renderable and every 40-point record binding survives.
  assert.equal(result.charts.length, 2)
  assert.deepEqual(result.charts.map(chart => chart.x.length), [40, 40])
  assert.deepEqual(result.charts.map(chart => chart.x_label), ['기간', '기간'])
  assert.deepEqual(result.charts.map(chart => chart.series[0]?.record_ids.length), [40, 40])
  assert.equal(result.chartError, undefined)
})

test('one malformed chart does not remove a valid chart from the same frame', async () => {
  // Given: one valid chart and one chart with inconsistent dimensions.
  const frame = [{
    chart_id: 'valid', chart_type: 'line', title: '유효 차트',
    x: { label: '기간', values: ['2026-01', '2026-02'] },
    series: [{ label: '매출', values: [1, 2], record_ids: ['r1', 'r2'] }],
    unit: '억원', source_label: '내부 데이터마트', unknown_field: '보존 경계 밖 필드',
  }, {
    chart_id: 'invalid', chart_type: 'line', title: '오류 차트',
    x: { label: '기간', values: ['2026-01', '2026-02'] },
    series: [{ label: '매출', values: [1], record_ids: ['r1'] }],
    unit: '억원', source_label: '내부 데이터마트',
  }]

  // When: both arrive in one backend frame.
  const result = await consumeMarketStream(
    streamResponse([
      `event: charts\ndata: ${JSON.stringify(frame)}\n\n`,
      'event: done\ndata: ok\n\n',
    ]),
    { onAnswer: () => undefined },
  )

  // Then: the valid chart renders and the rejected count is disclosed without internal fields.
  assert.deepEqual(result.charts.map(chart => chart.chart_id), ['valid'])
  assert.equal(result.chartError, '차트 1개를 표시하지 못했습니다(데이터 형식 불일치).')
  assert.doesNotMatch(result.chartError, /unknown_field|series|record_ids/)
})

test('delta and markdown block content preserve the exact SSE arrival order', async () => {
  // Given: the L-1 monthly table block arrives before narrative delta text.
  const monthlyBlock: unknown = JSON.parse(readFileSync(
    new URL('./fixtures/r60-live/l1-monthly-table.json', import.meta.url),
    'utf8',
  ))

  // When: the mixed text events are consumed.
  const result = await consumeMarketStream(
    streamResponse([
      `event: markdown_block\ndata: ${JSON.stringify(monthlyBlock)}\n\n`,
      'event: delta\ndata: ## 종합 인사이트\n\n후속 해설\n\n',
      'event: done\ndata: ok\n\n',
    ]),
    { onAnswer: () => undefined },
  )

  // Then: the monthly answer remains ahead of the later narrative instead of moving to the bottom.
  assert.ok(result.text.indexOf('## 핵심 답') < result.text.indexOf('## 종합 인사이트'))
  assert.match(result.text, /2026-06 \| 85\.87억원/)
})

test('malformed chart dimensions are absent instead of being truncated', async () => {
  const errors: string[] = []
  const result = await consumeMarketStream(
    streamResponse([
      'event: charts\ndata: [{"chart_id":"bad","chart_type":"line","title":"bad","x":["A","B"],"series":[{"label":"값","values":[1],"record_ids":["r1"]}],"unit":null,"source_label":"mart"}]\n\n',
      'event: done\ndata: ok\n\n',
    ]),
    { onAnswer: () => undefined, onChartError: error => errors.push(error) },
  )

  assert.deepEqual(result.charts, [])
  assert.equal(result.chartError, '차트를 표시하지 못했습니다(데이터 형식 불일치).')
  assert.deepEqual(errors, ['차트를 표시하지 못했습니다(데이터 형식 불일치).'])
})

test('trace SSE exposes the exact inspection_detail side channel', async () => {
  const inspectionDetail = {
    schema: 'r12.5.inspect.v1',
    question: '리바로젯 특허현황',
    expansion: { expanded_queries: ['리바로젯 조성물 특허'] },
    calls: [{
      sequence: 7,
      source_label: '식품의약품안전처 의약품 특허목록',
      status: '완료',
      elapsed_seconds: 28.118,
      request_parameters: { query: '리바로젯 조성물 특허' },
      counts: { returned: 280, parsed: 280, envelope: 280, rendered: 4, narrated: 4 },
      unused_count: 276,
      dropped_count: 0,
      drop_reasons: [{ stage: 'render', count: 276, reason: '현재 답변 표면에 배치되지 않음' }],
    }],
  }
  const result = await consumeMarketStream(
    streamResponse([
      'event: delta\ndata: 답변\n\n',
      `event: trace\ndata: ${JSON.stringify({ trace_id: 'trace-1', inspection_detail: inspectionDetail })}\n\n`,
      'event: done\ndata: ok\n\n',
    ]),
    { onAnswer: () => undefined },
  )

  assert.deepEqual(result.inspectionDetail, inspectionDetail)
  assert.equal(result.traceId, 'trace-1')
})

test('trace SSE preserves the lazy detail contract for live evidence expansion', async () => {
  const detailContract = {
    schema: 'jw.detail-on-demand.v1',
    response_id: 'trace-1',
    items: [
      { item_key: 'inspection:0', kind: 'inspection', source: 'ClinicalTrials.gov' },
      { item_key: 'ct:NCT0001', kind: 'evidence', identifier: 'NCT0001' },
    ],
  }
  const result = await consumeMarketStream(
    streamResponse([
      `event: trace\ndata: ${JSON.stringify({ trace_id: 'trace-1', detail_on_demand: detailContract })}\n\n`,
      'event: done\ndata: ok\n\n',
    ]),
    { onAnswer: () => undefined },
  )

  assert.equal(result.detailContract?.responseId, 'trace-1')
  assert.deepEqual(result.detailContract?.items.map(item => item.itemKey), ['inspection:0', 'ct:NCT0001'])
})

test('trace SSE exposes the exact file lane execution side channel', async () => {
  const laneExecution = {
    document_sql: { source: 'document_sql', planned: true, state: 'executed_success', reason_code: null },
    document_rag: { source: 'document_rag', planned: false, state: 'unplanned', reason_code: 'not_planned' },
  }
  const result = await consumeMarketStream(
    streamResponse([
      `event: trace\ndata: ${JSON.stringify({ lane_execution: laneExecution })}\n\n`,
      'event: done\ndata: ok\n\n',
    ]),
    { onAnswer: () => undefined },
  )

  assert.deepEqual(result.laneExecutions, laneExecution)
})

test('trace SSE preserves tool results and backend selection flags for the portal', async () => {
  const toolResults = [{
    source: 'hira', query: 'E10 환자수', status: 'success', elapsed_ms: 1200,
    payload: { calls: [{ render_data: { message: '2건' } }] },
  }]
  const result = await consumeMarketStream(
    streamResponse([
      `event: trace\ndata: ${JSON.stringify({
        tool_results: toolResults,
        selection_rule: 'leading_records_in_upstream_order',
        selection_is_ranked: false,
      })}\n\n`,
      'event: done\ndata: ok\n\n',
    ]),
    { onAnswer: () => undefined },
  )

  assert.deepEqual(result.toolResults, toolResults)
  assert.deepEqual(result.selectionPolicy, {
    rule: 'leading_records_in_upstream_order', ranked: false,
  })
})

test('trace SSE preserves the complete unnarrated record ledger for inspection', async () => {
  const records = [
    { record_id: 'ct:NCT05705804', reason_code: 'public_identifier_missing_from_final_prose' },
    { record_id: 'nedrug:1:1:2', reason_code: 'public_identifier_missing_from_final_prose' },
  ]
  const result = await consumeMarketStream(
    streamResponse([
      `event: trace\ndata: ${JSON.stringify({ lossless_spine: { unnarrated_record_count: 2, unnarrated_records: records } })}\n\n`,
      'event: done\ndata: ok\n\n',
    ]),
    { onAnswer: () => undefined },
  )

  assert.deepEqual(result.unnarratedRecords, records)
})

test('tables SSE exposes the exact root array once without an invented wrapper', async () => {
  const tables = [{
    table_id: 'v4-2741d71603c9ca78',
    title: '단일 임상시험 상세',
    source_label: 'ClinicalTrials.gov',
    columns: [{ key: 'column_1', label: '시험', type: 'string', unit: null, align: 'left' }],
    rows: [{ cells: { column_1: 'NCT05151731' }, record_id: 'ct:NCT05151731' }],
    row_count: 1,
    omitted_columns: [],
  }]
  const tableEvents: unknown[] = []
  const result = await consumeMarketStream(
    streamResponse([
      'event: delta\ndata: 답변\n\n',
      `event: tables\ndata: ${JSON.stringify(tables)}\n\n`,
      'event: done\ndata: ok\n\n',
    ]),
    {
      onAnswer: () => undefined,
      onTables: received => tableEvents.push(received),
    },
  )

  assert.deepEqual(result.tables, tables)
  assert.deepEqual(tableEvents, [tables])
})

test('malformed tables SSE is absent instead of becoming an empty table', async () => {
  const errors: string[] = []
  const result = await consumeMarketStream(
    streamResponse([
      'event: tables\ndata: [{"table_id":"bad","row_count":2,"rows":[]}]\n\n',
      'event: done\ndata: ok\n\n',
    ]),
    { onAnswer: () => undefined, onTableError: error => errors.push(error) },
  )

  assert.deepEqual(result.tables, [])
  assert.equal(result.tableError, '표 데이터를 표시할 수 없습니다.')
  assert.deepEqual(errors, ['표 데이터를 표시할 수 없습니다.'])
})

test('invalid inspection detail is absent rather than rendered as empty data', async () => {
  const result = await consumeMarketStream(
    streamResponse([
      'event: trace\ndata: {"inspection_detail":{"schema":"unknown","calls":[]}}\n\n',
      'event: done\ndata: ok\n\n',
    ]),
    { onAnswer: () => undefined },
  )

  assert.equal(result.inspectionDetail, undefined)
})

test('delta frames preserve an URL split across arbitrary stream chunks', async () => {
  const result = await consumeMarketStream(
    streamResponse([
      'event: delta\ndata: [HIRA](https://www.hira.or.kr/rc/insu/\n\n',
      'event: delta\ndata: insuadtcrtr/InsuAdtCrtrDetail.do?mtgHmeDd=2024%EB%85%84)\n\n',
      'event: done\ndata: ok\n\n',
    ]),
    { onAnswer: () => undefined },
  )

  assert.equal(
    result.text,
    '[HIRA](https://www.hira.or.kr/rc/insu/insuadtcrtr/InsuAdtCrtrDetail.do?mtgHmeDd=2024%EB%85%84)',
  )
})

test('chart component uses React Chart.js without markup injection', () => {
  const source = readFileSync(
    new URL('../src/components/main/MarketCharts.tsx', import.meta.url),
    'utf8',
  )

  assert.match(source, /from 'react-chartjs-2'/)
  assert.match(source, /className="market-chart"/)
  assert.match(source, /data-chart-id=\{chart\.chart_id\}/)
  assert.match(source, /chart\.source_label/)
  assert.doesNotMatch(source, /chart\.labels|chart\.datasets|chart\.type/)
  assert.doesNotMatch(source, /dangerouslySetInnerHTML|innerHTML|createElement\(['"](?:svg|script)/)
})
