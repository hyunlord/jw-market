import assert from 'node:assert/strict'
import test from 'node:test'

import { consumeMarketStream, type AnswerSectionState } from '../src/utils/marketStream.ts'
import { applyAnswerSectionDelta, parsePersistedAnswerSections } from '../src/utils/answerSections.ts'

const encoder = new TextEncoder()

function streamResponse(frames: readonly string[]): Response {
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const frame of frames) controller.enqueue(encoder.encode(frame))
      controller.close()
    },
  })
  return new Response(body, { headers: { 'content-type': 'text/event-stream' } })
}

function tableFrame(tableId: string, value: string): string {
  return `event: tables\ndata: [{"table_id":"${tableId}","title":"${tableId}","source_label":"내부 데이터마트","columns":[{"key":"value","label":"값","type":"string","unit":null,"align":"left"}],"rows":[{"record_id":"${tableId}-row","cells":{"value":"${value}"}}],"row_count":1,"omitted_columns":[]}]\n\n`
}

test('section metadata fixes display order while deltas arrive out of order', async () => {
  const snapshots: AnswerSectionState[][] = []
  const tableEvents: unknown[] = []
  const result = await consumeMarketStream(
    streamResponse([
      'event: answer_sections\ndata: {"schema":"jw.answer-sections.v1","sections":[{"id":"insight","order":0,"kind":"insight","status":"pending"},{"id":"facts","order":1,"kind":"facts","title":"조사 결과","status":"pending"}],"evidence_catalog":{"mart:call:1":{"evidence_id":"mart:call:1","source_name":"내부 데이터마트","identifier":"리바로젯","query":"리바로젯 추이","counts":{"received":12,"direct_related":12},"record":{"기간":"2026-07","매출":"91.53억원"}}}}\n\n',
      'event: answer_section_delta\ndata: {"schema":"jw.answer-section-delta.v1","section_id":"facts","delta":"하단 먼저","status":"complete"}\n\n',
      'event: tables\ndata: [{"table_id":"t1","title":"즉시 표","source_label":"내부 데이터마트","columns":[{"key":"value","label":"값","type":"string","unit":null,"align":"left"}],"rows":[{"record_id":"row-1","cells":{"value":"표 값"}}],"row_count":1,"omitted_columns":[]}]\n\n',
      'event: answer_section_delta\ndata: {"schema":"jw.answer-section-delta.v1","section_id":"insight","delta":"상단 나중","evidence":[{"evidence_id":"mart:call:1","label":"출처"}],"status":"complete"}\n\n',
      'event: done\ndata: ok\n\n',
    ]),
    {
      onAnswer: () => undefined,
      onSections: sections => snapshots.push(sections),
      onTables: tables => tableEvents.push(tables),
    },
  )

  assert.deepEqual(result.sections?.map(section => section.id), ['insight', 'facts'])
  assert.deepEqual(result.sections?.map(section => section.status), ['complete', 'complete'])
  assert.equal(result.sections?.[0]?.parts[0]?.type, 'text')
  assert.equal(result.sections?.[0]?.evidenceCatalog?.['mart:call:1']?.identifier, '리바로젯')
  assert.equal(result.sections?.[1]?.parts[0]?.type, 'text')
  assert.equal(snapshots[1]?.[0]?.status, 'pending')
  assert.equal(snapshots[1]?.[1]?.status, 'complete')
  assert.equal(tableEvents.length, 1)
})

test('incremental table events retain earlier tables and replace matching ids in place', async () => {
  const snapshots: string[][] = []
  const result = await consumeMarketStream(
    streamResponse([
      tableFrame('market', 'first'),
      tableFrame('clinical', 'second'),
      tableFrame('market', 'updated'),
      'event: done\ndata: ok\n\n',
    ]),
    { onAnswer: () => undefined, onTables: tables => snapshots.push(tables.map(table => table.table_id)) },
  )

  assert.deepEqual(result.tables.map(table => table.table_id), ['market', 'clinical'])
  assert.equal(result.tables[0]?.rows[0]?.cells.value, 'updated')
  assert.deepEqual(snapshots, [['market'], ['market', 'clinical'], ['market', 'clinical']])
})

test('done resolves immediately without waiting for the server to close the stream', async () => {
  let cancelled = false
  const response = new Response(new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(encoder.encode('event: done\ndata: ok\n\n'))
    },
    cancel() { cancelled = true },
  }))

  const result = await Promise.race([
    consumeMarketStream(response, { onAnswer: () => undefined }),
    new Promise<never>((_, reject) => setTimeout(() => reject(new Error('stream did not resolve after done')), 100)),
  ])

  assert.equal(result.done, true)
  assert.equal(cancelled, true)
})

