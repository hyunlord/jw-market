import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'

const tree = await readFile(new URL('../src/components/main/StructuredValueTree.tsx', import.meta.url), 'utf8').catch(() => '')
const detail = await readFile(new URL('../src/components/main/MarketDetailView.tsx', import.meta.url), 'utf8')

test('restores the historical recursive trace tree for source detail', () => {
  assert.match(tree, /trace-output-object/)
  assert.match(tree, /trace-record-block/)
  assert.match(tree, /StructuredValueTree/)
  assert.match(detail, /StructuredValueTree/)
  assert.doesNotMatch(detail, /detailLeaves\(/)
})
