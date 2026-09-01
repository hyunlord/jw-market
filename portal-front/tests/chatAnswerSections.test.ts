import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import {
  createInitialSectionState,
  hasCollapsedSection,
  orderChatAnswerSections,
  parseChatAnswerSections,
  partitionChatAnswerSections,
  reconstructChatAnswerMarkdown,
  setAllSectionsExpanded,
  shouldUseSectionCollapse,
} from '../src/utils/chatAnswerSections.ts'
import { parseMarketAnswerSources } from '../src/utils/marketSources.ts'

const FIXTURE_ROOT = new URL('./fixtures/chat-answer-collapse/', import.meta.url)

async function fixture(name: string): Promise<string> {
  return readFile(new URL(name, FIXTURE_ROOT), 'utf8')
}

function currentMarkerlessOrder(markdown: string): string[] {
  const marketAnswer = parseMarketAnswerSources(markdown)
  const parsed = parseChatAnswerSections(marketAnswer.bodyMarkdown)
  return orderChatAnswerSections(parsed, { hasSources: marketAnswer.sources.length > 0 })
    .map(item => item.type === 'source' ? '출처' : item.section.displayTitle)
}

function orderLabels(
  markdown: string,
  options: Parameters<typeof orderChatAnswerSections>[1],
): string[] {
  return orderChatAnswerSections(parseChatAnswerSections(markdown), options)
    .map(item => item.type === 'source' ? '출처' : item.section.displayTitle)
}

test('real response fixtures put narrative before sources and every data section after sources', async () => {
  const cases = [
    {
      fixture: 'rivaroxaban-clinical.md',
      expected: [
        '핵심 답', '종합 인사이트', '미확인 요소', '출처',
        '조사 범위와 완전성', '단계 및 상태 집계', '회사 및 제품별 그룹',
        '임상시험 전건', '주요 임상시험 건별 상세',
        '건강보험심사평가원 보조 자료', 'FDA 보조 자료', '웹 뉴스 보조 자료',
      ],
    },
    {
      fixture: 'rivaroxaban-patents.md',
      expected: [
        '핵심 답', '뉴스 맥락', '근거와 맥락', '종합 인사이트', '해석 상한',
        '미확인 요소', '출처', '조사 범위와 완전성',
        '국내 NeDrug 특허목록 정본', '미국 Orange Book 보조표',
      ],
    },
    { fixture: 'nct05151731-design.md', expected: ['출처'] },
  ]

  for (const item of cases) {
    const actual = currentMarkerlessOrder(await fixture(item.fixture))
    if (JSON.stringify(actual) !== JSON.stringify(item.expected)) {
      console.error(`ORDER_ASSERT fixture=${item.fixture}`)
      console.error(`actual=${JSON.stringify(actual)}`)
      console.error(`expected=${JSON.stringify(item.expected)}`)
    }
    assert.deepEqual(actual, item.expected)
  }
})

test('backend ordering and failure-injection inputs expose deterministic final arrays', () => {
  const backendBody = [
    '## 핵심 답', '답입니다.', '',
    '## 상세 표 (2건)', '| 값 |', '| --- |', '| 1 |', '',
    '## 알 수 없는 헤딩', '보존합니다.',
  ].join('\n')
  const backendOptions = { hasSources: true, backendOrdered: true, sourceSectionIndex: 1 }

  assert.deepEqual(orderLabels(backendBody, backendOptions), ['핵심 답', '출처', '상세 표', '알 수 없는 헤딩'])
  assert.deepEqual(orderLabels(backendBody, backendOptions), ['핵심 답', '출처', '상세 표', '알 수 없는 헤딩'])
  assert.deepEqual(orderLabels('헤딩 없는 짧은 답', { hasSources: true }), ['출처'])
  assert.deepEqual(
    orderLabels('## 핵심 답\n답\n\n## 알 수 없는 헤딩\n보존', { hasSources: false }),
    ['핵심 답', '알 수 없는 헤딩'],
  )
})

