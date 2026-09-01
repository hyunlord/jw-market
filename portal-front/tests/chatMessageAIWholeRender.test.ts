import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test, { after } from 'node:test'
import { createElement, type ComponentType } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import react from '@vitejs/plugin-react'
import { createServer } from 'vite'
import type { MarketTable } from '../src/utils/marketTables.ts'
import type { MarketChart } from '../src/utils/marketStream.ts'
import type { ReasoningStep } from '../src/utils/planSignal.ts'

interface ChatMessageAIProps {
  id: string
  planContent: string
  isGenerating: boolean
  headerLabel?: string
  onInspectionOpen?: () => void
  inspectionOpen?: boolean
  tables?: MarketTable[]
  charts?: MarketChart[]
  chartError?: string
  streamNotice?: string
  reasoningSteps?: ReasoningStep[]
  reasoningStreaming?: boolean
}

const vite = await createServer({
  configFile: false,
  root: new URL('..', import.meta.url).pathname,
  plugins: [react()],
  optimizeDeps: { noDiscovery: true },
  server: { middlewareMode: true, hmr: { port: 24682 } },
  appType: 'custom',
})
const { default: ChatMessageAI } = await vite.ssrLoadModule(
  '/src/components/main/ChatMessageAI.tsx',
) as { default: ComponentType<ChatMessageAIProps> }

after(async () => vite.close())

async function fixture(name: string): Promise<string> {
  return readFile(new URL(`./fixtures/chat-answer-collapse/${name}`, import.meta.url), 'utf8')
}

function renderAnswer(markdown: string): string {
  return renderToStaticMarkup(createElement(ChatMessageAI, {
    id: 'whole-answer',
    planContent: markdown,
    isGenerating: false,
    headerLabel: 'AI 분석 결과',
    onInspectionOpen: () => undefined,
  }))
}

test('the full answer component keeps narrative visible and known data sections collapsed', async () => {
  const markup = renderAnswer(await fixture('rivaroxaban-clinical.md'))

  assert.match(markup, /<div class="text-wrap">AI 분석 결과<\/div>/)
  assert.match(markup, /<h2>핵심 답<\/h2>/)
  assert.match(markup, /aria-expanded="false"[^>]*><span>조사 범위와 완전성<\/span>/)
  assert.match(markup, /<h2 class="answer-sources-heading">출처<\/h2>/)
  assert.match(markup, /aria-controls="answer-inspection-panel"/)
  assert.match(markup, /data-section-policy="source-boundary-v1"/)
})

test('an arbitrary section title remains expanded in the full answer component', () => {
  const markup = renderAnswer([
    '## 새 분석 제목',
    '제목이 바뀌어도 이 본문은 숨지 않습니다.',
    '',
    '## 조사 범위와 완전성',
    '이 데이터 절은 접힙니다.',
  ].join('\n'))

  assert.match(markup, /<h2>새 분석 제목<\/h2>/)
  assert.match(markup, /제목이 바뀌어도 이 본문은 숨지 않습니다\./)
  assert.doesNotMatch(markup, /aria-expanded="false"[^>]*><span>새 분석 제목<\/span>/)
})

test('the source boundary marks an arbitrary following section as collapsible data', () => {
  const markup = renderAnswer([
    '## 요약 제목',
    '출처 앞의 본문입니다.',
    '',
    '## 출처',
    '- 내부 데이터마트 — "리바로" 조회',
    '',
    '## 백엔드가 새로 만든 제목',
    '| 값 |',
    '| --- |',
    '| 1 |',
  ].join('\n'))

  assert.match(markup, /<h2>요약 제목<\/h2>/)
  assert.match(markup, /<h2 class="answer-sources-heading">출처<\/h2>/)
  assert.match(markup, /aria-expanded="false"[^>]*><span>백엔드가 새로 만든 제목<\/span>/)
})

test('headingless and very short answers render without collapse controls', () => {
  const markup = renderAnswer('확인된 값이 없습니다.')

  assert.match(markup, /<p>확인된 값이 없습니다\.<\/p>/)
  assert.doesNotMatch(markup, /answer-sections-toolbar|모두 펼치기|모두 접기/)
})

