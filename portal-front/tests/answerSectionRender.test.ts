import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test, { after } from 'node:test'
import { createElement, type ComponentType } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import react from '@vitejs/plugin-react'
import { createServer } from 'vite'

import type { AnswerSectionState } from '../src/utils/answerSections.ts'
import type { AnswerInspectionDetail } from '../src/utils/answerInspection.ts'
import type { MarketTable } from '../src/utils/marketTables.ts'
import { evidenceNavigationFailureMessage } from '../src/utils/evidenceNavigation.ts'
import { evidencePopoverRecord, groupEvidenceSources, prepareEvidenceDisplay } from '../src/utils/evidencePopover.ts'
import { restoreMarketAnswerSurface } from '../src/utils/marketChatRestore.ts'

interface ChatMessageAIProps {
  id: string
  planContent: string
  isGenerating: boolean
  sections?: AnswerSectionState[]
  onEvidenceOpen?: (evidenceId: string) => void
  reasoningSteps?: readonly { nodeId: string; nodeLabel: string; rationale: string }[]
  reasoningInitiallyExpanded?: boolean
  tables?: MarketTable[]
}

interface AnswerInspectionPanelProps {
  open: boolean
  answerLabel: string
  detail?: AnswerInspectionDetail
  focusEvidenceId?: string
  focusRequestId?: number
  onClose: () => void
}

interface EvidencePopoverProps {
  evidenceId: string
  evidence: readonly { evidenceId: string; label: string }[]
  catalog?: AnswerSectionState['evidenceCatalog']
  group?: Extract<AnswerSectionState['parts'][number], { type: 'evidence' }>['group']
  onClose: () => void
  onSelectEvidence: (evidenceId: string) => void
}

const vite = await createServer({
  configFile: false,
  root: new URL('..', import.meta.url).pathname,
  plugins: [react()],
  optimizeDeps: { noDiscovery: true },
  server: { middlewareMode: true, hmr: { port: 24690 } },
  appType: 'custom',
})
const { default: ChatMessageAI } = await vite.ssrLoadModule('/src/components/main/ChatMessageAI.tsx') as { default: ComponentType<ChatMessageAIProps> }
const { default: AnswerInspectionPanel } = await vite.ssrLoadModule('/src/components/main/AnswerInspectionPanel.tsx') as { default: ComponentType<AnswerInspectionPanelProps> }
const { default: EvidencePopover } = await vite.ssrLoadModule('/src/components/main/EvidencePopover.tsx') as { default: ComponentType<EvidencePopoverProps> }

after(async () => vite.close())

test('legacy answers keep the existing headings and do not activate section slots', () => {
  const markup = renderToStaticMarkup(createElement(ChatMessageAI, {
    id: 'legacy-answer',
    planContent: '## 핵심 답\n\n기존 본문\n\n## 종합 인사이트\n\n기존 인사이트',
    isGenerating: false,
  }))

  assert.match(markup, /<h2>핵심 답<\/h2>/)
  assert.match(markup, /종합 인사이트/)
  assert.doesNotMatch(markup, /data-answer-sections=/)
  assert.doesNotMatch(markup, />조사 결과<\/h2>/)
})

test('new sections render by metadata order with a headingless insight and metadata facts heading', () => {
  const sections: AnswerSectionState[] = [{
    id: 'facts', order: 1, kind: 'facts', title: '서버 조사 제목', status: 'complete',
    parts: [{ type: 'text', text: '하단 조사 결과' }],
  }, {
    id: 'insight', order: 0, kind: 'insight', status: 'complete',
    parts: [{ type: 'text', text: '상단 종합 인사이트' }],
  }]
  const markup = renderToStaticMarkup(createElement(ChatMessageAI, {
    id: 'section-answer', planContent: '', isGenerating: false, sections,
  }))

  assert.match(markup, /data-answer-sections="jw.answer-sections.v1"/)
  assert.ok(markup.indexOf('상단 종합 인사이트') < markup.indexOf('하단 조사 결과'))
  assert.match(markup, /<h2>서버 조사 제목<\/h2>/)
  assert.doesNotMatch(markup, /<h2>종합 인사이트<\/h2>/)
})

