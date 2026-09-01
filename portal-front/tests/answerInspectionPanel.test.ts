import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test, { after } from 'node:test'
import { createElement, type ComponentType } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import react from '@vitejs/plugin-react'
import { createServer } from 'vite'
import {
  parseInspectionDetail,
  parseLaneExecutions,
  type AnswerInspectionDetail,
  type LaneExecutionMap,
} from '../src/utils/answerInspection.ts'
import {
  parseTraceToolResults,
  type TraceToolResult,
  type UnnarratedRecord,
} from '../src/utils/traceToolResults.ts'

interface AnswerInspectionPanelProps {
  open: boolean
  answerLabel: string
  detail?: AnswerInspectionDetail
  laneExecutions?: LaneExecutionMap
  toolResults?: readonly TraceToolResult[]
  unnarratedRecords?: readonly UnnarratedRecord[]
  initiallyExpandedSequences?: readonly number[]
  focusLaneKey?: string
  focusRequestId?: number
  onClose: () => void
}

interface TracePayloadViewProps {
  source: string
  payload: TraceToolResult['payload']
}

const vite = await createServer({
  configFile: false,
  root: new URL('..', import.meta.url).pathname,
  plugins: [react()],
  optimizeDeps: { noDiscovery: true },
  server: { middlewareMode: true, hmr: { port: 24681 } },
  appType: 'custom',
})
const { default: AnswerInspectionPanel } = await vite.ssrLoadModule(
  '/src/components/main/AnswerInspectionPanel.tsx',
) as { default: ComponentType<AnswerInspectionPanelProps> }
const { default: TracePayloadView } = await vite.ssrLoadModule(
  '/src/components/main/TracePayloadView.tsx',
) as { default: ComponentType<TracePayloadViewProps> }

after(async () => vite.close())

async function realFixture(name: string): Promise<string> {
  return readFile(new URL(`./fixtures/chat-answer-collapse/${name}`, import.meta.url), 'utf8')
}

async function liveInspectionFixture(name: string): Promise<{
  readonly detail: AnswerInspectionDetail
  readonly toolResults: readonly TraceToolResult[]
}> {
  const text = await readFile(new URL(`./fixtures/r51-live/${name}`, import.meta.url), 'utf8')
  const raw: unknown = JSON.parse(text)
  if (raw === null || typeof raw !== 'object' || Array.isArray(raw)) throw new TypeError('R51 fixture root is invalid')
  const detail = parseInspectionDetail(Reflect.get(raw, 'detail'))
  const toolResults = parseTraceToolResults(Reflect.get(raw, 'tool_results'))
  if (detail === undefined) throw new TypeError('R51 inspection detail is invalid')
  return { detail, toolResults }
}

async function currentInspectionFixture(name: string): Promise<AnswerInspectionDetail> {
  const text = await readFile(new URL(`./fixtures/r52a-live/${name}`, import.meta.url), 'utf8')
  const detail = parseInspectionDetail(JSON.parse(text))
  if (detail === undefined) throw new TypeError('R52-A inspection detail is invalid')
  return detail
}

async function fileToolFixture(name: string): Promise<{
  readonly detail: AnswerInspectionDetail
  readonly laneExecutions: LaneExecutionMap
}> {
  const text = await readFile(new URL(`./fixtures/file-tool-detail/${name}`, import.meta.url), 'utf8')
  const raw: unknown = JSON.parse(text)
  if (raw === null || typeof raw !== 'object' || Array.isArray(raw)) throw new TypeError('file tool fixture root is invalid')
  const detail = parseInspectionDetail(Reflect.get(raw, 'inspection_detail'))
  if (detail === undefined) throw new TypeError('file tool inspection detail is invalid')
  return {
    detail,
    laneExecutions: parseLaneExecutions(Reflect.get(raw, 'lane_execution')),
  }
}

test('lane groups expose one stable jump target per source lane', async () => {
  const fixture = await liveInspectionFixture('e1-clinical.json')
  const markup = renderToStaticMarkup(createElement(AnswerInspectionPanel, {
    open: true,
    answerLabel: 'lane jump',
    detail: fixture.detail,
    toolResults: fixture.toolResults,
    focusLaneKey: 'clinicaltrials',
    onClose: () => undefined,
  }))

  assert.match(markup, /data-inspection-lane="clinicaltrials"/)
  assert.equal((markup.match(/data-inspection-lane="clinicaltrials"/g) ?? []).length, 1)
})