test('level-two headings inside fences and blockquotes are not section boundaries', () => {
  const markup = renderAnswer([
    '본문',
    '',
    '```md',
    '## 코드 내부',
    '```',
    '',
    '> ## 인용 내부',
  ].join('\n'))

  assert.match(markup, /<pre><code class="language-md">## 코드 내부/)
  assert.match(markup, /<blockquote>[\s\S]*<h2>인용 내부<\/h2>[\s\S]*<\/blockquote>/)
  assert.doesNotMatch(markup, /answer-sections-toolbar/)
})

test('the same complete answer produces deterministic DOM', async () => {
  const markdown = await fixture('rivaroxaban-patents.md')
  assert.equal(renderAnswer(markdown), renderAnswer(markdown))
})

test('the whole answer keeps markdown and renders every structured table row beside it', () => {
  const tables: MarketTable[] = [{
    table_id: 'v4-clinical',
    title: '임상시험 상세',
    source_label: 'ClinicalTrials.gov',
    columns: [{ key: 'trial', label: '시험', type: 'string', unit: null, align: 'left' }],
    rows: [
      { cells: { trial: 'NCT05151731' }, record_id: 'ct:NCT05151731' },
      { cells: { trial: 'NCT07523971' }, record_id: 'ct:NCT07523971' },
    ],
    row_count: 2,
    omitted_columns: [],
  }]
  const markup = renderToStaticMarkup(createElement(ChatMessageAI, {
    id: 'whole-answer-with-table',
    planContent: '## 핵심 답\n\n마크다운 본문은 그대로 남습니다.',
    isGenerating: false,
    headerLabel: 'AI 분석 결과',
    tables,
  }))

  assert.match(markup, /마크다운 본문은 그대로 남습니다\./)
  assert.match(markup, /aria-label="시장 분석 표"/)
  assert.match(markup, /NCT05151731/)
  assert.match(markup, /NCT07523971/)
  assert.ok(markup.indexOf('마크다운 본문은 그대로 남습니다.') < markup.indexOf('시장 분석 표'))
})

test('the whole answer renders an exact backend chart beside the preserved table', () => {
  const charts: MarketChart[] = [{
    chart_id: 'v4-chart-1',
    chart_type: 'line',
    title: '매출 추이',
    x: ['2026-04', '2026-05'],
    series: [{ label: '매출', values: [1, 2], record_ids: ['r1', 'r2'] }],
    unit: '억원',
    source_label: '내부 데이터마트',
  }]
  const markup = renderToStaticMarkup(createElement(ChatMessageAI, {
    id: 'whole-answer-with-chart',
    planContent: '## 핵심 답\n\n표와 차트가 함께 남습니다.',
    isGenerating: false,
    headerLabel: 'AI 분석 결과',
    charts,
  }))

  assert.match(markup, /data-chart-id="v4-chart-1"/)
  assert.match(markup, /매출 추이/)
  assert.match(markup, /내부 데이터마트/)
  assert.match(markup, /<canvas/)
  assert.ok(markup.indexOf('표와 차트가 함께 남습니다.') < markup.indexOf('data-chart-id="v4-chart-1"'))
})

test('the whole answer exposes malformed chart data as an alert', () => {
  const markup = renderToStaticMarkup(createElement(ChatMessageAI, {
    id: 'whole-answer-chart-error',
    planContent: '본문은 유지됩니다.',
    isGenerating: false,
    chartError: '차트 데이터를 표시할 수 없습니다.',
  }))

  assert.match(markup, /본문은 유지됩니다./)
  assert.match(markup, /role="alert">차트 데이터를 표시할 수 없습니다\./)
})

test('the whole answer keeps valid charts visible beside a partial-format warning', () => {
  const charts: MarketChart[] = [{
    chart_id: 'valid-after-partial-failure',
    chart_type: 'line',
    title: '유효 차트',
    x: ['2026-01', '2026-02'],
    x_label: '기간',
    series: [{ label: '매출', values: [1, 2], record_ids: ['r1', 'r2'] }],
    unit: '억원',
    source_label: '내부 데이터마트',
  }]
  const markup = renderToStaticMarkup(createElement(ChatMessageAI, {
    id: 'whole-answer-partial-chart-error',
    planContent: '본문도 유지됩니다.',
    isGenerating: false,
    charts,
    chartError: '차트 1개를 표시하지 못했습니다(데이터 형식 불일치).',
  }))

  assert.match(markup, /본문도 유지됩니다./)
  assert.match(markup, /role="alert">차트 1개를 표시하지 못했습니다/)
  assert.match(markup, /data-chart-id="valid-after-partial-failure"/)
  assert.match(markup, /<canvas/)
})

test('a terminal stream notice remains beside the already received body', () => {
  const reasoningSteps = Array.from({ length: 5 }, (_, index) => ({
    nodeId: `step-${index}`,
    nodeLabel: 'Market',
    rationale: `단계 ${index + 1}`,
  }))
  const markup = renderToStaticMarkup(createElement(ChatMessageAI, {
    id: 'whole-answer-stream-timeout',
    planContent: '지금까지 받은 본문입니다.',
    isGenerating: false,
    streamNotice: '응답이 시간 제한(240초)을 넘어 중단됐습니다. 지금까지 받은 내용만 표시합니다.',
    reasoningSteps,
    reasoningStreaming: false,
  }))

  assert.match(markup, /지금까지 받은 본문입니다\./)
  assert.match(markup, /role="alert"/)
  assert.match(markup, /시간 제한\(240초\)/)
  assert.equal((markup.match(/단계 [1-5]/g) ?? []).length, 5)
  assert.doesNotMatch(markup, /BFF_SSE_STREAM_TIMEOUT|internal detail/)
})