test('a heading with no body is omitted while following content remains byte-identical', () => {
  // Given: an empty heading followed by one section with content.
  const markdown = '## 핵심 답\n\n## 종합 인사이트\n실제 본문'

  // When: the answer is split into renderable sections.
  const parsed = parseChatAnswerSections(markdown)

  // Then: the empty shell is absent and the supplied body is unchanged.
  assert.deepEqual(parsed.sections.map(section => section.title), ['종합 인사이트'])
  assert.equal(parsed.sections[0]?.bodyMarkdown, '실제 본문')
})

test('reordering preserves every parsed section byte for expanded rendering', async () => {
  for (const name of ['rivaroxaban-clinical.md', 'rivaroxaban-patents.md']) {
    const marketAnswer = parseMarketAnswerSources(await fixture(name))
    const parsed = parseChatAnswerSections(marketAnswer.bodyMarkdown)
    const ordered = orderChatAnswerSections(parsed, { hasSources: marketAnswer.sources.length > 0 })
      .filter(item => item.type === 'section')
      .map(item => item.section)
    const originalById = new Map(parsed.sections.map(section => [section.id, section.markdown]))

    assert.equal(ordered.length, parsed.sections.length)
    for (const section of ordered) assert.equal(section.markdown, originalById.get(section.id))
  }
})

test('real patent answer classifies narrative, data, and source sections without changing bytes', async () => {
  const markdown = await fixture('rivaroxaban-patents.md')
  const parsed = parseChatAnswerSections(markdown)

  assert.equal(reconstructChatAnswerMarkdown(parsed), markdown)
  assert.equal(shouldUseSectionCollapse(parsed), true)
  assert.equal(parsed.sections.find(section => section.title === '핵심 답')?.defaultExpanded, true)
  assert.equal(parsed.sections.find(section => section.title === '근거와 맥락')?.defaultExpanded, true)
  assert.equal(parsed.sections.find(section => section.title === '종합 인사이트')?.defaultExpanded, true)
  assert.equal(parsed.sections.find(section => section.title === '조사 범위와 완전성')?.kind, 'data')
  assert.equal(parsed.sections.find(section => section.title === '국내 NeDrug 특허목록 정본')?.defaultExpanded, false)
  assert.equal(parsed.sections.find(section => section.title === '미국 Orange Book 보조표')?.defaultExpanded, false)
  assert.equal(parsed.sections.find(section => section.title === '뉴스 맥락')?.kind, 'narrative')
  assert.equal(parsed.sections.find(section => section.title === '해석 상한')?.kind, 'narrative')
  assert.equal(parsed.sections.find(section => section.title === '미확인 요소')?.kind, 'narrative')
  assert.equal(parsed.sections.find(section => section.title === '출처')?.defaultExpanded, true)
  assert.equal(parsed.sections.find(section => section.title === '출처')?.kind, 'source')
  assert.equal(parsed.sections.find(section => section.title === '조사 범위와 완전성')?.countLabel, undefined)
  assert.equal(parsed.sections.find(section => section.title === '국내 NeDrug 특허목록 정본')?.countLabel, undefined)

  const partitioned = partitionChatAnswerSections(parsed)
  assert.ok(partitioned.narrative.every(section => section.kind !== 'data'))
  assert.ok(partitioned.data.every(section => section.kind === 'data'))
})

test('real clinical answer keeps all detailed records while collapsing known detail sections', async () => {
  const markdown = await fixture('rivaroxaban-clinical.md')
  const parsed = parseChatAnswerSections(markdown)

  assert.equal(reconstructChatAnswerMarkdown(parsed), markdown)
  assert.equal(shouldUseSectionCollapse(parsed), true)
  assert.equal(parsed.sections.find(section => section.title === '임상시험 전건')?.defaultExpanded, false)
  assert.equal(parsed.sections.find(section => section.title === '주요 임상시험 건별 상세 (12건)')?.countLabel, '12건')
  assert.equal(parsed.sections.find(section => section.title === '주요 임상시험 건별 상세 (12건)')?.displayTitle, '주요 임상시험 건별 상세')
  assert.equal(parsed.sections.find(section => section.title === '주요 임상시험 건별 상세 (12건)')?.defaultExpanded, false)
  assert.ok(parsed.sections.find(section => section.title === '임상시험 전건')?.bodyMarkdown.includes('NCT'))
})