test('structured tables render inside the facts section instead of flattening into prose', () => {
  const sections: AnswerSectionState[] = [{
    id: 'insight', order: 0, kind: 'insight', status: 'complete',
    parts: [{ type: 'text', text: '상단 인사이트' }],
  }, {
    id: 'facts', order: 1, kind: 'facts', status: 'complete',
    parts: [{ type: 'text', text: '조사 결과 요약' }],
  }]
  const tables: MarketTable[] = [{
    table_id: 'market-table', title: '시장 표', source_label: '내부 데이터마트',
    columns: [{ key: 'value', label: '값', type: 'string', unit: null, align: 'left' }],
    rows: [{ record_id: 'market-row', cells: { value: '구조화 값' } }],
    row_count: 1, omitted_columns: [],
  }]
  const markup = renderToStaticMarkup(createElement(ChatMessageAI, {
    id: 'section-table-answer', planContent: '', isGenerating: false, sections, tables,
  }))
  const factsStart = markup.indexOf('data-section-id="facts"')
  const factsEnd = markup.indexOf('</section>', factsStart)
  const tableStart = markup.indexOf('market-structured-table')

  assert.ok(factsStart >= 0 && tableStart > factsStart && tableStart < factsEnd)
  assert.match(markup, /구조화 값/)
  assert.doesNotMatch(markup, /\|\s*값\s*\|/)
})

test('three sections render answer then insight then titled facts', () => {
  const sections: AnswerSectionState[] = [{
    id: 'facts', order: 2, kind: 'facts', status: 'complete',
    parts: [{ type: 'text', text: '하단 조사 결과' }],
  }, {
    id: 'answer', order: 0, kind: 'answer', status: 'complete',
    parts: [{ type: 'text', text: '최상단 직접 답변' }],
  }, {
    id: 'insight', order: 1, kind: 'insight', status: 'complete',
    parts: [{ type: 'text', text: '중단 확장 인사이트' }],
  }]
  const markup = renderToStaticMarkup(createElement(ChatMessageAI, {
    id: 'three-section-answer', planContent: '', isGenerating: false, sections,
  }))

  assert.ok(markup.indexOf('최상단 직접 답변') < markup.indexOf('중단 확장 인사이트'))
  assert.ok(markup.indexOf('중단 확장 인사이트') < markup.indexOf('하단 조사 결과'))
  assert.equal((markup.match(/<h2>조사 결과<\/h2>/g) ?? []).length, 1)
})

test('pending slots show generation state and evidence markers retain the exact identifier', () => {
  const sections: AnswerSectionState[] = [{
    id: 'insight', order: 0, kind: 'insight', status: 'streaming',
    parts: [
      { type: 'text', text: '관찰된 변화입니다. ' },
      { type: 'evidence', evidenceId: 'document:chunk:한글-7', label: '출처' },
    ],
  }, {
    id: 'facts', order: 1, kind: 'facts', status: 'pending', parts: [],
  }]
  const markup = renderToStaticMarkup(createElement(ChatMessageAI, {
    id: 'evidence-answer', planContent: '', isGenerating: true, sections,
  }))

  assert.match(markup, /data-evidence-id="document:chunk:한글-7"/)
  assert.match(markup, />\[출처\]<\/button>/)
  assert.equal((markup.match(/생성 중/g) ?? []).length, 2)
})

test('inspection items expose the exact evidence identifier as a stable anchor', () => {
  const detail: AnswerInspectionDetail = {
    schema: 'r12.5.inspect.v1', question: '앵커', expansion: null,
    calls: [{
      sequence: 1,
      evidence_id: 'mart:call:1',
      source_label: '내부 데이터마트',
      status: '완료', elapsed_seconds: 0.1,
      request_parameters: { query: '시장 규모' },
      counts: { returned: 1 }, unused_count: 0, dropped_count: 0,
      output: { evidence_id: 'mart:row:1', record_id: 'row-1', value: 10 },
      drop_reasons: [],
    }],
  }
  const markup = renderToStaticMarkup(createElement(AnswerInspectionPanel, {
    open: true, answerLabel: '앵커', detail, focusEvidenceId: 'mart:row:1', focusRequestId: 1, onClose: () => undefined,
  }))

  assert.match(markup, /data-evidence-id="mart:call:1"/)
  assert.match(markup, /data-evidence-id="mart:row:1"/)
})

