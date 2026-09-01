import assert from 'node:assert/strict'
import test, { after } from 'node:test'
import { createElement, type ComponentType } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import react from '@vitejs/plugin-react'
import { createServer } from 'vite'

import type { EvidenceDisplayCatalog, EvidenceGroup } from '../src/utils/answerSections.ts'
import type { MarketDetailLookup } from '../src/utils/marketDetail.ts'

interface EvidencePopoverProps {
  evidenceId: string
  evidence: readonly { evidenceId: string; label: string }[]
  catalog?: EvidenceDisplayCatalog
  group?: EvidenceGroup
  detailLookup?: MarketDetailLookup
  onClose: () => void
  onSelectEvidence: (evidenceId: string) => void
}

const vite = await createServer({
  configFile: false,
  root: new URL('..', import.meta.url).pathname,
  plugins: [react()],
  optimizeDeps: { noDiscovery: true },
  server: { middlewareMode: true, hmr: { port: 24692 } },
  appType: 'custom',
})
const { default: EvidencePopover } = await vite.ssrLoadModule('/src/components/main/EvidencePopover.tsx') as {
  default: ComponentType<EvidencePopoverProps>
}
const { evidenceSourceKeyForRecord, evidenceSourceTabIndex } = await vite.ssrLoadModule('/src/utils/evidencePopover.ts') as {
  evidenceSourceKeyForRecord: (sources: readonly { sourceKey: string; items: readonly { evidenceId: string }[] }[], evidenceId: string) => string | undefined
  evidenceSourceTabIndex: (key: string, currentIndex: number, count: number) => number | undefined
}

after(async () => vite.close())

