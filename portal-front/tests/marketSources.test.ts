import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test, { after } from 'node:test'
import { createElement, type ComponentType } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import react from '@vitejs/plugin-react'
import { createServer } from 'vite'

import type { MarketSourceItem } from '../src/utils/marketSources.ts'
import {
  getMarketSourceCount,
  parseMarketAnswerSources,
} from '../src/utils/marketSources.ts'

interface SourcesModule {
  MarketSourceCitationText: ComponentType<{
    text: string
    sources: readonly MarketSourceItem[]
    anchorPrefix: string
    onInspectionSourceOpen?: (sourceLabel: string) => void
  }>
  MarketSourcesSection: ComponentType<{
    sources: readonly MarketSourceItem[]
    anchorPrefix: string
  }>
}

const vite = await createServer({
  configFile: false,
  root: new URL('..', import.meta.url).pathname,
  plugins: [react()],
  optimizeDeps: { noDiscovery: true },
  server: { middlewareMode: true, hmr: { port: 24680 } },
  appType: 'custom',
})
const {
  MarketSourceCitationText,
  MarketSourcesSection,
} = await vite.ssrLoadModule('/src/components/main/MarketSources.tsx') as SourcesModule

const MARKET_SOURCES_SOURCE = readFileSync(
  new URL('../src/components/main/MarketSources.tsx', import.meta.url),
  'utf8',
)

after(async () => vite.close())

const LONG_HIRA_URL = 'https://www.hira.or.kr/rc/insu/insuadtcrtr/InsuAdtCrtrDetail.do?mtgHmeDd=2024%EB%85%84&mtgMtrRegSno=42'

const ANSWER = [
  '## 핵심 답',
  '급여 기준입니다 [출처: HIRA, 내부 데이터마트].',
  '',
  '## 출처',
  '- 내부 데이터마트 — "리바로 시장" 내부 지표 조회',
  '- 건강보험심사평가원 — "리바로 급여기준" 고시 검색',
  `  - [hira.or.kr · %EB%A6%AC%EB%B0%94%EB%A1%9C 급여 인정기준](${LONG_HIRA_URL})`,
  '- 특허 자료 — "리바로 특허" 특허 검색',
  '  - [example.com · ""리바로"" 특허](https://example.com/patent-a)',
  '  - [example.org · 리바로 재심사](https://example.org/patent-b)',
].join('\n')

test('source parser removes the markdown source block and retains complete hrefs', () => {
  const parsed = parseMarketAnswerSources(ANSWER)

  assert.equal(parsed.bodyMarkdown, '## 핵심 답\n급여 기준입니다 [출처: HIRA, 내부 데이터마트].')
  assert.equal(parsed.sourceSectionIndex, 1)
  assert.equal(parsed.sources.length, 3)
  assert.equal(parsed.sources[0].links.length, 0)
  assert.equal(parsed.sources[1].links[0].href, LONG_HIRA_URL)
  assert.equal(parsed.sources[1].links[0].displayText, 'hira.or.kr · 리바로 급여 인정기준')
  assert.equal(parsed.sources[2].links[0].displayText, 'example.com · "리바로" 특허')
})

test('source parser preserves backend-ordered sections that follow the source block', () => {
  const markdown = [
    '## 핵심 답',
    '답입니다.',
    '',
    '## 출처',
    '- 내부 데이터마트 — "리바로" 조회',
    '',
    '## 상세 표 (2건)',
    '| 값 |',
    '| --- |',
    '| 1 |',
  ].join('\n')
  const parsed = parseMarketAnswerSources(markdown)

  assert.equal(parsed.sourceSectionIndex, 1)
  assert.equal(parsed.bodyMarkdown, [
    '## 핵심 답',
    '답입니다.',
    '',
    '## 상세 표 (2건)',
    '| 값 |',
    '| --- |',
    '| 1 |',
  ].join('\n'))
  assert.equal(parsed.sources.length, 1)
})