test('evidence markers render every source label without anonymous compression', () => {
  const sections: AnswerSectionState[] = [{
    id: 'insight', order: 0, kind: 'insight', status: 'complete',
    parts: [
      { type: 'text', text: '관계 문장입니다. ' },
      { type: 'evidence', evidenceId: 'mart:row:1', label: '출처: 내부 데이터마트' },
      { type: 'evidence', evidenceId: 'ct:row:NCT01234567', label: '출처: ClinicalTrials.gov NCT01234567' },
      { type: 'evidence', evidenceId: 'patent:10-2571797', label: '출처: 식약처 특허목록 10-2571797' },
    ],
  }]
  const markup = renderToStaticMarkup(createElement(ChatMessageAI, {
    id: 'source-label-answer', planContent: '', isGenerating: false, sections,
  }))

  assert.match(markup, />\[출처: 내부 데이터마트\]<\/button>/)
  assert.match(markup, />\[출처: ClinicalTrials\.gov NCT01234567\]<\/button>/)
  assert.match(markup, />\[출처: 식약처 특허목록 10-2571797\]<\/button>/)
  assert.equal((markup.match(/class="answer-evidence-marker"/g) ?? []).length, 3)
  assert.doesNotMatch(markup, /외 \d+건/)
})

test('compound evidence renders one informative marker and keeps its group context', () => {
  const group = {
    schema: 'jw.evidence-group.v1' as const,
    groupId: 'eg-insight-1',
    primary: { evidenceId: 'mart:row:1', label: '출처: 내부 데이터마트', sourceKey: 'mart', sourceLabel: '내부 데이터마트' },
    members: [
      { evidenceId: 'mart:row:1', label: '출처: 내부 데이터마트', sourceKey: 'mart', sourceLabel: '내부 데이터마트' },
      { evidenceId: 'ct:NCT1', label: '출처: ClinicalTrials.gov NCT1', sourceKey: 'ct', sourceLabel: 'ClinicalTrials.gov' },
      { evidenceId: 'patent:10-1', label: '출처: 식약처 특허목록 10-1', sourceKey: 'patent', sourceLabel: '식약처 특허목록' },
    ],
    sourceBreakdown: [
      { sourceKey: 'mart', sourceLabel: '내부 데이터마트', count: 1 },
      { sourceKey: 'ct', sourceLabel: 'ClinicalTrials.gov', count: 1 },
      { sourceKey: 'patent', sourceLabel: '식약처 특허목록', count: 1 },
    ],
  }
  const sections: AnswerSectionState[] = [{
    id: 'insight', order: 0, kind: 'insight', status: 'complete',
    parts: [{ type: 'text', text: '복합 근거 문장입니다. ' }, {
      type: 'evidence', evidenceId: group.primary.evidenceId,
      label: '출처: 내부 데이터마트 + ClinicalTrials.gov + 식약처 특허목록', group,
    }],
  }]
  const markup = renderToStaticMarkup(createElement(ChatMessageAI, {
    id: 'compound-evidence-answer', planContent: '', isGenerating: false, sections,
  }))
  const prepared = prepareEvidenceDisplay(sections[0]!.parts)

  assert.equal((markup.match(/class="answer-evidence-marker"/g) ?? []).length, 1)
  assert.match(markup, />\[출처: 내부 데이터마트 \+ ClinicalTrials\.gov \+ 식약처 특허목록\]<\/button>/)
  assert.doesNotMatch(markup, /외 \d+건/)
  assert.equal(prepared.targetsByLookupKey.get('eg-insight-1')?.group?.groupId, 'eg-insight-1')
  assert.equal(prepared.targetsByLookupKey.get('eg-insight-1')?.evidenceId, 'mart:row:1')
})