test('completed sections use the bounded idle fallback when done is missing', async () => {
  const warnings: unknown[][] = []
  const originalWarn = console.warn
  console.warn = (...args: unknown[]) => warnings.push(args)
  try {
    const response = new Response(new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoder.encode(
          'event: answer_sections\ndata: {"schema":"jw.answer-sections.v1","sections":[{"id":"insight","order":0,"kind":"insight","status":"pending"}]}\n\n'
          + 'event: answer_section_delta\ndata: {"schema":"jw.answer-section-delta.v1","section_id":"insight","delta":"완료 본문","status":"complete"}\n\n',
        ))
      },
    }))
    const result = await consumeMarketStream(
      response,
      { onAnswer: () => undefined },
      { completedSectionIdleMs: 5 },
    )

    assert.equal(result.done, true)
    assert.equal(result.completionFallback, 'section_idle')
    assert.match(String(warnings[0]?.[0]), /completed sections/i)
  } finally {
    console.warn = originalWarn
  }
})

test('three section metadata keeps answer first regardless of completion order', async () => {
  const snapshots: AnswerSectionState[][] = []
  const result = await consumeMarketStream(
    streamResponse([
      'event: answer_sections\ndata: {"schema":"jw.answer-sections.v1","sections":[{"id":"answer","order":0,"kind":"answer","status":"pending"},{"id":"insight","order":1,"kind":"insight","status":"pending"},{"id":"facts","order":2,"kind":"facts","title":"조사 결과","status":"pending"}]}\n\n',
      'event: answer_section_delta\ndata: {"schema":"jw.answer-section-delta.v1","section_id":"facts","delta":"하단 먼저","status":"complete"}\n\n',
      'event: answer_section_delta\ndata: {"schema":"jw.answer-section-delta.v1","section_id":"answer","delta":"직접 답변","status":"complete"}\n\n',
      'event: answer_section_delta\ndata: {"schema":"jw.answer-section-delta.v1","section_id":"insight","delta":"확장 인사이트","status":"complete"}\n\n',
      'event: done\ndata: ok\n\n',
    ]),
    { onAnswer: () => undefined, onSections: sections => snapshots.push(sections) },
  )

  assert.deepEqual(result.sections?.map(section => section.id), ['answer', 'insight', 'facts'])
  assert.deepEqual(result.sections?.map(section => section.status), ['complete', 'complete', 'complete'])
  assert.deepEqual(snapshots[1]?.map(section => section.status), ['pending', 'pending', 'complete'])
})

test('legacy streams never activate section rendering', async () => {
  let sectionEvents = 0
  const result = await consumeMarketStream(
    streamResponse([
      'event: answer_section_delta\ndata: {"schema":"jw.answer-section-delta.v1","section_id":"facts","delta":"메타 없는 조각","status":"complete"}\n\n',
      'event: delta\ndata: ## 핵심 답\n\n',
      'event: delta\ndata: 기존 본문\n\n',
      'event: done\ndata: ok\n\n',
    ]),
    { onAnswer: () => undefined, onSections: () => { sectionEvents += 1 } },
  )

  assert.equal(result.sections, undefined)
  assert.equal(sectionEvents, 0)
  assert.equal(result.text, '## 핵심 답기존 본문')
})

test('section content is retained when a terminal notice arrives without legacy text', async () => {
  const result = await consumeMarketStream(
    streamResponse([
      'event: answer_sections\ndata: {"schema":"jw.answer-sections.v1","sections":[{"id":"insight","order":0,"kind":"insight","status":"pending"}]}\n\n',
      'event: answer_section_delta\ndata: {"schema":"jw.answer-section-delta.v1","section_id":"insight","delta":"먼저 받은 인사이트","status":"complete"}\n\n',
      'event: markdown_block\ndata: {"markdown":"응답이 시간 안에 끝나지 않아 중단됐습니다."}\n\n',
      'event: done\ndata: ok\n\n',
    ]),
    { onAnswer: () => undefined },
  )

  assert.equal(result.hadBodyBeforeTerminalNotice, true)
})