test('structured sources render external links and never invent an internal link', () => {
  const parsed = parseMarketAnswerSources(ANSWER)
  const markup = renderToStaticMarkup(
    createElement(MarketSourcesSection, {
      sources: parsed.sources,
      anchorPrefix: 'market-source-answer-1',
    }),
  )

  const renderedHref = LONG_HIRA_URL.replaceAll('&', '&amp;')
  assert.match(markup, new RegExp(`href="${renderedHref.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}"`))
  assert.match(markup, /target="_blank"/)
  assert.match(markup, /rel="noopener noreferrer"/)
  assert.match(markup, /hira\.or\.kr · 리바로 급여 인정기준/)
  assert.doesNotMatch(markup, /%EB%A6%AC%EB%B0%94%EB%A1%9C 급여 인정기준/)

  const internalItem = markup.slice(markup.indexOf('내부 데이터마트'), markup.indexOf('건강보험심사평가원'))
  assert.doesNotMatch(internalItem, /<a\b/)
  assert.doesNotMatch(markup, /\[[^\]]+\]\(https?:\/\//)
})

test('real clinical sources keep the source block open while collapsing each link list', () => {
  const markdown = readFileSync(
    new URL('./fixtures/chat-answer-collapse/rivaroxaban-clinical.md', import.meta.url),
    'utf8',
  )
  const parsed = parseMarketAnswerSources(markdown)
  const markup = renderToStaticMarkup(
    createElement(MarketSourcesSection, {
      sources: parsed.sources,
      anchorPrefix: 'clinical-answer',
    }),
  )

  assert.equal(parsed.sources[1].links.length, 583)
  assert.match(markup, /aria-label="출처"/)
  assert.match(markup, />출처 모두 펼치기</)
  assert.match(markup, /aria-expanded="false"/)
  assert.match(markup, /data-count-origin="derived"[^>]*>583건</)
  assert.match(markup, /class="market-source-links"[^>]*hidden=""/)
  assert.match(markup, /NCT04056169/)
  assert.match(markup, /https:\/\/clinicaltrials\.gov\/study\/NCT04056169/)
  assert.match(markup, /외 582건/)
})

test('source summary is one lightweight line and maps internal lane labels', () => {
  const parsed = parseMarketAnswerSources([
    '본문',
    '',
    '## 출처',
    '- aux:patent:news',
    '  - [첫 특허 뉴스](https://example.com/a)',
    '  - [둘째 특허 뉴스](https://example.com/b)',
    '  - [셋째 특허 뉴스](https://example.com/c)',
  ].join('\n'))
  const markup = renderToStaticMarkup(createElement(MarketSourcesSection, {
    sources: parsed.sources,
    anchorPrefix: 'friendly-source',
  }))

  assert.equal(parsed.sources[0]?.label, '특허 뉴스')
  assert.doesNotMatch(markup, /aux:/)
  assert.match(markup, /href="https:\/\/example\.com\/a"/)
  assert.match(markup, /외 2건/)
  assert.match(markup, /class="market-source-links"[^>]*hidden=""/)
})

test('top-level markdown links are grouped under one user-facing source', () => {
  const parsed = parseMarketAnswerSources([
    '본문',
    '',
    '## 출처',
    '- [NCT00000001 · 시험 A](https://clinicaltrials.gov/study/NCT00000001)',
    '- [NCT00000002 · 시험 B](https://clinicaltrials.gov/study/NCT00000002)',
    '- [medicaltimes.com · [메디칼타임즈] 기사](https://medicaltimes.com/news/1)',
  ].join('\n'))

  assert.equal(parsed.sources.length, 2)
  assert.equal(parsed.sources[0]?.label, 'ClinicalTrials.gov')
  assert.equal(parsed.sources[0]?.links.length, 2)
  const markup = renderToStaticMarkup(createElement(MarketSourcesSection, { sources: parsed.sources, anchorPrefix: 'flat-links' }))
  assert.doesNotMatch(markup, /\]\(http/)
  assert.match(markup, /외 1건/)
  assert.match(markup, /\[메디칼타임즈]/)
})

test('linkless patent sources remain visible without empty collapse panels', () => {
  const markdown = readFileSync(
    new URL('./fixtures/chat-answer-collapse/rivaroxaban-patents.md', import.meta.url),
    'utf8',
  )
  const parsed = parseMarketAnswerSources(markdown)
  const markup = renderToStaticMarkup(
    createElement(MarketSourcesSection, {
      sources: parsed.sources,
      anchorPrefix: 'patent-answer',
    }),
  )

  assert.equal(parsed.sources.filter(source => source.links.length === 0).length, 4)
  assert.equal((markup.match(/market-source-toggle/g) ?? []).length, 1)
  assert.equal((markup.match(/class="market-source-links"/g) ?? []).length, 1)
  assert.match(markup, /내부 데이터마트/)
  assert.match(markup, /식품의약품안전처 의약품 특허목록/)
  assert.doesNotMatch(markup, />0건</)
})

test('source counts distinguish backend values from derived link counts', () => {
  const source: MarketSourceItem = {
    label: 'ClinicalTrials.gov',
    links: [{ href: 'https://clinicaltrials.gov/study/NCT05151731', displayText: 'NCT05151731' }],
  }

  assert.deepEqual(getMarketSourceCount(source), { value: 1, origin: 'derived' })
  assert.deepEqual(getMarketSourceCount({ ...source, recordCount: 12 }), { value: 12, origin: 'backend' })
  assert.equal(getMarketSourceCount({ label: '내부 데이터마트', links: [] }), undefined)
})

test('inline source labels split and choose direct URL or source anchor by URL count', () => {
  const parsed = parseMarketAnswerSources(ANSWER)
  const markup = renderToStaticMarkup(
    createElement(
      'p',
      null,
      createElement(MarketSourceCitationText, {
        text: '[출처: HIRA, 내부 데이터마트] [출처: 특허]',
        sources: parsed.sources,
        anchorPrefix: 'market-source-answer-1',
      }),
    ),
  )

  const renderedHref = LONG_HIRA_URL.replaceAll('&', '&amp;')
  assert.match(markup, new RegExp(`href="${renderedHref.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}"[^>]*>HIRA</a>`))
  assert.match(markup, /href="#market-source-answer-1-0"[^>]*>내부 데이터마트<\/a>/)
  assert.match(markup, /href="#market-source-answer-1-2"[^>]*>특허<\/a>/)
})

test('inline source labels target inspection lanes without changing source-list external links', () => {
  const parsed = parseMarketAnswerSources(ANSWER)
  const inlineMarkup = renderToStaticMarkup(createElement(MarketSourceCitationText, {
    text: '[출처: HIRA, 내부 데이터마트, 특허]',
    sources: parsed.sources,
    anchorPrefix: 'market-source-answer-1',
    onInspectionSourceOpen: () => undefined,
  }))
  const sourceListMarkup = renderToStaticMarkup(createElement(MarketSourcesSection, {
    sources: parsed.sources,
    anchorPrefix: 'market-source-answer-1',
  }))

  assert.equal((inlineMarkup.match(/href="#answer-inspection-panel"/g) ?? []).length, 3)
  assert.match(inlineMarkup, /data-inspection-source-label="HIRA"/)
  assert.match(inlineMarkup, /data-inspection-source-label="내부 데이터마트"/)
  assert.match(inlineMarkup, /data-inspection-source-label="특허"/)
  assert.doesNotMatch(inlineMarkup, /target="_blank"/)
  assert.match(sourceListMarkup, new RegExp(`href="${LONG_HIRA_URL.replaceAll('&', '&amp;').replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}"`))
})

test('unsafe and cluster-internal URLs are not rendered as external links', () => {
  const parsed = parseMarketAnswerSources([
    '본문',
    '',
    '## 출처',
    '- 식품의약품안전처 — "허가" 검색',
    '  - [내부 MCP](http://mcp-nedrug-standby-svc:8080/json)',
    '  - [위험](javascript:alert(1))',
  ].join('\n'))

  assert.equal(parsed.sources[0].links.length, 0)
})

test('source heading inside a fenced code block is not extracted', () => {
  const markdown = [
    '## 핵심 답',
    '본문',
    '',
    '```md',
    '## 출처',
    '- 코드 예시',
    '```',
  ].join('\n')

  assert.deepEqual(parseMarketAnswerSources(markdown), {
    bodyMarkdown: markdown,
    sources: [],
  })
})

test('legacy backend failure labels are replaced only at the display boundary', () => {
  const parsed = parseMarketAnswerSources('조회 실패\n\n자동 분류 실패 · 공식 원문 표시')

  assert.equal(parsed.bodyMarkdown, '자료를 가져오지 못했습니다\n\n분류 결과 없이 공식 원문을 표시합니다')
})

test('inline source navigation expands a collapsed source before highlighting it', () => {
  assert.match(MARKET_SOURCES_SOURCE, /market-source-toggle/)
  assert.match(MARKET_SOURCES_SOURCE, /aria-expanded="false"/)
  assert.match(MARKET_SOURCES_SOURCE, /\.click\(\)/)
  assert.match(MARKET_SOURCES_SOURCE, /scrollIntoView/)
})

test('source collapse state is React-local and never persisted in browser storage', () => {
  assert.match(MARKET_SOURCES_SOURCE, /useState/)
  assert.doesNotMatch(MARKET_SOURCES_SOURCE, /localStorage|sessionStorage/)
})