test('compound groups sharing one primary evidence keep label and members under distinct group keys', () => {
  const first = {
    schema: 'jw.evidence-group.v1' as const,
    groupId: 'eg-patent',
    primary: { evidenceId: 'mart:shared', label: '출처: 내부 데이터마트', sourceKey: 'mart', sourceLabel: '내부 데이터마트' },
    members: [
      { evidenceId: 'mart:shared', label: '출처: 내부 데이터마트', sourceKey: 'mart', sourceLabel: '내부 데이터마트' },
      { evidenceId: 'patent:10-1198822', label: '출처: 식품의약품안전처 의약품 특허목록', sourceKey: 'patent', sourceLabel: '식품의약품안전처 의약품 특허목록' },
    ],
    sourceBreakdown: [
      { sourceKey: 'mart', sourceLabel: '내부 데이터마트', count: 1 },
      { sourceKey: 'patent', sourceLabel: '식품의약품안전처 의약품 특허목록', count: 1 },
    ],
  }
  const second = {
    schema: 'jw.evidence-group.v1' as const,
    groupId: 'eg-clinical',
    primary: { evidenceId: 'mart:shared', label: '출처: 내부 데이터마트', sourceKey: 'mart', sourceLabel: '내부 데이터마트' },
    members: [
      { evidenceId: 'mart:shared', label: '출처: 내부 데이터마트', sourceKey: 'mart', sourceLabel: '내부 데이터마트' },
      { evidenceId: 'ct:NCT0001', label: '출처: ClinicalTrials.gov', sourceKey: 'ct', sourceLabel: 'ClinicalTrials.gov' },
    ],
    sourceBreakdown: [
      { sourceKey: 'mart', sourceLabel: '내부 데이터마트', count: 1 },
      { sourceKey: 'ct', sourceLabel: 'ClinicalTrials.gov', count: 1 },
    ],
  }
  const parts: AnswerSectionState['parts'] = [
    { type: 'text', text: '특허 문장 ' },
    { type: 'evidence', evidenceId: first.primary.evidenceId, label: '출처: 내부 데이터마트 + 식품의약품안전처 의약품 특허목록', group: first },
    { type: 'text', text: ' 임상 문장 ' },
    { type: 'evidence', evidenceId: second.primary.evidenceId, label: '출처: 내부 데이터마트 + ClinicalTrials.gov', group: second },
  ]
  const prepared = prepareEvidenceDisplay(parts)
  const markup = renderToStaticMarkup(createElement(ChatMessageAI, {
    id: 'shared-primary-groups', planContent: '', isGenerating: false,
    sections: [{ id: 'insight', order: 0, kind: 'insight', status: 'complete', parts }],
  }))
  const patent = prepared.targetsByLookupKey.get('eg-patent')
  const clinical = prepared.targetsByLookupKey.get('eg-clinical')

  assert.equal(prepared.targetsByLookupKey.size, 2)
  assert.equal(patent?.group?.groupId, 'eg-patent')
  assert.deepEqual(patent?.evidence.map(item => item.evidenceId), ['mart:shared', 'patent:10-1198822'])
  assert.equal(clinical?.group?.groupId, 'eg-clinical')
  assert.deepEqual(clinical?.evidence.map(item => item.evidenceId), ['mart:shared', 'ct:NCT0001'])
  assert.deepEqual(
    prepared.visibleParts.filter(part => part.type === 'evidence').map(part => part.lookupKey),
    ['eg-patent', 'eg-clinical'],
  )
  assert.equal((markup.match(/data-evidence-id="mart:shared"/g) ?? []).length, 2)
  assert.match(markup, /data-evidence-group-id="eg-patent"/)
  assert.match(markup, /data-evidence-group-id="eg-clinical"/)
})

test('compound evidence is grouped by source and missing records stay explicit', () => {
  const group = {
    schema: 'jw.evidence-group.v1' as const,
    groupId: 'eg-insight-1',
    primary: { evidenceId: 'mart:row:1', label: '출처: 내부 데이터마트', sourceKey: 'mart', sourceLabel: '내부 데이터마트' },
    members: [
      { evidenceId: 'mart:row:1', label: '출처: 내부 데이터마트', sourceKey: 'mart', sourceLabel: '내부 데이터마트' },
      { evidenceId: 'ct:NCT1', label: '출처: ClinicalTrials.gov NCT1', sourceKey: 'ct', sourceLabel: 'ClinicalTrials.gov' },
      { evidenceId: 'ct:NCT2', label: '출처: ClinicalTrials.gov NCT2', sourceKey: 'ct', sourceLabel: 'ClinicalTrials.gov' },
    ],
    sourceBreakdown: [
      { sourceKey: 'mart', sourceLabel: '내부 데이터마트', count: 1 },
      { sourceKey: 'ct', sourceLabel: 'ClinicalTrials.gov', count: 2 },
    ],
  }
  const grouped = groupEvidenceSources(group, {
    'mart:row:1': {
      evidence_id: 'mart:row:1', source_name: '내부 데이터마트', identifier: '리바로젯', query: '매출 추이',
      counts: { received: 12, direct_related: 12 }, record: { 매출: '91.53억원' },
    },
    'ct:NCT1': {
      evidence_id: 'ct:NCT1', source_name: 'ClinicalTrials.gov', identifier: 'NCT1', query: 'pitavastatin',
      counts: { received: 2, direct_related: 1 }, record: { 상태: 'COMPLETED' },
    },
  })

  assert.deepEqual(grouped.map(source => [source.sourceLabel, source.count, source.items.length]), [
    ['내부 데이터마트', 1, 1], ['ClinicalTrials.gov', 2, 2],
  ])
  assert.equal(grouped[1]?.items[1]?.available, false)
  assert.equal(grouped[1]?.items[1]?.identifier, 'ct:NCT2')
})