test('persisted section paragraphs restore live parts and self-contained evidence', () => {
  const sections = parsePersistedAnswerSections({
    schema: 'jw.answer-sections.v1',
    sections: [
      { id: 'insight', order: 0, kind: 'insight', status: 'pending' },
      { id: 'facts', order: 1, kind: 'facts', title: '조사 결과', status: 'pending' },
    ],
    paragraphs: {
      insight: [
        {
          text: '첫 해석입니다.',
          paragraph_start: true,
          evidence: [{ evidence_id: 'mart:row:1', label: '출처: 내부 데이터마트' }],
        },
        {
          text: '같은 문단의 다음 문장입니다.',
          paragraph_start: false,
          evidence: [],
        },
        {
          text: '다음 문단입니다.',
          paragraph_start: true,
          evidence: [],
        },
      ],
      facts: [{ text: '직접 답변입니다.', paragraph_start: true, evidence: [] }],
    },
    evidence_catalog: {
      'mart:row:1': {
        evidence_id: 'mart:row:1', source_name: '내부 데이터마트', identifier: '리바로젯', query: '리바로젯 추이',
        counts: { received: 12, direct_related: 12 }, record: { '기간': '2026-07', '매출': '91.53억원' },
      },
    },
  })

  assert.deepEqual(sections?.map(section => [section.id, section.status]), [['insight', 'complete'], ['facts', 'complete']])
  assert.equal(sections?.[0]?.parts.map(part => part.type === 'text' ? part.text : `[${part.label}]`).join(''), '첫 해석입니다.[출처: 내부 데이터마트] 같은 문단의 다음 문장입니다.\n\n다음 문단입니다.')
  assert.equal(sections?.[0]?.evidenceCatalog?.['mart:row:1']?.record['매출'], '91.53억원')
})

test('persisted three section answers restore in contract order', () => {
  const sections = parsePersistedAnswerSections({
    schema: 'jw.answer-sections.v1',
    sections: [
      { id: 'facts', order: 2, kind: 'facts', title: '조사 결과', status: 'pending' },
      { id: 'answer', order: 0, kind: 'answer', status: 'pending' },
      { id: 'insight', order: 1, kind: 'insight', status: 'pending' },
    ],
    paragraphs: {
      answer: [{ text: '직접 답변', paragraph_start: true, evidence: [] }],
      insight: [{ text: '확장 인사이트', paragraph_start: true, evidence: [] }],
      facts: [{ text: '조사 결과', paragraph_start: true, evidence: [] }],
    },
  })

  assert.deepEqual(sections?.map(section => section.id), ['answer', 'insight', 'facts'])
})

test('compound evidence groups survive both live deltas and persisted restore as one marker', () => {
  const group = {
    schema: 'jw.evidence-group.v1',
    group_id: 'eg-insight-1',
    primary: { evidence_id: 'mart:row:1', label: '출처: 내부 데이터마트', source_key: 'mart', source_label: '내부 데이터마트' },
    members: [
      { evidence_id: 'mart:row:1', label: '출처: 내부 데이터마트', source_key: 'mart', source_label: '내부 데이터마트' },
      { evidence_id: 'ct:NCT1', label: '출처: ClinicalTrials.gov NCT1', source_key: 'ct', source_label: 'ClinicalTrials.gov' },
    ],
    source_breakdown: [
      { source_key: 'mart', source_label: '내부 데이터마트', count: 1 },
      { source_key: 'ct', source_label: 'ClinicalTrials.gov', count: 1 },
    ],
  }
  const metadata = [{ id: 'insight', order: 0, kind: 'insight' as const, status: 'pending' as const, parts: [] }]
  const live = applyAnswerSectionDelta(metadata, {
    schema: 'jw.answer-section-delta.v1', section_id: 'insight', delta: '복합 근거 문장. ',
    evidence: [group.primary], evidence_group: group, status: 'complete',
  })
  const restored = parsePersistedAnswerSections({
    schema: 'jw.answer-sections.v1',
    sections: [{ id: 'insight', order: 0, kind: 'insight', status: 'pending' }],
    paragraphs: { insight: [{ text: '복합 근거 문장. ', paragraph_start: true, evidence: [group.primary], evidence_group: group }] },
  })

  for (const sections of [live, restored]) {
    const evidenceParts = sections?.[0]?.parts.filter(part => part.type === 'evidence') ?? []
    assert.equal(evidenceParts.length, 1)
    assert.equal(evidenceParts[0]?.label, '출처: 내부 데이터마트 + ClinicalTrials.gov')
    assert.equal(evidenceParts[0]?.group?.groupId, 'eg-insight-1')
    assert.deepEqual(evidenceParts[0]?.group?.members.map(member => member.evidenceId), ['mart:row:1', 'ct:NCT1'])
  }
})

test('persisted section parser rejects malformed rich state for legacy fallback', () => {
  assert.equal(parsePersistedAnswerSections({ schema: 'jw.answer-sections.v1', sections: [], paragraphs: {} }), undefined)
  assert.equal(parsePersistedAnswerSections({
    schema: 'jw.answer-sections.v1',
    sections: [{ id: 'insight', order: 0, kind: 'insight', status: 'pending' }],
    paragraphs: { insight: [{ text: '근거', paragraph_start: true, evidence: [{ evidence_id: '', label: '출처' }] }] },
  }), undefined)
})