test('only an explicit backend heading count is displayed', () => {
  const markdown = [
    '## 핵심 답',
    '답입니다.',
    '',
    '## 임상시험 전건 (12건)',
    '| 시험 | 상태 |',
    '| --- | --- |',
    '| A | 완료 |',
    '| B | 진행 |',
    '',
    '## 미확인 요소',
    '- 하나',
    '- 둘',
  ].join('\n')
  const parsed = parseChatAnswerSections(markdown)

  assert.equal(parsed.sections[1].countLabel, '12건')
  assert.equal(parsed.sections[2].countLabel, undefined)
})

test('short single-section answer remains ordinary markdown without collapse controls', async () => {
  const markdown = await fixture('nct05151731-design.md')
  const parsed = parseChatAnswerSections(markdown)

  assert.equal(reconstructChatAnswerMarkdown(parsed), markdown)
  assert.equal(parsed.sections.length, 1)
  assert.equal(shouldUseSectionCollapse(parsed), false)
})

test('headingless and empty answers remain unchanged', () => {
  for (const markdown of ['', '짧은 답변입니다.', '첫 줄\n\n둘째 줄']) {
    const parsed = parseChatAnswerSections(markdown)
    assert.equal(parsed.sections.length, 0)
    assert.equal(reconstructChatAnswerMarkdown(parsed), markdown)
    assert.equal(shouldUseSectionCollapse(parsed), false)
  }
})

test('empty sections are omitted and non-empty unknown headings default open', () => {
  const markdown = '서문\n\n## 알 수 없는 새 섹션\n\n## 미확인 요소\n- 항목 1\n'
  const parsed = parseChatAnswerSections(markdown)

  assert.equal(parsed.sections.length, 1)
  assert.equal(parsed.sections[0].title, '미확인 요소')
  assert.equal(parsed.sections[0].defaultExpanded, true)
  assert.equal(parsed.sections[0].countLabel, undefined)
})

test('only top-level level-two headings outside fences become section boundaries', () => {
  const markdown = [
    '## 핵심 답',
    '본문',
    '',
    '```md',
    '## 코드 안 헤딩',
    '```',
    '',
    '> ## 인용 안 헤딩',
    '| 값 |',
    '| --- |',
    '| ## 표 안 헤딩 |',
    '### 하위 헤딩',
    '',
    '## 출처',
    '- 출처 A',
  ].join('\n')
  const parsed = parseChatAnswerSections(markdown)

  assert.deepEqual(parsed.sections.map(section => section.title), ['핵심 답', '출처'])
  assert.match(parsed.sections[0].bodyMarkdown, /## 코드 안 헤딩/)
  assert.match(parsed.sections[0].bodyMarkdown, /> ## 인용 안 헤딩/)
  assert.match(parsed.sections[0].bodyMarkdown, /### 하위 헤딩/)
  assert.equal(reconstructChatAnswerMarkdown(parsed), markdown)
})

test('same markdown produces the same ids and initial state', async () => {
  const markdown = await fixture('rivaroxaban-patents.md')
  const first = parseChatAnswerSections(markdown)
  const second = parseChatAnswerSections(markdown)

  assert.deepEqual(second, first)
  assert.deepEqual(createInitialSectionState(second), createInitialSectionState(first))
})

test('expand-all and collapse-all update every section without storage', async () => {
  const parsed = parseChatAnswerSections(await fixture('rivaroxaban-patents.md'))
  const initial = createInitialSectionState(parsed)
  const expanded = setAllSectionsExpanded(parsed, true)
  const collapsed = setAllSectionsExpanded(parsed, false)

  assert.equal(hasCollapsedSection(parsed, initial), true)
  assert.equal(hasCollapsedSection(parsed, expanded), false)
  assert.ok(Object.values(expanded).every(Boolean))
  assert.ok(Object.values(collapsed).every(value => !value))
  assert.deepEqual(Object.keys(expanded), parsed.sections.filter(section => section.kind === 'data').map(section => section.id))
})
