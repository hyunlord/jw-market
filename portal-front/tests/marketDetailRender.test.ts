import assert from 'node:assert/strict'
import test, { after } from 'node:test'
import { createElement, type ComponentType } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import react from '@vitejs/plugin-react'
import { createServer } from 'vite'

import {
  createMarketDetailLookup,
  fetchMarketDetail,
  marketDetailContractFromChatLogData,
  parseMarketDetailContract,
  type MarketDetailResponse,
} from '../src/utils/marketDetail.ts'

interface MarketDetailViewProps {
  detail: MarketDetailResponse
}

const vite = await createServer({
  configFile: false,
  root: new URL('..', import.meta.url).pathname,
  plugins: [react()],
  optimizeDeps: { noDiscovery: true },
  server: { middlewareMode: true, hmr: { port: 24695 } },
  appType: 'custom',
})
const { MarketDetailView } = await vite.ssrLoadModule('/src/components/main/MarketDetailView.tsx') as {
  MarketDetailView: ComponentType<MarketDetailViewProps>
}

after(async () => vite.close())

const contractPayload = {
  schema: 'jw.detail-on-demand.v1',
  response_id: 'trace-1',
  items: [
    { item_key: 'inspection:0', kind: 'inspection', source: 'ClinicalTrials.gov', summary: { returned: 32 } },
    { item_key: 'ct:NCT0001', kind: 'evidence', identifier: 'NCT0001' },
  ],
  truncation: { silent: false, detail_fetch_required: true, notice: 'compact' },
}

test('restores stable lazy-detail keys without changing evidence identifiers', () => {
  const contract = marketDetailContractFromChatLogData({
    genos_persist: { chat_agent_answer: { trace: { detail_on_demand: contractPayload } } },
  })
  const lookup = createMarketDetailLookup(contract, 'conversation-1', 'trace-1')

  assert.deepEqual(parseMarketDetailContract(contractPayload)?.items.map(item => item.itemKey), ['inspection:0', 'ct:NCT0001'])
  assert.ok(lookup)
  assert.equal(lookup.itemKeys.has('ct:NCT0001'), true)
  assert.equal(createMarketDetailLookup(contract, 'conversation-1', 'wrong-trace'), undefined)
})

test('loads the exact item through the authenticated portal detail route', async () => {
  const contract = parseMarketDetailContract(contractPayload)
  const lookup = createMarketDetailLookup(contract, 'conversation 1', 'trace-1')
  assert.ok(lookup)
  let requestedUrl = ''
  const detail = await fetchMarketDetail(lookup, 'ct:NCT0001', async url => {
    requestedUrl = url
    return new Response(JSON.stringify({
      status: 'SUCCESS',
      result: { code: 0, data: {
        schema: 'jw.detail-on-demand.v1',
        conversation_id: 'conversation 1',
        response_id: 'trace-1',
        item_key: 'ct:NCT0001',
        kind: 'evidence',
        detail: { nct_id: 'NCT0001', title: 'A'.repeat(320) },
        input: { query: 'ferric carboxymaltose', request_parameters: { pageSize: 100 }, expansion_grade: 'notation' },
        output: { received_count: 32, directly_relevant_count: 32, summary: { status: 'COMPLETED' }, called_at: '2026-08-31T00:00:00Z', elapsed_ms: 1234 },
        field_metadata: {
          public_field_count: 32,
          hidden_field_count: 4,
          hidden_field_notice: '내부 필드는 값 없이 개수만 제공합니다.',
          missing_fields: { 'detail.phase': '원천 응답에 값이 없습니다.' },
          length_hints: { title: 320 },
        },
        partial: false,
      } },
    }), { status: 200, headers: { 'Content-Type': 'application/json' } })
  })

  assert.equal(requestedUrl, '/api/v1/market/chat/detail/conversation%201/trace-1?item_key=ct%3ANCT0001')
  assert.equal(detail.input?.query, 'ferric carboxymaltose')
  assert.equal(detail.output?.receivedCount, 32)
  assert.equal(detail.fieldMetadata.hiddenFieldCount, 4)
  assert.equal(detail.fieldMetadata.lengthHints.title, 320)
})

test('accepts the direct BFF result envelope used by the live detail route', async () => {
  const lookup = createMarketDetailLookup(parseMarketDetailContract(contractPayload), 'conversation-1', 'trace-1')
  assert.ok(lookup)
  const detail = await fetchMarketDetail(lookup, 'ct:NCT0001', async () => new Response(JSON.stringify({
    status: 'SUCCESS',
    result: {
      schema: 'jw.detail-on-demand.v1',
      conversation_id: 'conversation-1',
      response_id: 'trace-1',
      item_key: 'ct:NCT0001',
      kind: 'evidence',
      detail: { patent_number: '10-0101149', directly_related: null },
      input: { query: '리바로젯 특허현황' },
      output: { received_count: 274 },
      field_metadata: { public_field_count: 31, hidden_field_count: 0 },
      partial: false,
    },
  }), { status: 200, headers: { 'Content-Type': 'application/json' } }))

  assert.equal(detail.itemKey, 'ct:NCT0001')
  assert.equal(detail.output?.receivedCount, 274)
  assert.equal(detail.output?.directlyRelevantCount, undefined)
  assert.deepEqual(detail.detail, { patent_number: '10-0101149', directly_related: null })
})

