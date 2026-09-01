import assert from 'node:assert/strict'
import test, { after } from 'node:test'
import { createElement, type ComponentType } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import react from '@vitejs/plugin-react'
import { createServer } from 'vite'

import type { AnswerSectionState } from '../src/utils/answerSections.ts'

interface SectionSlotAnswerProps {
  sections: readonly AnswerSectionState[]
  components: Record<string, never>
}

const vite = await createServer({
  configFile: false,
  root: new URL('..', import.meta.url).pathname,
  plugins: [react()],
  optimizeDeps: { noDiscovery: true },
  server: { middlewareMode: true, hmr: { port: 24691 } },
  appType: 'custom',
})
const { default: SectionSlotAnswer } = await vite.ssrLoadModule('/src/components/main/SectionSlotAnswer.tsx') as {
  default: ComponentType<SectionSlotAnswerProps>
}

after(async () => vite.close())

test('facts uses its metadata title while answer and insight remain headingless', () => {
  const sections: AnswerSectionState[] = [
    { id: 'facts', order: 2, kind: 'facts', title: '검증된 조사 결과', status: 'complete', parts: [{ type: 'text', text: '사실 본문' }] },
    { id: 'answer', order: 0, kind: 'answer', title: '직답 제목', status: 'complete', parts: [{ type: 'text', text: '직답 본문' }] },
    { id: 'insight', order: 1, kind: 'insight', title: '인사이트 제목', status: 'complete', parts: [{ type: 'text', text: '인사이트 본문' }] },
  ]
  const markup = renderToStaticMarkup(createElement(SectionSlotAnswer, { sections, components: {} }))

  assert.match(markup, /<h2>검증된 조사 결과<\/h2>/)
  assert.doesNotMatch(markup, /직답 제목/)
  assert.doesNotMatch(markup, /인사이트 제목/)
  assert.ok(markup.indexOf('직답 본문') < markup.indexOf('인사이트 본문'))
  assert.ok(markup.indexOf('인사이트 본문') < markup.indexOf('사실 본문'))
})

test('facts falls back to the canonical title when metadata is absent or blank', () => {
  for (const title of [undefined, '   ']) {
    const sections: AnswerSectionState[] = [{
      id: 'facts', order: 0, kind: 'facts', title, status: 'complete', parts: [{ type: 'text', text: '사실 본문' }],
    }]
    const markup = renderToStaticMarkup(createElement(SectionSlotAnswer, { sections, components: {} }))

    assert.match(markup, /<h2>조사 결과<\/h2>/)
  }
})
