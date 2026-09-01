import assert from 'node:assert/strict'
import test from 'node:test'

import type { AtcOptionsTree } from '../src/types/market.ts'
import {
  createAtcHierarchySelectionState,
  expandAtcCodesToLeaves,
  transitionAtcHierarchySelection,
} from '../src/utils/atcHierarchySelection.ts'

const tree: AtcOptionsTree = {
  atc1: [
    { key: 'A', label: 'A', level: 'atc1' },
    { key: 'B', label: 'B', level: 'atc1' },
  ],
  atc2: [
    { key: 'A01', label: 'A01', level: 'atc2', parent: 'A' },
    { key: 'A02', label: 'A02', level: 'atc2', parent: 'A' },
    { key: 'B01', label: 'B01', level: 'atc2', parent: 'B' },
  ],
  atc3: [
    { key: 'A01A', label: 'A01A', level: 'atc3', parent: 'A01' },
    { key: 'A02A', label: 'A02A', level: 'atc3', parent: 'A02' },
    { key: 'B01A', label: 'B01A', level: 'atc3', parent: 'B01' },
  ],
  atc4: [
    { key: 'A01A1', label: 'A01A1', level: 'atc4', parent: 'A01A' },
    { key: 'A02A1', label: 'A02A1', level: 'atc4', parent: 'A02A' },
    { key: 'B01A1', label: 'B01A1', level: 'atc4', parent: 'B01A' },
  ],
}

const individualB = {
  atc1: [],
  atc2: ['B01'],
  atc3: ['B01A'],
  atc4: ['B01A1'],
}

test('selecting an ATC1 parent selects every descendant under it', () => {
  const initial = createAtcHierarchySelectionState(individualB)

  const next = transitionAtcHierarchySelection(initial, tree, 'atc1', ['A'])

  assert.deepEqual(next.selection, {
    atc1: ['A'],
    atc2: ['A01', 'A02'],
    atc3: ['A01A', 'A02A'],
    atc4: ['A01A1', 'A02A1'],
  })
})

test('removing an untouched parent restores the descendant snapshot', () => {
  const selected = transitionAtcHierarchySelection(
    createAtcHierarchySelectionState(individualB),
    tree,
    'atc1',
    ['A'],
  )

  const restored = transitionAtcHierarchySelection(selected, tree, 'atc1', [])

  assert.deepEqual(restored.selection, individualB)
})

test('removing a parent after a descendant edit clears every descendant', () => {
  const selected = transitionAtcHierarchySelection(
    createAtcHierarchySelectionState(individualB),
    tree,
    'atc1',
    ['A'],
  )
  const edited = transitionAtcHierarchySelection(selected, tree, 'atc4', ['A01A1'])

  const cleared = transitionAtcHierarchySelection(edited, tree, 'atc1', [])

  assert.deepEqual(cleared.selection, { atc1: [], atc2: [], atc3: [], atc4: [] })
})

test('a descendant edit remains dirty even when its values return to the automatic set', () => {
  const selected = transitionAtcHierarchySelection(
    createAtcHierarchySelectionState(individualB),
    tree,
    'atc1',
    ['A'],
  )
  const edited = transitionAtcHierarchySelection(selected, tree, 'atc4', ['A01A1'])
  const sameValuesAgain = transitionAtcHierarchySelection(
    edited,
    tree,
    'atc4',
    ['A01A1', 'A02A1'],
  )

  const cleared = transitionAtcHierarchySelection(sameValuesAgain, tree, 'atc1', [])

  assert.deepEqual(cleared.selection, { atc1: [], atc2: [], atc3: [], atc4: [] })
})

test('multiple parent selections keep all descendants until the baseline is restored', () => {
  const initial = createAtcHierarchySelectionState(individualB)
  const selectedA = transitionAtcHierarchySelection(initial, tree, 'atc1', ['A'])
  const selectedAB = transitionAtcHierarchySelection(selectedA, tree, 'atc1', ['A', 'B'])

  const remainingA = transitionAtcHierarchySelection(selectedAB, tree, 'atc1', ['A'])
  const restored = transitionAtcHierarchySelection(remainingA, tree, 'atc1', [])

  assert.deepEqual(remainingA.selection.atc4, ['A01A1', 'A02A1'])
  assert.deepEqual(restored.selection, individualB)
})

test('editing ATC2 after automatic ATC1 selection makes the ATC1 rollback clear all descendants', () => {
  const selected = transitionAtcHierarchySelection(
    createAtcHierarchySelectionState(individualB),
    tree,
    'atc1',
    ['A'],
  )
  const edited = transitionAtcHierarchySelection(selected, tree, 'atc2', ['A01'])

  const cleared = transitionAtcHierarchySelection(edited, tree, 'atc1', [])

  assert.deepEqual(edited.selection, {
    atc1: ['A'],
    atc2: ['A01'],
    atc3: ['A01A'],
    atc4: ['A01A1'],
  })
  assert.deepEqual(cleared.selection, { atc1: [], atc2: [], atc3: [], atc4: [] })
})

test('selecting ATC2 and ATC3 parents selects every descendant at each level', () => {
  const initial = createAtcHierarchySelectionState({
    atc1: ['A'],
    atc2: [],
    atc3: [],
    atc4: [],
  })
  const selectedAtc2 = transitionAtcHierarchySelection(initial, tree, 'atc2', ['A01'])
  const selectedAtc3 = transitionAtcHierarchySelection(selectedAtc2, tree, 'atc3', ['A01A'])

  assert.deepEqual(selectedAtc2.selection.atc3, ['A01A'])
  assert.deepEqual(selectedAtc2.selection.atc4, ['A01A1'])
  assert.deepEqual(selectedAtc3.selection.atc4, ['A01A1'])
})

test('selection transitions never mutate the full ATC navigation tree', () => {
  const before = structuredClone(tree)
  const initial = createAtcHierarchySelectionState(individualB)

  transitionAtcHierarchySelection(initial, tree, 'atc1', ['A'])

  assert.deepEqual(tree, before)
})

test('brand defaults expand parent ATC codes to concrete leaves', () => {
  assert.deepEqual(expandAtcCodesToLeaves(['A01A', 'B01A1'], tree), [
    'A01A1',
    'B01A1',
  ])
})

test('unknown ATC defaults are not sent as leaf filters', () => {
  assert.deepEqual(expandAtcCodesToLeaves(['UNKNOWN'], tree), [])
})