test('a real completed answer opens an inspection panel with an explicit missing-field state', async () => {
  const answer = await realFixture('rivaroxaban-clinical.md')
  assert.ok(answer.includes('## 핵심 답'))

  const markup = renderToStaticMarkup(createElement(AnswerInspectionPanel, {
    open: true,
    answerLabel: '리바로젯 제네릭 임상현황',
    onClose: () => undefined,
  }))

  assert.match(markup, /aside/)
  assert.match(markup, /조회 상세/)
  assert.match(markup, /조회 상세가 제공되지 않았습니다/)
  assert.doesNotMatch(markup, />0건</)
  assert.match(markup, /aria-label="조회 상세 닫기"/)
})

test('the missing-field state is deterministic for a short real answer', async () => {
  const answer = await realFixture('nct05151731-design.md')
  assert.ok(answer.length > 0)

  const render = () => renderToStaticMarkup(createElement(AnswerInspectionPanel, {
    open: true,
    answerLabel: 'NCT05151731 시험 디자인',
    onClose: () => undefined,
  }))

  assert.equal(render(), render())
})

test('a closed panel renders no drawer content', () => {
  const markup = renderToStaticMarkup(createElement(AnswerInspectionPanel, {
    open: false,
    answerLabel: '짧은 답변',
    onClose: () => undefined,
  }))

  assert.equal(markup, '')
})

test('renders the exact inspection detail with backend counts and unused-call evidence', () => {
  const detail: AnswerInspectionDetail = {
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
        calls: [{ item_name: '리바로젯', limit: '500' }],
      },
      counts: { returned: 280, parsed: 280, envelope: 280, rendered: 4, narrated: 4 },
      unused_count: 276,
      dropped_count: 0,
      output: {
        rendered_documents: [
          { record_id: 'MFDS-001', title: '리바로젯정' },
        ],
      },
      drop_reasons: [{
        stage: 'render',
        count: 276,
        reason: '현재 답변 표면에 배치되지 않음',
        record_ids: ['MFDS-277', 'MFDS-278'],
      }],
    }],
  }

  const markup = renderToStaticMarkup(createElement(AnswerInspectionPanel, {
    open: true,
    answerLabel: detail.question,
    detail,
    initiallyExpandedSequences: [7],
    onClose: () => undefined,
  }))

  assert.match(markup, /질문 확장/)
  assert.match(markup, /리바로젯 조성물 특허/)
  assert.match(markup, /식품의약품안전처 의약품 특허목록/)
  assert.match(markup, /성공/)
  assert.match(markup, /28\.118초/)
  assert.match(markup, /수신 건수/)
  assert.match(markup, />280</)
  assert.match(markup, /답변에 쓰이지 않은 276건/)
  assert.match(markup, />INPUT</)
  assert.match(markup, /보낸 것/)
  assert.match(markup, />OUTPUT</)
  assert.match(markup, /받은 것/)
  assert.match(markup, /검색어/)
  assert.match(markup, /호출 목록/)
  assert.match(markup, /MFDS-001/)
  assert.match(markup, /현재 답변 표면에 배치되지 않음/)
  assert.match(markup, /<details class="[^"]*answer-inspection-record-ids[^"]*">/)
  assert.match(markup, /제외 항목 식별자/)
  assert.match(markup, /MFDS-277/)
  assert.doesNotMatch(markup, /<details class="[^"]*answer-inspection-output[^"]*" open="">/)
  assert.doesNotMatch(markup, /<details class="[^"]*answer-inspection-record-ids[^"]*" open="">/)
  assert.doesNotMatch(markup, /조회 상세가 제공되지 않았습니다/)
})

