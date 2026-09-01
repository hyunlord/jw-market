import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { readFile } from 'node:fs/promises'
import test, { after } from 'node:test'
import { createElement, type ComponentType, type ReactNode } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import react from '@vitejs/plugin-react'
import { createServer } from 'vite'
import { parseMarketAnswerSources } from '../src/utils/marketSources.ts'

interface CollapsibleAnswerProps {
  markdown: string
  components: Record<string, never>
  idPrefix: string
  sourceCount: number
  renderSources: ReactNode
  collapseEnabled: boolean
  backendOrdered?: boolean
  sourceSectionIndex?: number
}

const vite = await createServer({
  configFile: false,
  root: new URL('..', import.meta.url).pathname,
  plugins: [react()],
  optimizeDeps: { noDiscovery: true },
  server: { middlewareMode: true, hmr: { port: 24679 } },
  appType: 'custom',
})
const { default: CollapsibleAnswerMarkdown } = await vite.ssrLoadModule(
  '/src/components/main/CollapsibleAnswerMarkdown.tsx',
) as { default: ComponentType<CollapsibleAnswerProps> }

after(async () => vite.close())

async function patentFixture(): Promise<string> {
  return readFile(
    new URL('./fixtures/chat-answer-collapse/rivaroxaban-patents.md', import.meta.url),
    'utf8',
  )
}

async function fixture(name: string): Promise<string> {
  return readFile(new URL(`./fixtures/chat-answer-collapse/${name}`, import.meta.url), 'utf8')
}

test('markerless answer renders narrative first, expanded sources second, then collapsed data', async () => {
  const parsed = parseMarketAnswerSources(await patentFixture())
  const markup = renderToStaticMarkup(createElement(CollapsibleAnswerMarkdown, {
    markdown: parsed.bodyMarkdown,
    components: {},
    idPrefix: 'answer-r12-4a',
    sourceCount: parsed.sources.length,
    sourceSectionIndex: parsed.sourceSectionIndex,
    collapseEnabled: true,
    renderSources: createElement('div', { 'data-testid': 'sources' }, '구조화 출처'),
  }))

  assert.equal(markup.match(/모두 펼치기/g)?.length, 1)
  assert.match(markup, /<h2>핵심 답<\/h2>/)
  assert.match(markup, /aria-expanded="false"[^>]*><span>조사 범위와 완전성<\/span>/)
  assert.match(markup, /aria-expanded="false"[^>]*><span>국내 NeDrug 특허목록 정본<\/span>/)
  assert.doesNotMatch(markup, /answer-section-count">(?:17건|7건|7개 소스)/)
  assert.match(markup, /<h2 class="answer-sources-heading">출처<\/h2><div data-testid="sources">구조화 출처<\/div>/)
  assert.equal(markup.match(/answer-sources-heading/g)?.length, 1)
  assert.match(markup, /hidden=""/)
  assert.match(markup, /<h2>뉴스 맥락<\/h2>/)
  assert.ok(markup.indexOf('<h2>핵심 답</h2>') < markup.indexOf('answer-sources-heading'))
  assert.ok(markup.indexOf('answer-sources-heading') < markup.indexOf('answer-sections-toolbar'))
  assert.ok(markup.indexOf('answer-sections-toolbar') < markup.indexOf('<span>조사 범위와 완전성</span>'))
})

test('backend-ordered branch preserves the source slot and does not reorder data twice', () => {
  const markdown = [
    '## 핵심 답',
    '답이 먼저 보입니다.',
    '',
    '## 상세 표 (2건)',
    '| 값 |',
    '| --- |',
    '| 1 |',
  ].join('\n')
  const markup = renderToStaticMarkup(createElement(CollapsibleAnswerMarkdown, {
    markdown,
    components: {},
    idPrefix: 'answer-backend-marker',
    sourceCount: 1,
    sourceSectionIndex: 1,
    backendOrdered: true,
    collapseEnabled: true,
    renderSources: createElement('div', { 'data-testid': 'sources' }, '구조화 출처'),
  }))

  assert.ok(markup.indexOf('<h2>핵심 답</h2>') < markup.indexOf('answer-sources-heading'))
  assert.ok(markup.indexOf('answer-sources-heading') < markup.indexOf('<span>상세 표</span>'))
  assert.equal(markup.match(/구조화 출처/g)?.length, 1)
  assert.match(markup, /<span>상세 표<\/span><span class="answer-section-count">2건<\/span>/)
})

test('short real answer keeps a visible source heading without data collapse controls', async () => {
  const parsed = parseMarketAnswerSources(await fixture('nct05151731-design.md'))
  const markup = renderToStaticMarkup(createElement(CollapsibleAnswerMarkdown, {
    markdown: parsed.bodyMarkdown,
    components: {},
    idPrefix: 'answer-short-source',
    sourceCount: parsed.sources.length,
    sourceSectionIndex: parsed.sourceSectionIndex,
    collapseEnabled: true,
    renderSources: createElement('div', { 'data-testid': 'sources' }, '구조화 출처'),
  }))

  assert.match(markup, /<h2 class="answer-sources-heading">출처<\/h2>/)
  assert.doesNotMatch(markup, /answer-sections-toolbar/)
})

test('headingless answer and fenced heading render as ordinary Markdown without collapse controls', () => {
  const markdown = ['짧은 답', '', '```md', '## 코드 안 헤딩', '```'].join('\n')
  const markup = renderToStaticMarkup(createElement(CollapsibleAnswerMarkdown, {
    markdown,
    components: {},
    idPrefix: 'answer-fence',
    sourceCount: 0,
    collapseEnabled: true,
    renderSources: null,
  }))

  assert.doesNotMatch(markup, /모두 펼치기|모두 접기/)
  assert.match(markup, /<pre><code class="language-md">## 코드 안 헤딩/)
  assert.doesNotMatch(markup, /<h2>코드 안 헤딩<\/h2>/)
})

test('collapse component keeps state in React and contains no browser storage path', () => {
  const source = readFileSync(
    new URL('../src/components/main/CollapsibleAnswerMarkdown.tsx', import.meta.url),
    'utf8',
  )

  assert.match(source, /useState/)
  assert.doesNotMatch(source, /localStorage|sessionStorage/)
})
