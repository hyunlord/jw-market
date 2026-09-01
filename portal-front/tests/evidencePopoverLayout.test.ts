import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'

import {
  createEvidencePopoverViewKey,
  emptyEvidencePopoverViewState,
  setEvidenceDetailExpanded,
  setEvidenceLongFieldExpanded,
} from '../src/utils/evidencePopoverViewState.ts'

test('popover uses a bounded large viewport surface with independently scrolling content', async () => {
  const css = await readFile(new URL('../src/styles/common.css', import.meta.url), 'utf8')
  const source = await readFile(new URL('../src/components/main/EvidencePopover.tsx', import.meta.url), 'utf8')

  assert.match(css, /width:\s*clamp\(560px,\s*62vw,\s*900px\)/)
  assert.match(css, /height:\s*clamp\(600px,\s*85dvh,\s*900px\)/)
  assert.match(css, /grid-template-rows:\s*auto minmax\(0,\s*1fr\) auto/)
  assert.match(css, /\.answer-evidence-popover-scroll[^{]*\{[^}]*overflow-y:\s*auto/s)
  assert.match(css, /\.answer-evidence-popover-footer/)
  assert.match(css, /\.market-detail-long-value pre[^{]*\{[^}]*max-width:\s*68ch[^}]*overflow:\s*auto/s)
  assert.match(css, /@media \(max-width:\s*720px\)/)
  assert.match(source, /answer-evidence-popover-scroll/)
  assert.match(source, /answer-evidence-popover-footer/)
})

test('detail fields reserve readable label and value widths while stacking on narrow screens', async () => {
  const css = await readFile(new URL('../src/styles/common.css', import.meta.url), 'utf8')

  assert.match(css, /\.market-detail-fields\s*>\s*div\s*\{[^}]*grid-template-columns:\s*minmax\(210px,\s*36%\)\s+minmax\(280px,\s*1fr\)/s)
  assert.match(css, /\.market-detail-field-primary[^{]*\{[^}]*-webkit-line-clamp:\s*2/s)
  assert.match(css, /@media \(max-width:\s*720px\)[\s\S]*\.market-detail-fields\s*>\s*div\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\)/s)
})

test('keeps scalar labels and their colon together while allowing values to wrap', async () => {
  const css = await readFile(new URL('../src/styles/common.css', import.meta.url), 'utf8')

  assert.match(css, /\.trace-output-scalar \.trace-output-field-label \{[^}]*display:\s*inline-flex/s)
  assert.match(css, /\.trace-output-scalar \.trace-output-field-label::after \{[^}]*white-space:\s*nowrap/s)
})

test('detail and long-field expansion state survives popover remounts under a stable response key', () => {
  const key = createEvidencePopoverViewKey({
    conversationId: 'conversation-1',
    responseId: 'response-1',
    itemKey: 'openfda:1:1:1',
  })
  let state = emptyEvidencePopoverViewState()
  state = setEvidenceDetailExpanded(state, true)
  state = setEvidenceLongFieldExpanded(state, 'payload.calls[0].render_data.mcp.content_text', true)

  assert.equal(key, 'conversation-1\u0000response-1\u0000openfda:1:1:1')
  assert.equal(state.detailExpanded, true)
  assert.deepEqual([...state.expandedLongFields], ['payload.calls[0].render_data.mcp.content_text'])
  assert.equal(setEvidenceLongFieldExpanded(state, 'payload.calls[0].render_data.mcp.content_text', false).expandedLongFields.size, 0)
})