test('distinguishes an unpreserved output from an empty result', () => {
  const detail: AnswerInspectionDetail = {
    schema: 'r12.5.inspect.v1',
    question: '선택 필드 누락 확인',
    expansion: null,
    calls: [{
      sequence: 2,
      source_label: '내부 데이터마트',
      status: '완료',
      elapsed_seconds: 0.5,
      request_parameters: { query: '리바로젯' },
      counts: { returned: 1, parsed: 1, rendered: 1, narrated: 1 },
      unused_count: 0,
      dropped_count: 0,
      drop_reasons: [{ stage: 'render', count: 0, reason: '폐기 없음' }],
    }],
  }

  const markup = renderToStaticMarkup(createElement(AnswerInspectionPanel, {
    open: true,
    answerLabel: detail.question,
    detail,
    initiallyExpandedSequences: [2],
    onClose: () => undefined,
  }))

  assert.match(markup, /answer-inspection-output/)
  assert.match(markup, /원문 미보존/)
  assert.doesNotMatch(markup, /answer-inspection-unpreserved">없음/)
  assert.doesNotMatch(markup, /answer-inspection-record-ids/)
  assert.doesNotMatch(markup, /\{\}/)
  assert.match(markup, /폐기 없음/)
})

test('shows a missing stage as unavailable rather than zero', () => {
  const detail: AnswerInspectionDetail = {
    schema: 'r12.5.inspect.v1',
    question: '단계 누락 확인',
    expansion: null,
    calls: [{
      sequence: 1,
      source_label: '내부 데이터마트',
      status: '성공+0건',
      elapsed_seconds: 0.25,
      request_parameters: { query: '없는 브랜드' },
      counts: { returned: 0, parsed: 0, rendered: 0, narrated: 0 },
      unused_count: 0,
      dropped_count: 0,
      drop_reasons: [],
    }],
  }

  const markup = renderToStaticMarkup(createElement(AnswerInspectionPanel, {
    open: true,
    answerLabel: detail.question,
    detail,
    initiallyExpandedSequences: [1],
    onClose: () => undefined,
  }))

  assert.match(markup, /답변 구성/)
  assert.match(markup, /제공되지 않음/)
  assert.match(markup, /0건/)
  assert.match(markup, /data-status="empty"/)
})

test('renders deterministic collapsed source cards with four user-facing status kinds', () => {
  const statuses = [
    ['완료', 2, 'success', '성공'],
    ['성공+0건', 0, 'empty', '0건'],
    ['실패', 0, 'failure', '실패'],
    ['쿼터 소진', 0, 'quota', '쿼터 소진'],
  ] as const
  const detail: AnswerInspectionDetail = {
    schema: 'r12.5.inspect.v1',
    question: '상태 표시',
    expansion: null,
    calls: statuses.map(([status, returned], index) => ({
      sequence: index + 1,
      source_label: index === 0 ? 'aux:patent:news' : `소스 ${index + 1}`,
      status,
      elapsed_seconds: index + 0.25,
      request_parameters: { query: '상태 확인' },
      counts: { returned, parsed: returned, rendered: returned, narrated: returned },
      unused_count: 0,
      dropped_count: 0,
      drop_reasons: [],
    })),
  }

  const markup = renderToStaticMarkup(createElement(AnswerInspectionPanel, {
    open: true,
    answerLabel: detail.question,
    detail,
    onClose: () => undefined,
  }))

  assert.match(markup, /특허 뉴스/)
  assert.doesNotMatch(markup, /aux:/)
  assert.match(markup, />전체 펼치기</)
  for (const [, , kind, label] of statuses) {
    assert.match(markup, new RegExp(`data-status="${kind}"[^>]*>${label}<`))
  }
  assert.equal((markup.match(/class="answer-inspection-call-body"[^>]*hidden=""/g) ?? []).length, 4)
})

test('keeps whitelisted trace output in the DOM while its matched card is collapsed', () => {
  const detail: AnswerInspectionDetail = {
    schema: 'r12.5.inspect.v1',
    question: '당뇨병 환자수 알려줘',
    expansion: null,
    calls: [{
      sequence: 3,
      source_label: '건강보험심사평가원',
      status: '완료',
      elapsed_seconds: 1.2,
      request_parameters: { query: 'E10 환자수' },
      counts: { returned: 2, parsed: 2, rendered: 1, narrated: 1 },
      unused_count: 1,
      dropped_count: 0,
      drop_reasons: [],
    }],
  }
  const toolResults: TraceToolResult[] = [{
    source: 'hira',
    query: 'E10 환자수',
    status: 'success',
    elapsed_ms: 1200,
    payload: { calls: [{ render_data: { message: '실제 반환 2건' } }] },
  }]

  const collapsed = renderToStaticMarkup(createElement(AnswerInspectionPanel, {
    open: true, answerLabel: detail.question, detail, toolResults, onClose: () => undefined,
  }))
  const expanded = renderToStaticMarkup(createElement(AnswerInspectionPanel, {
    open: true, answerLabel: detail.question, detail, toolResults, initiallyExpandedSequences: [3], onClose: () => undefined,
  }))

  assert.match(collapsed, /class="answer-inspection-call-body"[^>]*hidden=""/)
  assert.match(collapsed, /실제 반환 2건/)
  assert.match(expanded, /실제 반환 2건/)
  assert.match(expanded, /실행 trace/)
  assert.match(expanded, /조회 상세/)
})

test('marks duplicated rows as unmatched instead of arbitrarily pairing trace output', () => {
  const call = {
    sequence: 1,
    source_label: '내부 데이터마트',
    status: '완료',
    elapsed_seconds: 1,
    request_parameters: { query: '같은 질의' },
    counts: { returned: 1 },
    unused_count: 0,
    dropped_count: 0,
    drop_reasons: [],
  }
  const detail: AnswerInspectionDetail = {
    schema: 'r12.5.inspect.v1', question: '중복', expansion: null,
    calls: [call, { ...call, sequence: 2 }],
  }
  const toolResults: TraceToolResult[] = [
    { source: 'mart', query: '같은 질의', status: 'success', elapsed_ms: 1000, payload: { calls: [] } },
    { source: 'mart', query: '같은 질의', status: 'success', elapsed_ms: 1000, payload: { calls: [] } },
  ]
  const markup = renderToStaticMarkup(createElement(AnswerInspectionPanel, {
    open: true, answerLabel: '중복', detail, toolResults, initiallyExpandedSequences: [1, 2], onClose: () => undefined,
  }))

  assert.equal((markup.match(/실행 trace 대응 불가/g) ?? []).length, 2)
})

test('renders the real HIRA patient counts, separates input fields, and groups five calls into one lane card', async () => {
  // Given: the complete E-2 payload captured from backend revision 1381.
  const fixture = await liveInspectionFixture('e2-hira.json')

  // When: every HIRA call is expanded in the inspection panel.
  const markup = renderToStaticMarkup(createElement(AnswerInspectionPanel, {
    open: true,
    answerLabel: fixture.detail.question,
    detail: fixture.detail,
    toolResults: fixture.toolResults,
    initiallyExpandedSequences: [3, 9, 14, 16, 18],
    onClose: () => undefined,
  }))

  // Then: supplied values and correlation are visible without internal transport labels.
  assert.match(markup, /data-lane-call-count="5"/)
  assert.equal((markup.match(/data-lane-call-count="5"/g) ?? []).length, 1)
  assert.match(markup, /호출 5회/)
  assert.match(markup, /총 반환 15건/)
  assert.match(markup, /질의어/)
  assert.match(markup, /호출 파라미터/)
  assert.match(markup, />55228</)
  assert.match(markup, />3017</)
  assert.match(markup, /대응 18\/18/)
  assert.doesNotMatch(markup, /hira_disease_hospitalization_outpatient_stats/)
  assert.doesNotMatch(markup, /mcp-hira-standby-svc/)
  assert.doesNotMatch(markup, /safe_url/)
})

test('keeps five real HIRA calls as collapsed rows inside one collapsed lane', async () => {
  const fixture = await liveInspectionFixture('e2-hira.json')
  const markup = renderToStaticMarkup(createElement(AnswerInspectionPanel, {
    open: true,
    answerLabel: fixture.detail.question,
    detail: fixture.detail,
    toolResults: fixture.toolResults,
    onClose: () => undefined,
  }))

  assert.equal((markup.match(/data-lane-call-count="5"/g) ?? []).length, 1)
  const groupedCallCount = fixture.detail.calls.filter(call => (
    fixture.detail.calls.filter(peer => peer.source_label === call.source_label).length > 1
  )).length
  assert.equal((markup.match(/class="answer-inspection-call is-lane-row/g) ?? []).length, groupedCallCount)
  assert.doesNotMatch(markup, /answer-inspection-call is-nested/)
  assert.match(markup, /class="answer-inspection-lane-body" hidden=""/)
  assert.equal((markup.match(/class="answer-inspection-call-body"[^>]*hidden=""/g) ?? []).length, fixture.detail.calls.length)
  assert.match(markup, />55228</)
  assert.match(markup, />3017</)
})

test('shows backend duplicate and displayed counts only when they differ from the returned count', () => {
  const baseCall = {
    sequence: 1,
    source_label: '건강보험심사평가원',
    status: '완료',
    elapsed_seconds: 1,
    request_parameters: { query: 'E10 환자수' },
    counts: { returned: 7, parsed: 7, rendered: 6, narrated: 6 },
    unused_count: 1,
    dropped_count: 0,
    drop_reasons: [],
  }
  const detail: AnswerInspectionDetail = {
    schema: 'r12.5.inspect.v1', question: '출력 집계', expansion: null,
    calls: [
      { ...baseCall, output: { displayed_record_count: 6, duplicate_records_collapsed: 3 } },
      { ...baseCall, sequence: 2, output: { displayed_record_count: 7, duplicate_records_collapsed: 0 } },
    ],
  }
  const markup = renderToStaticMarkup(createElement(AnswerInspectionPanel, {
    open: true,
    answerLabel: detail.question,
    detail,
    initiallyExpandedSequences: [1, 2],
    onClose: () => undefined,
  }))

  assert.equal((markup.match(/동일 항목 3건/g) ?? []).length, 1)
  assert.equal((markup.match(/표시 6건 · 반환 7건/g) ?? []).length, 1)
  assert.doesNotMatch(markup, /동일 항목 0건/)
  assert.doesNotMatch(markup, /표시 7건 · 반환 7건/)
})

test('shows current live duplicate and displayed counts from the R52-A payload', async () => {
  const detail = await currentInspectionFixture('e2-inspection.json')
  const changedSequences = detail.calls
    .filter(call => call.output !== undefined && JSON.stringify(call.output).includes('"duplicate_records_collapsed":1'))
    .map(call => call.sequence)
  const markup = renderToStaticMarkup(createElement(AnswerInspectionPanel, {
    open: true,
    answerLabel: detail.question,
    detail,
    initiallyExpandedSequences: changedSequences,
    onClose: () => undefined,
  }))

  assert.deepEqual(changedSequences, [7, 13])
  assert.equal((markup.match(/동일 항목 1건/g) ?? []).length, 2)
  assert.equal((markup.match(/표시 6건 · 반환 7건/g) ?? []).length, 2)
})

test('renders the live Q9E document SQL contract as visible execution detail', async () => {
  const fixture = await fileToolFixture('q9e-document-sql.json')
  const sqlCall = fixture.detail.calls.find(call => call.tool === 'document_sql')
  assert.ok(sqlCall)

  const markup = renderToStaticMarkup(createElement(AnswerInspectionPanel, {
    open: true,
    answerLabel: fixture.detail.question,
    detail: fixture.detail,
    laneExecutions: fixture.laneExecutions,
    initiallyExpandedSequences: [sqlCall.sequence],
    onClose: () => undefined,
  }))

  assert.match(markup, /실행 SQL 원문/)
  assert.match(markup, /SELECT SUM\(c72\) AS total_value, COUNT\(\*\) AS applied_rows/)
  assert.match(markup, /CHSO_KOR_SellOut_Basic_Feb-19-2026/)
  assert.match(markup, /Sell Out {2}Standard/)
  assert.match(markup, /doc_6724_sell_out_standard/)
  assert.match(markup, /결과 행 표본/)
  assert.match(markup, /193,466,912,759/)
  assert.match(markup, /12,267/)
  assert.match(markup, /planned/)
  assert.match(markup, /executed_success/)
  assert.match(markup, /복사/)
})

test('renders the live Q9P PDF chunks with page, excerpt, similarity, and selection state', async () => {
  const fixture = await fileToolFixture('q9p-document-rag.json')
  const ragCall = fixture.detail.calls.find(call => call.tool === 'document_rag')
  assert.ok(ragCall)

  const markup = renderToStaticMarkup(createElement(AnswerInspectionPanel, {
    open: true,
    answerLabel: fixture.detail.question,
    detail: fixture.detail,
    laneExecutions: fixture.laneExecutions,
    initiallyExpandedSequences: [ragCall.sequence],
    onClose: () => undefined,
  }))

  assert.match(markup, /검색 질의문/)
  assert.match(markup, /업로드한 당뇨 Fact sheet 2024 PDF 내용을 요약해줘/)
  assert.equal((markup.match(/당뇨 Fact sheet 2024\.pdf/g) ?? []).length >= 4, true)
  assert.match(markup, /<dt>페이지<\/dt><dd>5<\/dd>/)
  assert.match(markup, /자료원 2/)
  assert.match(markup, /본문 발췌/)
  assert.match(markup, /최근 11년간 당뇨병 유병률/)
  assert.match(markup, /유사도 점수/)
  assert.match(markup, /이 항목은 원천에서 제공되지 않았습니다/)
  assert.match(markup, /답변 사용/)
  assert.match(markup, /data-selected="true"/)
})

test('renders the post-235 live vector distance with its direction and keeps missing full text explicit', async () => {
  const fixture = await fileToolFixture('q9p-document-rag-scored.json')
  const ragCall = fixture.detail.calls.find(call => call.tool === 'document_rag')
  assert.ok(ragCall)

  const markup = renderToStaticMarkup(createElement(AnswerInspectionPanel, {
    open: true,
    answerLabel: fixture.detail.question,
    detail: fixture.detail,
    laneExecutions: fixture.laneExecutions,
    initiallyExpandedSequences: [ragCall.sequence],
    onClose: () => undefined,
  }))

  assert.match(markup, /distance 0\.29566467 \(낮을수록 유사\)/)
  assert.match(markup, /전체 내용/)
  assert.match(markup, /이 항목은 원천에서 제공되지 않았습니다/)
  assert.doesNotMatch(markup, />전체 보기</)
})

test('offers the supplied chunk text up to 2400 characters behind a collapsed full-view control', () => {
  const fullText = `미리보기-${'가'.repeat(320)}-중간-${'나'.repeat(2200)}-잘림표식`
  const detail: AnswerInspectionDetail = {
    schema: 'r12.5.inspect.v1',
    question: '전체 청크 보기',
    expansion: null,
    calls: [{
      sequence: 1,
      tool: 'document_rag',
      source_label: '업로드 문서(문서 검색)',
      status: '성공',
      elapsed_seconds: 0.1,
      request_parameters: { query: '전체 청크 보기' },
      counts: { returned: 1, parsed: 1, rendered: 1, narrated: 1 },
      unused_count: 0,
      dropped_count: 0,
      output: { chunks: [{
        document_name: '긴 청크.pdf', page: 3, selected: true,
        distance: 0.18, content_excerpt: fullText,
      }] },
      drop_reasons: [],
    }],
  }

  const markup = renderToStaticMarkup(createElement(AnswerInspectionPanel, {
    open: true,
    answerLabel: detail.question,
    detail,
    initiallyExpandedSequences: [1],
    onClose: () => undefined,
  }))

  assert.match(markup, /<summary>전체 보기<\/summary>/)
  assert.match(markup, /미리보기-/)
  assert.ok(markup.includes('나'.repeat(2000)))
  assert.doesNotMatch(markup, /잘림표식/)
  assert.doesNotMatch(markup, /<details class="document-chunk-full" open/)
})

test('keeps twenty chunks, selection labels, and a bounded scroll container', async () => {
  const chunks = Array.from({ length: 20 }, (_, index) => ({
    document_name: `문서-${index + 1}.pdf`,
    record_id: `DOC-${index + 1}`,
    page: index + 1,
    selected: index % 3 === 0,
    score_kind: index % 2 === 0 ? 'vector' : 'bm25',
    ...(index % 2 === 0 ? { distance: 0.1 + index / 100 } : { score: 10 + index }),
    content_excerpt: `청크 ${index + 1} 본문`,
  }))
  const detail: AnswerInspectionDetail = {
    schema: 'r12.5.inspect.v1',
    question: '20개 청크',
    expansion: null,
    calls: [{
      sequence: 1,
      tool: 'document_rag',
      source_label: '업로드 문서(문서 검색)',
      status: '성공',
      elapsed_seconds: 0.1,
      request_parameters: { query: '20개 청크' },
      counts: { returned: 20, parsed: 20, rendered: 7, narrated: 7 },
      unused_count: 13,
      dropped_count: 0,
      output: { chunks },
      drop_reasons: [],
    }],
  }

  const markup = renderToStaticMarkup(createElement(AnswerInspectionPanel, {
    open: true,
    answerLabel: detail.question,
    detail,
    initiallyExpandedSequences: [1],
    onClose: () => undefined,
  }))

  assert.equal((markup.match(/<li data-selected=/g) ?? []).length, 20)
  assert.match(markup, /문서-20\.pdf/)
  assert.match(markup, /score 11 \(높을수록 유사\)/)
  assert.match(markup, /답변 사용/)
  assert.match(markup, /답변 미사용/)
  assert.match(markup, /document-chunk-list/)
  const css = await readFile(new URL('../src/styles/common.css', import.meta.url), 'utf8')
  assert.match(css, /\.document-chunk-list\s*\{[^}]*max-height:\s*min\(68vh,\s*760px\)[^}]*overflow-y:\s*auto/)
})

test('renders the real reimbursement notice fields without fabricating an inspection anchor link', async () => {
  // Given: the complete E-8 payload captured from backend revision 1381.
  const fixture = await liveInspectionFixture('e8-hira-reimbursement.json')

  // When: the matching HIRA reimbursement call is expanded.
  const markup = renderToStaticMarkup(createElement(AnswerInspectionPanel, {
    open: true,
    answerLabel: fixture.detail.question,
    detail: fixture.detail,
    toolResults: fixture.toolResults,
    initiallyExpandedSequences: [3],
    onClose: () => undefined,
  }))

  // Then: every supplied business field is visible while the unsupported body-to-record jump remains absent.
  assert.match(markup, /제2021-245호/)
  assert.match(markup, /20211001-5-0001/)
  assert.match(markup, /2021-10-01/)
  assert.match(markup, /보험인정기준 상세내용/)
  assert.match(markup, /품명 &#x27;리바로젯정&#x27; 기준/)
  assert.doesNotMatch(markup, /href="#insp-/)
  assert.doesNotMatch(markup, /id="insp-/)
})

test('labels an absent backend output as not supplied instead of an empty value', () => {
  // Given: a completed call whose backend contract omitted output.
  const detail: AnswerInspectionDetail = {
    schema: 'r12.5.inspect.v1',
    question: '출력 미제공',
    expansion: null,
    calls: [{
      sequence: 1,
      source_label: '내부 데이터마트',
      status: '완료',
      elapsed_seconds: 1,
      request_parameters: { query: '출력 미제공' },
      counts: { returned: 1 },
      unused_count: 0,
      dropped_count: 0,
      drop_reasons: [],
    }],
  }

  // When: the call is expanded without a trace result.
  const markup = renderToStaticMarkup(createElement(AnswerInspectionPanel, {
    open: true,
    answerLabel: detail.question,
    detail,
    initiallyExpandedSequences: [1],
    onClose: () => undefined,
  }))

  // Then: omission is explicit and never represented as an empty output.
  assert.match(markup, /백엔드 미제공/)
  assert.doesNotMatch(markup, /answer-inspection-unpreserved">없음/)
})

test('renders more than 10 MB of live-derived inspection payload without dropping supplied call records', async () => {
  const names = ['e1-clinical.json', 'e2-hira.json', 'e8-hira-reimbursement.json'] as const
  const fixtures = await Promise.all(names.map(name => liveInspectionFixture(name)))
  const bytesPerPass = (await Promise.all(names.map(name => readFile(
    new URL(`./fixtures/r51-live/${name}`, import.meta.url),
  )))).reduce((sum, value) => sum + value.byteLength, 0)
  const startedAt = performance.now()
  let renderedCallCount = 0
  for (let attempt = 0; attempt < 2; attempt += 1) {
    for (const fixture of fixtures) {
      const markup = renderToStaticMarkup(createElement(AnswerInspectionPanel, {
        open: true,
        answerLabel: 'R51 live payload performance probe',
        detail: fixture.detail,
        toolResults: fixture.toolResults,
        initiallyExpandedSequences: fixture.detail.calls.map(call => call.sequence),
        onClose: () => undefined,
      }))
      renderedCallCount += (markup.match(/class="answer-inspection-call(?: |")/g) ?? []).length
    }
  }
  const elapsedMs = performance.now() - startedAt
  const totalBytes = bytesPerPass * 2

  console.log(JSON.stringify({ probe: 'R51_10MB_RENDER', totalBytes, elapsedMs, renderedCallCount }))
  assert.ok(totalBytes >= 10_000_000)
  assert.equal(renderedCallCount, fixtures.reduce((sum, fixture) => sum + fixture.detail.calls.length, 0) * 2)
})

test('inspection payload uses a stair-step branch and wraps values without horizontal overflow', async () => {
  // Given: the production inspection stylesheet used for both compact and 30-field payloads.
  const css = await readFile(new URL('../src/styles/common.css', import.meta.url), 'utf8')

  // When: the responsive inspection rules are inspected at the wide-screen breakpoint.
  const wideRule = css.slice(css.indexOf('@media (min-width: 1280px)'))

  // Then: INPUT/OUTPUT stay stacked while scalar rows and nested branches wrap without a table-like label track.
  assert.doesNotMatch(wideRule, /\.answer-inspection-io\s*\{[^}]*repeat\(2/)
  assert.match(css, /\.trace-output-scalar\s*\{[^}]*display:\s*flex/)
  assert.match(css, /\.trace-output-branch\s*\{[^}]*border-left:\s*2px/)
  assert.doesNotMatch(css, /\.trace-output-object\s*>\s*div\s*\{[^}]*grid-template-columns/)
  assert.doesNotMatch(css, /\.trace-output-object\s*\{[^}]*min-width:\s*420px/)
  assert.match(css, /\.trace-output-scalar \.trace-output-field-value\s*\{[^}]*overflow-wrap:\s*anywhere/)
  assert.match(css, /\.trace-output-branch\s*>\s*\.trace-output-field-label\s*\{[^}]*overflow-wrap:\s*anywhere/)
  assert.match(css, /\.answer-inspection-panel\s*\{[^}]*clamp\(360px,\s*30vw,\s*460px\)/)
  assert.match(css, /\.answer-inspection-counts\s*\{[^}]*repeat\(2/)
})

test('keeps long trace values in the DOM behind an explicit expander and copy command', () => {
  const longValue = 'FDA label 원문 '.repeat(30)
  const detail: AnswerInspectionDetail = {
    schema: 'r12.5.inspect.v1', question: '긴 원문', expansion: null,
    calls: [{
      sequence: 1, source_label: 'FDA', status: '완료', elapsed_seconds: 1,
      request_parameters: { query: '긴 원문' }, counts: { returned: 1 },
      unused_count: 0, dropped_count: 0, drop_reasons: [],
    }],
  }
  const markup = renderToStaticMarkup(createElement(AnswerInspectionPanel, {
    open: true, answerLabel: detail.question, detail,
    toolResults: [{ sequence: 1, source: 'openfda', query: '긴 원문', status: 'complete', payload: { message: longValue } }],
    initiallyExpandedSequences: [1], onClose: () => undefined,
  }))

  assert.match(markup, /긴 값 펼치기/)
  assert.match(markup, /복사/)
  assert.match(markup, new RegExp(longValue.slice(0, 40)))
  assert.match(markup, /data-full-value-length=/)
})

test('renders all 18 MFDS patent fields with public labels and keeps all 500 records in backend order', async () => {
  const raw = await readFile(
    new URL('./fixtures/r66-patent/preserved_mfds_patent_500_items.json', import.meta.url),
    'utf8',
  )
  const records = JSON.parse(raw) as Record<string, string>[]
  const heapBefore = process.memoryUsage().heapUsed
  const startedAt = performance.now()
  const markup = renderToStaticMarkup(createElement(TracePayloadView, {
    source: 'patent',
    payload: { items: records },
  }))
  const elapsedMs = performance.now() - startedAt
  const heapDeltaBytes = Math.max(0, process.memoryUsage().heapUsed - heapBefore)
  const domNodeCount = (markup.match(/<[a-z][^>]*>/g) ?? []).length

  console.log(JSON.stringify({
    probe: 'R66_PATENT_500X18_RENDER',
    fixtureBytes: Buffer.byteLength(raw),
    recordCount: records.length,
    fieldCount: Object.keys(records[0] ?? {}).length,
    elapsedMs,
    domNodeCount,
    heapDeltaBytes,
    markupBytes: Buffer.byteLength(markup),
  }))

  assert.equal(records.length, 500)
  assert.equal(Object.keys(records[0] ?? {}).length, 18)
  assert.equal((markup.match(/class="trace-record-block"/g) ?? []).length, 500)
  assert.equal((markup.match(/data-record-identifier=/g) ?? []).length, 500)
  assert.match(markup, /총 500건/)
  assert.match(markup, /식별자 목록/)
  assert.doesNotMatch(markup, /<details class="trace-record-block"[^>]* open=/)
  assert.equal((markup.match(/특허 목록 구분/g) ?? []).length, 500)
  assert.match(markup, /제품특허/)
  assert.match(markup, /기타특허/)
  assert.match(markup, /특허 구분/)
  assert.match(markup, /국내 특허 상태/)
  assert.match(markup, /긴 값 펼치기/)
  assert.ok(markup.indexOf('리바로정2밀리그램') < markup.indexOf('리바로정4밀리그램'))
  assert.doesNotMatch(markup, /더 보기 \(25\/500\)/)
})

test('renders a trace-backed entry point for every unnarrated record without inventing rows', () => {
  const records: UnnarratedRecord[] = [
    { record_id: 'ct:NCT05705804', reason_code: 'public_identifier_missing_from_final_prose' },
    { record_id: 'nedrug:1:1:2', reason_code: 'public_identifier_missing_from_final_prose' },
  ]
  const detail: AnswerInspectionDetail = {
    schema: 'r12.5.inspect.v1', question: '미반영 목록', expansion: null,
    calls: [{
      sequence: 1, source_label: 'ClinicalTrials.gov', status: '완료', elapsed_seconds: 1,
      request_parameters: { query: '미반영 목록' }, counts: { returned: 2, narrated: 0 },
      unused_count: 2, dropped_count: 0, drop_reasons: [],
    }],
  }
  const markup = renderToStaticMarkup(createElement(AnswerInspectionPanel, {
    open: true, answerLabel: detail.question, detail, unnarratedRecords: records,
    initiallyExpandedSequences: [1], onClose: () => undefined,
  }))

  assert.match(markup, /미반영 2건 보기/)
  assert.match(markup, /ct:NCT05705804/)
  assert.match(markup, /nedrug:1:1:2/)
})

test('exposes resize and fullscreen controls while preserving the inspection summary', () => {
  const markup = renderToStaticMarkup(createElement(AnswerInspectionPanel, {
    open: true, answerLabel: '패널 조절', onClose: () => undefined,
  }))

  assert.match(markup, /role="separator"/)
  assert.match(markup, /aria-label="조회 상세 너비 조절"/)
  assert.match(markup, /aria-label="조회 상세 전체 화면"/)
  assert.match(markup, /조회 상세가 제공되지 않았습니다/)
})