test('compound evidence popover renders primary detail and selectable source tabs', () => {
  const group = {
    schema: 'jw.evidence-group.v1' as const,
    groupId: 'eg-insight-1',
    primary: { evidenceId: 'mart:row:1', label: '출처: 내부 데이터마트', sourceKey: 'mart', sourceLabel: '내부 데이터마트' },
    members: [
      { evidenceId: 'mart:row:1', label: '출처: 내부 데이터마트', sourceKey: 'mart', sourceLabel: '내부 데이터마트' },
      { evidenceId: 'ct:NCT1', label: '출처: ClinicalTrials.gov NCT1', sourceKey: 'ct', sourceLabel: 'ClinicalTrials.gov' },
    ],
    sourceBreakdown: [
      { sourceKey: 'mart', sourceLabel: '내부 데이터마트', count: 1 },
      { sourceKey: 'ct', sourceLabel: 'ClinicalTrials.gov', count: 1 },
    ],
  }
  const markup = renderToStaticMarkup(createElement(EvidencePopover, {
    evidenceId: 'mart:row:1',
    evidence: group.members,
    group,
    catalog: {
      'mart:row:1': {
        evidence_id: 'mart:row:1', source_name: '내부 데이터마트', identifier: '리바로젯', query: '매출 추이',
        counts: { received: 12, direct_related: 12 }, record: { 매출: '91.53억원' },
      },
      'ct:NCT1': {
        evidence_id: 'ct:NCT1', source_name: 'ClinicalTrials.gov', identifier: 'NCT1', query: 'pitavastatin',
        counts: { received: 2, direct_related: 1 }, record: { 상태: 'COMPLETED' },
      },
    },
    onClose: () => undefined,
    onSelectEvidence: () => undefined,
  }))

  assert.match(markup, /대표 근거 상세/)
  assert.match(markup, /관련 출처 전체/)
  assert.match(markup, /role="tablist"/)
  assert.match(markup, /role="tab"[^>]*aria-selected="true"[^>]*>내부 데이터마트 <span>1건<\/span>/)
  assert.match(markup, /role="tab"[^>]*aria-selected="false"[^>]*>ClinicalTrials\.gov <span>1건<\/span>/)
  assert.match(markup, /aria-pressed="true"/)
  assert.match(markup, /role="tabpanel"/)
})

test('missing evidence navigation is visible and delayed targets are observed', async () => {
  assert.equal(
    evidenceNavigationFailureMessage('mart:missing:7'),
    '해당 근거 항목을 찾을 수 없습니다(mart:missing:7)',
  )
  const source = await readFile(new URL('../src/components/main/AnswerInspectionPanel.tsx', import.meta.url), 'utf8')
  assert.match(source, /MutationObserver/)
  assert.match(source, /role="alert"/)
  assert.match(source, /console\.warn/)
})