test('rejects absent keys, HTTP failures, and malformed schemas explicitly', async () => {
  const lookup = createMarketDetailLookup(parseMarketDetailContract(contractPayload), 'conversation-1', 'trace-1')
  assert.ok(lookup)
  await assert.rejects(() => fetchMarketDetail(lookup, 'missing', async () => new Response('{}')), /요청한 상세 항목/)
  await assert.rejects(() => fetchMarketDetail(lookup, 'inspection:0', async () => new Response('{}', { status: 503 })), /HTTP 503/)
  await assert.rejects(() => fetchMarketDetail(lookup, 'inspection:0', async () => new Response('{}')), /응답 형식/)
})

test('distinguishes network, authorization, empty, and malformed detail failures', async () => {
  const lookup = createMarketDetailLookup(parseMarketDetailContract(contractPayload), 'conversation-1', 'trace-1')
  assert.ok(lookup)
  await assert.rejects(
    () => fetchMarketDetail(lookup, 'inspection:0', async () => { throw new TypeError('fetch failed') }),
    /네트워크 연결/,
  )
  await assert.rejects(
    () => fetchMarketDetail(lookup, 'inspection:0', async () => new Response('{}', { status: 403 })),
    /조회 권한.*HTTP 403/,
  )
  await assert.rejects(
    () => fetchMarketDetail(lookup, 'inspection:0', async () => new Response('', { status: 200 })),
    /응답이 비어/,
  )
  await assert.rejects(
    () => fetchMarketDetail(lookup, 'inspection:0', async () => new Response('{broken', { status: 200 })),
    /응답 형식/,
  )
})

test('renders every public field, input/output provenance, long-value hints, hidden counts, and missing reasons', () => {
  const detail: MarketDetailResponse = {
    schema: 'jw.detail-on-demand.v1',
    conversationId: 'conversation-1', responseId: 'trace-1', itemKey: 'ct:NCT0001', kind: 'evidence', partial: false,
    detail: { nct_id: 'NCT0001', intervention_details: [{ description: 'A'.repeat(320) }], secondary_outcomes: [{ description: null }] },
    input: { query: 'ferric carboxymaltose', requestParameters: { pageSize: 100 }, expansionGrade: 'notation' },
    output: { receivedCount: 32, directlyRelevantCount: 32, summary: { status: 'COMPLETED' }, calledAt: '2026-08-31T00:00:00Z', elapsedMs: 1234 },
    fieldMetadata: {
      publicFieldCount: 32, hiddenFieldCount: 4, hiddenFieldNotice: '내부 필드는 값 없이 개수만 제공합니다.',
      missingFields: { 'secondary_outcomes[0].description': '원천 응답에 값이 없습니다.' }, lengthHints: { 'intervention_details[0].description': 320 },
    },
  }
  const markup = renderToStaticMarkup(createElement(MarketDetailView, { detail }))

  assert.match(markup, /인풋/)
  assert.match(markup, /ferric carboxymaltose/)
  assert.match(markup, /아웃풋/)
  assert.match(markup, /32건/)
  assert.match(markup, /nct_id/)
  assert.match(markup, /NCT0001/)
  assert.match(markup, /intervention_details 1\.description/)
  assert.match(markup, /320자/)
  assert.match(markup, /내부 필드 4개 비표시/)
  assert.match(markup, /원천 응답에 값이 없습니다/)
  assert.match(markup, />-</)
})

test('presents source record labels for people and keeps internal metadata collapsed', () => {
  const detail: MarketDetailResponse = {
    schema: 'jw.detail-on-demand.v1',
    conversationId: 'conversation-1', responseId: 'trace-1', itemKey: 'patent:10-0101149', kind: 'evidence', partial: false,
    detail: {
      source_record: {
        DOMESTIC_INVN_NM: '퀴놀린형 메발로노락톤',
        PATENTEE: '닛산 가가쿠 고교',
        DOMESTIC_PATENT_NO: '10-0101149',
      },
      status_variants: ['소멸(존속기간만료)'],
      source_row_count: 274,
    },
    input: { query: '리바로젯 조성물 특허', requestParameters: {}, expansionGrade: 'exact' },
    output: { receivedCount: 274, directlyRelevantCount: undefined, summary: null, calledAt: '2026-08-31T00:00:00Z', elapsedMs: 1234 },
    fieldMetadata: { publicFieldCount: 5, hiddenFieldCount: 0, hiddenFieldNotice: '', missingFields: {}, lengthHints: {} },
  }
  const markup = renderToStaticMarkup(createElement(MarketDetailView, { detail }))

  assert.doesNotMatch(markup, /source_record\./)
  assert.doesNotMatch(markup, /status_variants\[0\]/)
  assert.match(markup, /market-detail-field-primary[^>]*>발명의 명칭</)
  assert.match(markup, /market-detail-field-source[^>]*>DOMESTIC_INVN_NM</)
  assert.match(markup, /조회 메타데이터 2개/)
  assert.match(markup, /market-detail-internal-fields/)
  assert.match(markup, /상태 변형 1/)
})
