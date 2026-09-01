import assert from 'node:assert/strict'
import test from 'node:test'
import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import MoleculeStrengthDrilldown from '../src/components/main/MoleculeStrengthDrilldown.tsx'
import type { DimensionHierarchy } from '../src/types/market.ts'

test('renders every child for the active molecule when search matches one strength', () => {
  // Given an active molecule with multiple strengths and a search matching one leaf.
  const children = Array.from({ length: 13 }, (_, index) => ({
    key: `acetylcysteine strength ${index + 1}`,
    value: index === 12 ? 'acetylcysteine 100mg/ml [101833BIJ]' : `acetylcysteine strength ${index + 1}`,
    parent_keys: ['acetylcysteine'],
  }))
  const hierarchy: DimensionHierarchy = {
    parent_dimension: 'molecule',
    child_dimension: 'molecule_strength',
    relation: 'one_to_many',
    parents: [{ key: 'acetylcysteine', value: 'acetylcysteine' }],
    children,
  }

  // When the hierarchy panel renders with the descendant search term.
  const markup = renderToStaticMarkup(createElement(MoleculeStrengthDrilldown, {
    hierarchy,
    selectedChildKeys: [],
    search: '33',
    onSelectionChange: () => undefined,
  }))

  // Then the total count and the rendered leaf count describe the same 13 items.
  assert.equal((markup.match(/class="check-item"/g) ?? []).length, 13)
  for (const child of children) {
    assert.match(markup, new RegExp(child.value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')))
  }
})