const group: EvidenceGroup = {
  schema: 'jw.evidence-group.v1',
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

const catalog: EvidenceDisplayCatalog = {
  'mart:row:1': {
    evidence_id: 'mart:row:1', source_name: '내부 데이터마트', identifier: '리바로젯', query: '매출 추이',
    counts: { received: 12, direct_related: 12 }, record: { 브랜드: '리바로젯', 기간: '2026-07', 지표명: '매출', 값: '91.53억원', 내부필드: '전체 상세 유지' },
  },
  'ct:NCT1': {
    evidence_id: 'ct:NCT1', source_name: 'ClinicalTrials.gov', identifier: 'NCT1', query: 'pitavastatin',
    counts: { received: 2, direct_related: 1 }, record: { 상태: 'COMPLETED' },
  },
}

test('compound evidence renders source tabs and the selected source identifiers', () => {
  const markup = renderToStaticMarkup(createElement(EvidencePopover, {
    evidenceId: 'mart:row:1', evidence: group.members, group, catalog,
    onClose: () => undefined, onSelectEvidence: () => undefined,
  }))

  assert.match(markup, /role="tablist"/)
  assert.match(markup, /role="tab"[^>]*aria-selected="true"[^>]*>내부 데이터마트 <span>1건<\/span>/)
  assert.match(markup, /role="tab"[^>]*aria-selected="false"[^>]*>ClinicalTrials\.gov <span>1건<\/span>/)
  assert.match(markup, /role="tabpanel"/)
  assert.match(markup, />리바로젯<\/span>/)
  assert.doesNotMatch(markup, />NCT1<\/span>/)
})

test('the selected source follows the currently displayed evidence record', () => {
  const sources = [
    { sourceKey: 'mart', items: [{ evidenceId: 'mart:row:1' }] },
    { sourceKey: 'ct', items: [{ evidenceId: 'ct:NCT1' }] },
  ]

  assert.equal(evidenceSourceKeyForRecord(sources, 'ct:NCT1'), 'ct')
  assert.equal(evidenceSourceKeyForRecord(sources, 'missing'), undefined)
})

test('source tabs support wrapping arrow navigation plus home and end', () => {
  assert.equal(evidenceSourceTabIndex('ArrowRight', 1, 2), 0)
  assert.equal(evidenceSourceTabIndex('ArrowLeft', 0, 2), 1)
  assert.equal(evidenceSourceTabIndex('Home', 1, 2), 0)
  assert.equal(evidenceSourceTabIndex('End', 0, 2), 1)
  assert.equal(evidenceSourceTabIndex('Enter', 0, 2), undefined)
})

test('single evidence keeps the existing fallback without source tabs', () => {
  const markup = renderToStaticMarkup(createElement(EvidencePopover, {
    evidenceId: 'mart:row:1', evidence: [{ evidenceId: 'mart:row:1', label: '출처: 내부 데이터마트' }], catalog,
    onClose: () => undefined, onSelectEvidence: () => undefined,
  }))

  assert.doesNotMatch(markup, /role="tablist"/)
  assert.match(markup, /해당 근거 레코드/)
})

test('available lazy detail adds an explicit expander while absent schema keeps compact content', () => {
  const props = {
    evidenceId: 'mart:row:1', evidence: [{ evidenceId: 'mart:row:1', label: '출처: 내부 데이터마트' }], catalog,
    onClose: () => undefined, onSelectEvidence: () => undefined,
  }
  const compact = renderToStaticMarkup(createElement(EvidencePopover, props))
  const expandable = renderToStaticMarkup(createElement(EvidencePopover, {
    ...props,
    detailLookup: { conversationId: 'conversation-1', responseId: 'trace-1', itemKeys: new Set(['mart:row:1']) },
  }))

  assert.match(compact, /91\.53억원/)
  assert.match(compact, /answer-evidence-summary-table/)
  assert.match(compact, /<details[^>]*class="trace-record-block"[^>]*open=""/)
  assert.doesNotMatch(compact, /내부필드/)
  assert.doesNotMatch(compact, /전체 상세 펼치기/)
  assert.match(expandable, /전체 상세 펼치기/)
})

test('states that direct-related counts were not provided instead of rendering dash-count', () => {
  const missingCountCatalog: EvidenceDisplayCatalog = {
    'mart:row:1': { ...catalog['mart:row:1']!, counts: { received: 4, direct_related: null } },
  }
  const markup = renderToStaticMarkup(createElement(EvidencePopover, {
    evidenceId: 'mart:row:1', evidence: [{ evidenceId: 'mart:row:1', label: '출처: 내부 데이터마트' }], catalog: missingCountCatalog,
    onClose: () => undefined, onSelectEvidence: () => undefined,
  }))

  assert.match(markup, /4건 \/ 직접 관련 값 미제공/)
  assert.doesNotMatch(markup, /-건/)
})

test('deduplicates repeated group members but distinguishes genuinely separate records with the same identifier', () => {
  const repeatedGroup: EvidenceGroup = {
    ...group,
    members: [
      group.members[0]!,
      group.members[0]!,
      { evidenceId: 'mart:row:2', label: '출처: 내부 데이터마트', sourceKey: 'mart', sourceLabel: '내부 데이터마트' },
    ],
    sourceBreakdown: [{ sourceKey: 'mart', sourceLabel: '내부 데이터마트', count: 3 }],
  }
  const repeatedCatalog: EvidenceDisplayCatalog = {
    ...catalog,
    'mart:row:2': { ...catalog['mart:row:1']!, evidence_id: 'mart:row:2' },
  }
  const markup = renderToStaticMarkup(createElement(EvidencePopover, {
    evidenceId: 'mart:row:1', evidence: repeatedGroup.members, group: repeatedGroup, catalog: repeatedCatalog,
    onClose: () => undefined, onSelectEvidence: () => undefined,
  }))

  assert.equal((markup.match(/>리바로젯 \(1\)<\/span>/g) ?? []).length, 1)
  assert.equal((markup.match(/>리바로젯 \(2\)<\/span>/g) ?? []).length, 1)
})

test('opens both records when there are two and only the first when there are three', () => {
  const records = Object.fromEntries([1, 2, 3].map(index => [`ct:NCT${index}`, {
    evidence_id: `ct:NCT${index}`,
    source_name: 'ClinicalTrials.gov',
    identifier: `NCT${index}`,
    query: 'statin',
    counts: { received: 3, direct_related: 3 },
    record: { nct_id: `NCT${index}`, brief_title: `Study ${index}`, overall_status: 'COMPLETED' },
  }])) as EvidenceDisplayCatalog
  const refs = [1, 2, 3].map(index => ({ evidenceId: `ct:NCT${index}`, label: `출처: ClinicalTrials.gov NCT${index}` }))
  const two = renderToStaticMarkup(createElement(EvidencePopover, {
    evidenceId: 'ct:NCT1', evidence: refs.slice(0, 2), catalog: records,
    onClose: () => undefined, onSelectEvidence: () => undefined,
  }))
  const three = renderToStaticMarkup(createElement(EvidencePopover, {
    evidenceId: 'ct:NCT1', evidence: refs, catalog: records,
    onClose: () => undefined, onSelectEvidence: () => undefined,
  }))

  assert.equal((two.match(/<details class="trace-record-block" open=""/g) ?? []).length, 2)
  assert.equal((three.match(/<details class="trace-record-block" open=""/g) ?? []).length, 1)
})

test('keeps the existing full compact renderer for an unregistered source', () => {
  const unknownCatalog: EvidenceDisplayCatalog = {
    'new:1': {
      evidence_id: 'new:1', source_name: '새 원천', identifier: 'NEW-1', query: '새 질의',
      counts: { received: 1, direct_related: 1 }, record: { 원본필드: '보존된 값' },
    },
  }
  const markup = renderToStaticMarkup(createElement(EvidencePopover, {
    evidenceId: 'new:1', evidence: [{ evidenceId: 'new:1', label: '출처: 새 원천' }], catalog: unknownCatalog,
    onClose: () => undefined, onSelectEvidence: () => undefined,
  }))

  assert.match(markup, /원본필드/)
  assert.match(markup, /보존된 값/)
  assert.doesNotMatch(markup, /answer-evidence-summary-table/)
})

test('falls back to the full compact renderer when a registered source has no recognized summary fields', () => {
  const unrecognizedClinicalCatalog: EvidenceDisplayCatalog = {
    'ct:NCT1': {
      evidence_id: 'ct:NCT1', source_name: 'ClinicalTrials.gov', identifier: 'NCT1', query: 'pitavastatin',
      counts: { received: 1, direct_related: 1 }, record: { 원본전용필드: '보존된 임상 값' },
    },
  }
  const markup = renderToStaticMarkup(createElement(EvidencePopover, {
    evidenceId: 'ct:NCT1', evidence: [{ evidenceId: 'ct:NCT1', label: '출처: ClinicalTrials.gov NCT1' }], catalog: unrecognizedClinicalCatalog,
    onClose: () => undefined, onSelectEvidence: () => undefined,
  }))

  assert.match(markup, /원본전용필드/)
  assert.match(markup, /보존된 임상 값/)
  assert.doesNotMatch(markup, /answer-evidence-summary-table/)
})
