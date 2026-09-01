import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import {
  isSameAtc4Scope,
  resolveOptionLoadAtcSelection,
  type AtcLevelSelection,
} from '../src/utils/analyzeFilterSelection.ts'

const defaults: AtcLevelSelection = {
  atc1: ['C'],
  atc2: ['C10'],
  atc3: ['C10C'],
  atc4: ['C10C'],
}

function afterResultRefresh(appliedAtc4: string[]) {
  const operatorSelection = {
    atc1: [...new Set(appliedAtc4.map(code => code.slice(0, 1)))],
    atc2: [...new Set(appliedAtc4.map(code => code.slice(0, 3)))],
    atc3: [...new Set(appliedAtc4.map(code => code.slice(0, 4)))],
    atc4: appliedAtc4,
  }
  return resolveOptionLoadAtcSelection({
    sameContext: true,
    defaults,
  }) ?? operatorSelection
}

test('keeps a single operator ATC4 selection after one result refresh', () => {
  assert.deepEqual(afterResultRefresh(['C10A1']), {
    atc1: ['C'],
    atc2: ['C10'],
    atc3: ['C10A'],
    atc4: ['C10A1'],
  })
})

test('keeps the canonical selection across three consecutive searches', () => {
  const searches = [
    ['C10A1', 'C10A2'],
    ['C10A3', 'C10A4'],
    ['C10A9', 'C10C'],
  ]

  assert.deepEqual(
    searches.map(atc4 => afterResultRefresh(atc4)?.atc4),
    searches,
  )
})

test('keeps deselected ATC4 entries absent after another search', () => {
  assert.deepEqual(
    afterResultRefresh(['C10A1', 'C10A3', 'C10C'])?.atc4,
    ['C10A1', 'C10A3', 'C10C'],
  )
})

test('derives mixed ATC levels from the canonical ATC4 selection', () => {
  assert.deepEqual(afterResultRefresh(['C10A1', 'C10C']), {
    atc1: ['C'],
    atc2: ['C10'],
    atc3: ['C10A', 'C10C'],
    atc4: ['C10A1', 'C10C'],
  })
})

test('uses the parent applied filter to resynchronize the existing dropdown state', () => {
  const source = readFileSync(
    new URL('../src/components/main/AnalyzeFilterBar.tsx', import.meta.url),
    'utf8',
  )

  assert.match(source, /applied,\s+onApply,/)
  assert.match(source, /const appliedAtcKey = applied\.atc4\.join\(','\)/)
  assert.match(source, /setDraftAtc\(atcLevelsFromCanonicalAtc4\(appliedAtcKey\.split\(','\)\)\)/)
  assert.equal(source.match(/useState<AtcLevelSelection>/g)?.length, 1)
  assert.doesNotMatch(source, /applied:\s*_applied/)
})

test('treats reordered ATC4 values as the same applied scope', () => {
  assert.equal(isSameAtc4Scope(['C10C', 'C10A1'], ['C10A1', 'C10C']), true)
})

test('detects an ATC4 scope change before reusing dimension selections', () => {
  assert.equal(isSameAtc4Scope(['C10C'], ['C10A1']), false)
  assert.equal(isSameAtc4Scope([], ['C10C']), false)
})
