import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import type { Root } from 'hast'

import {
  portalMarkdownRemarkPlugins,
  portalMarkdownRehypePlugins,
  portalMarkdownSchema,
} from '../src/utils/markdownSanitize.ts'

const CHAT_MESSAGE_SOURCE = readFileSync(
  new URL('../src/components/main/ChatMessageAI.tsx', import.meta.url),
  'utf8',
)
const COLLAPSIBLE_ANSWER_SOURCE = readFileSync(
  new URL('../src/components/main/CollapsibleAnswerMarkdown.tsx', import.meta.url),
  'utf8',
)

function injectUnsafeHast() {
  return (tree: Root) => {
    tree.children.push(
      {
        type: 'element',
        tagName: 'script',
        properties: {},
        children: [{ type: 'text', value: 'alert(1)' }],
      },
      {
        type: 'element',
        tagName: 'img',
        properties: { src: 'x', onError: 'alert(1)' },
        children: [],
      },
      {
        type: 'element',
        tagName: 'a',
        properties: { href: 'javascript:alert(1)', onClick: 'alert(1)' },
        children: [{ type: 'text', value: '주입 링크' }],
      },
    )
  }
}

test('react-markdown baseline does not activate raw HTML', () => {
  const markdown = [
    '<script>alert(1)</script>',
    '<img src=x onerror=alert(1)>',
    '',
    '**정상 강조**',
  ].join('\n')

  const rendered = renderToStaticMarkup(
    createElement(ReactMarkdown, { remarkPlugins: [remarkGfm] }, markdown),
  )

  assert.doesNotMatch(rendered, /<script\b|<img\b/i)
  assert.match(rendered, /&lt;script&gt;alert\(1\)&lt;\/script&gt;/)
  assert.match(rendered, /&lt;img src=x onerror=alert\(1\)&gt;/)
  assert.match(rendered, /<strong>정상 강조<\/strong>/)
})

test('ChatMessageAI explicitly sanitizes every Markdown render sink', () => {
  const renderSources = [CHAT_MESSAGE_SOURCE, COLLAPSIBLE_ANSWER_SOURCE]
  assert.match(CHAT_MESSAGE_SOURCE, /from '\.\.\/\.\.\/utils\/markdownSanitize'/)
  assert.match(COLLAPSIBLE_ANSWER_SOURCE, /from '\.\.\/\.\.\/utils\/markdownSanitize'/)
  assert.ok(renderSources.every(source => !source.includes("from 'rehype-raw'")))

  const sanitizeBindings = renderSources.flatMap(source => (
    source.match(/rehypePlugins=\{portalMarkdownRehypePlugins\}/g) ?? []
  ))
  const remarkBindings = renderSources.flatMap(source => (
    source.match(/remarkPlugins=\{portalMarkdownRemarkPlugins\}/g) ?? []
  ))
  const markdownSinks = renderSources.flatMap(source => source.match(/<ReactMarkdown\b/g) ?? [])

  assert.equal(sanitizeBindings.length, markdownSinks.length)
  assert.equal(remarkBindings.length, markdownSinks.length)
  assert.equal(markdownSinks.length, 2)
})

test('portal sanitizer strips disallowed elements attributes and protocols', () => {
  assert.equal(portalMarkdownSchema.tagNames?.includes('script'), false)
  assert.equal(portalMarkdownSchema.tagNames?.includes('iframe'), false)
  assert.equal(portalMarkdownSchema.attributes?.img, undefined)
  assert.deepEqual(portalMarkdownSchema.protocols?.href, ['http', 'https', 'mailto'])

  const rawMarkdownRendered = renderToStaticMarkup(
    createElement(
      ReactMarkdown,
      {
        rehypePlugins: portalMarkdownRehypePlugins,
        remarkPlugins: [remarkGfm],
      },
      [
        '<script>alert(1)</script>',
        '<iframe src="https://example.com"></iframe>',
        '<img src=x onerror=alert(1)>',
        '[위험 링크](javascript:alert(1))',
      ].join('\n\n'),
    ),
  )

  assert.doesNotMatch(
    rawMarkdownRendered,
    /<script\b|<iframe\b|<img\b|onerror=|javascript:/i,
  )
  assert.match(rawMarkdownRendered, /<a>위험 링크<\/a>/)

  const injectedHastRendered = renderToStaticMarkup(
    createElement(
      ReactMarkdown,
      {
        rehypePlugins: [injectUnsafeHast, ...portalMarkdownRehypePlugins],
      },
      '정상 본문',
    ),
  )

  assert.doesNotMatch(
    injectedHastRendered,
    /<script\b|<img\b|onerror=|onclick=|javascript:/i,
  )
  assert.match(injectedHastRendered, /<a>주입 링크<\/a>/)
})

test('portal sanitizer preserves supported answer Markdown exactly', () => {
  const markdown = [
    '# 시장 요약',
    '',
    '**핵심**과 *보조 설명*',
    '',
    '- 첫 번째',
    '- 두 번째',
    '',
    '| 브랜드 | 매출 |',
    '| --- | ---: |',
    '| 리바로 | 80.39억원 |',
    '',
    '`inline`',
    '',
    '```ts',
    'const safe = true',
    '```',
    '',
    '[근거](https://example.com/source)',
  ].join('\n')

  const baseline = renderToStaticMarkup(
    createElement(ReactMarkdown, { remarkPlugins: [remarkGfm] }, markdown),
  )
  const sanitized = renderToStaticMarkup(
    createElement(
      ReactMarkdown,
      {
        rehypePlugins: portalMarkdownRehypePlugins,
        remarkPlugins: [remarkGfm],
      },
      markdown,
    ),
  )

  assert.equal(sanitized, baseline)
})

test('chat markdown preserves single tildes in age ranges without disabling explicit strikethrough', () => {
  const markdown = '0~9세 · 10~19세 · 20~29세 · 30~39세 · 40~49세 · 50~59세\n\n~~의도한 취소선~~'
  const rendered = renderToStaticMarkup(createElement(
    ReactMarkdown,
    { remarkPlugins: portalMarkdownRemarkPlugins, rehypePlugins: portalMarkdownRehypePlugins },
    markdown,
  ))

  assert.match(rendered, /0~9세 · 10~19세 · 20~29세 · 30~39세 · 40~49세 · 50~59세/)
  assert.match(rendered, /<del>의도한 취소선<\/del>/)
  assert.doesNotMatch(rendered, /<del>9세/)
})

test('chat markdown preserves every age-range tilde from the live E-9 answer', () => {
  const markdown = readFileSync(new URL('./fixtures/r52-live/e9-turn2.md', import.meta.url), 'utf8')
  const rendered = renderToStaticMarkup(createElement(
    ReactMarkdown,
    { remarkPlugins: portalMarkdownRemarkPlugins, rehypePlugins: portalMarkdownRehypePlugins },
    markdown,
  ))

  for (const range of ['0~9세', '10~19세', '20~29세', '30~39세', '40~49세']) {
    assert.ok(markdown.includes(range))
    assert.ok(rendered.includes(range))
  }
  assert.doesNotMatch(rendered, /<del>9세/)
})

test('chat markdown keeps unmatched data punctuation as supplied', () => {
  const markdown = 'A_B · A*B · A`B · A#B · A|B · [A] (B)'
  const rendered = renderToStaticMarkup(createElement(
    ReactMarkdown,
    { remarkPlugins: portalMarkdownRemarkPlugins, rehypePlugins: portalMarkdownRehypePlugins },
    markdown,
  ))

  assert.match(rendered, /A_B · A\*B · A`B · A#B · A\|B · \[A\] \(B\)/)
})
