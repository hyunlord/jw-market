import assert from 'node:assert/strict'
import test from 'node:test'

import { resolveAtcNavigationTree } from '../src/utils/atcNavigationTree.ts'

const item = (key: string, level: 'atc1' | 'atc2' | 'atc3' | 'atc4') => ({
  key,
  value: key,
  label: key,
  level,
  default: false,
  selected: false,
})

test('the first scoped response cannot replace the full ATC navigation tree', () => {
  const fullTree = {
    atc1: ['A', 'B', 'C', 'D'].map(key => item(key, 'atc1')),
    atc2: [item('C10', 'atc2')],
    atc3: [item('C10C', 'atc3')],
    atc4: [item('C10C', 'atc4')],
  }
  const scopedTree = {
    atc1: [item('C', 'atc1')],
    atc2: [item('C10', 'atc2')],
    atc3: [item('C10C', 'atc3')],
    atc4: [item('C10C', 'atc4')],
  }

  const navigationTree = resolveAtcNavigationTree(fullTree, scopedTree)

  assert.deepEqual(navigationTree.atc1?.map(option => option.key), ['A', 'B', 'C', 'D'])
})

test('repeated scoped responses cannot narrow a previously loaded navigation tree', () => {
  const fullTree = {
    atc1: ['A', 'B', 'C', 'D'].map(key => item(key, 'atc1')),
  }

  for (const atc1 of ['C', 'A', 'B']) {
    const scopedTree = { atc1: [item(atc1, 'atc1')] }
    const navigationTree = resolveAtcNavigationTree(fullTree, scopedTree)
    assert.deepEqual(navigationTree.atc1?.map(option => option.key), ['A', 'B', 'C', 'D'])
  }
})