test('evidence display keeps every unique source for the popover but renders at most three markers', () => {
  const prepared = prepareEvidenceDisplay([
    { type: 'text', text: '관계 문장입니다. ' },
    { type: 'evidence', evidenceId: 'mart:1', label: '출처: 내부 데이터마트' },
    { type: 'evidence', evidenceId: 'mart:1', label: '출처: 내부 데이터마트' },
    { type: 'evidence', evidenceId: 'ct:1', label: '출처: ClinicalTrials.gov NCT1' },
    { type: 'evidence', evidenceId: 'patent:1', label: '출처: 식약처 특허목록 10-1' },
    { type: 'evidence', evidenceId: 'hira:1', label: '출처: HIRA I10 2025' },
  ])

  assert.deepEqual(prepared.visibleParts.filter(part => part.type === 'evidence').map(part => part.evidenceId), ['mart:1', 'ct:1', 'patent:1'])
  assert.deepEqual(prepared.targetsByLookupKey.get('mart:1')?.evidence.map(part => part.evidenceId), ['mart:1', 'ct:1', 'patent:1', 'hira:1'])
  assert.doesNotMatch(prepared.visibleParts.map(part => part.type === 'text' ? part.text : part.label).join(''), /외 \d+건/)
})

test('evidence popover resolves the bundled record without inspection traversal', () => {
  const catalog = {
    'ct:NCT01234567': {
      evidence_id: 'ct:NCT01234567',
      source_name: 'ClinicalTrials.gov',
      identifier: 'NCT01234567',
      query: 'pitavastatin',
      counts: { received: 23, direct_related: 2 },
      record: { 시험명: 'Pitavastatin study', 상태: 'COMPLETED' },
    },
  }
  const record = evidencePopoverRecord(catalog, 'ct:NCT01234567')
  assert.equal(record?.source_name, 'ClinicalTrials.gov')
  assert.equal(record?.identifier, 'NCT01234567')
  assert.equal(record?.query, 'pitavastatin')
  assert.equal(record?.counts.received, 23)
  assert.deepEqual(record?.record, { 시험명: 'Pitavastatin study', 상태: 'COMPLETED' })
  assert.equal(evidencePopoverRecord(catalog, 'ct:missing'), undefined)
})

test('restored reasoning is expanded by default without changing completed live answers', () => {
  const markup = renderToStaticMarkup(createElement(ChatMessageAI, {
    id: 'restored-reasoning', planContent: '복원 본문', isGenerating: false,
    reasoningSteps: [{ nodeId: 'direct-plan', nodeLabel: 'Market', rationale: '조회 계획' }],
    reasoningInitiallyExpanded: true,
  }))

  assert.match(markup, />추론 과정</)
  assert.match(markup, /ai-content-wrap open/)
  assert.match(markup, /aria-expanded="true"/)
  assert.doesNotMatch(markup, /ai-content-wrap close/)
})

test('chat log restore maps rich sections, structured tables, and reasoning into live state', () => {
  const answer = restoreMarketAnswerSurface({
    text: '평탄 본문',
    agentFlowExecutedData: [{ nodeId: 'direct-plan', nodeLabel: '조회 계획' }],
    answer_sections: {
      schema: 'jw.answer-sections.v1',
      sections: [
        { id: 'insight', order: 0, kind: 'insight', status: 'pending' },
        { id: 'facts', order: 1, kind: 'facts', title: '조사 결과', status: 'pending' },
      ],
      paragraphs: {
        insight: [{ text: '복원 인사이트', paragraph_start: true, evidence: [] }],
        facts: [{ text: '복원 조사 결과', paragraph_start: true, evidence: [] }],
      },
    },
    tables: [{
      table_id: 'restore-table', title: '복원 표', source_label: '내부 데이터마트',
      columns: [{ key: 'value', label: '값', type: 'string', unit: null, align: 'left' }],
      rows: [{ record_id: 'row-1', cells: { value: '복원값' } }], row_count: 1, omitted_columns: [],
    }],
  })

  assert.equal(answer.planContent, '복원 인사이트\n\n## 조사 결과\n\n복원 조사 결과')
  assert.deepEqual(answer.sections?.map(section => section.id), ['insight', 'facts'])
  assert.equal(answer.tables?.[0]?.table_id, 'restore-table')
  assert.equal(answer.reasoningInitiallyExpanded, true)
})

test('chat log restore keeps legacy text when rich fields are absent', () => {
  const answer = restoreMarketAnswerSurface({ text: '## 핵심 답\n\n구 본문' })

  assert.equal(answer.planContent, '## 핵심 답\n\n구 본문')
  assert.equal(answer.sections, undefined)
  assert.equal(answer.tables, undefined)
})
