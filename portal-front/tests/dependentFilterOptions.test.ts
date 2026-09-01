import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import type { DimensionHierarchy } from '../src/types/market.ts'
import { parseFilterOptionsFetchResult } from '../src/utils/filterOptionsResult.ts'
import {
  analysisLevelForHierarchyRequest,
  applyHierarchySelectionDefaults,
  childrenForParent,
  groupSelectedChildrenByParent,
  hierarchyDimensionDefinitions,
  navigateHierarchyParent,
  reconcileHierarchySelections,
} from '../src/utils/moleculeStrengthHierarchy.ts'

const filterBarSource = readFileSync(
  new URL('../src/components/main/AnalyzeFilterBar.tsx', import.meta.url),
  'utf8',
)
const dynamicMarketSource = readFileSync(
  new URL('../src/utils/dynamicMarket.ts', import.meta.url),
  'utf8',
)
const filterPanelSource = readFileSync(
  new URL('../src/components/main/MarketFilterPanel.tsx', import.meta.url),
  'utf8',
)

const hierarchy: DimensionHierarchy = {
  parent_dimension: 'molecule',
  child_dimension: 'molecule_strength',
  relation: 'one_to_many',
  parents: [
    { key: 'alpha', value: 'Alpha' },
    { key: 'beta', value: 'Beta' },
    { key: 'orphan', value: 'Orphan' },
  ],
  children: [
    { key: 'alpha 5mg', value: 'Alpha 5mg', parent_keys: ['alpha'] },
    { key: 'shared 10mg', value: 'Shared 10mg', parent_keys: ['alpha', 'beta'] },
  ],
}

test('stores canonical option keys instead of display values', () => {
  const fullAnalysisLevel = dynamicMarketSource.match(
    /export function fullAnalysisLevel\(([\s\S]*?)\n\}/,
  )?.[0] ?? ''
  const reconcileAnalysisLevel = dynamicMarketSource.match(
    /export function reconcileAnalysisLevel\(([\s\S]*?)\n\}/,
  )?.[0] ?? ''

  assert.match(fullAnalysisLevel, /dimensionValues\(dimensions, k\)\.map\(v => v\.key\)/)
  assert.match(reconcileAnalysisLevel, /dimensionValues\(dimensions, d\.key\)\.map\(v => v\.key\)/)
})

test('sends selections as a JSON string inside the BFF POST body', () => {
  const fetchFilterOptions = dynamicMarketSource.match(
    /export function fetchFilterOptions\(([\s\S]*?)\nexport function dimensionsForSource/,
  )?.[0] ?? ''

  assert.match(fetchFilterOptions, /selections\?: Record<string, string\[\]>/)
  assert.match(fetchFilterOptions, /selections: JSON\.stringify\(params\.selections \?\? \{\}\)/)
})

test('keeps a BFF 400 distinct from a successful empty option payload', () => {
  assert.deepEqual(parseFilterOptionsFetchResult(false, 400, {
    status: 'FAIL',
    message: 'Bad Request',
  }), {
    ok: false,
    reason: 'http',
    status: 400,
  })
  assert.deepEqual(parseFilterOptionsFetchResult(true, 200, {
    status: 'SUCCESS',
    result: { atc: {}, dimensions: [] },
  }), {
    ok: true,
    data: { atc: {}, dimensions: [] },
  })
})

test('renders option-load failures instead of silently merging them as empty data', () => {
  assert.match(filterBarSource, /filterOptionsError/)
  assert.match(filterBarSource, /필터 옵션을 불러오지 못했습니다/)
  assert.match(
    filterBarSource,
    /if \(!result\.ok\) \{[\s\S]*?return[\s\S]*?const filterOpts = result\.data/,
  )
})

test('uses canonical keys for checkbox state while retaining display labels', () => {
  assert.match(filterPanelSource, /const allVals = values\.map\(v => v\.key\)/)
  assert.match(filterPanelSource, /selected\.includes\(v\.key\)/)
  assert.match(filterPanelSource, /toggleOne\(v\.key, e\.target\.checked\)/)
  assert.match(filterPanelSource, />\{v\.value\}</)
})

test('replaces the two flat dimensions with one hierarchy panel definition', () => {
  assert.deepEqual(
    hierarchyDimensionDefinitions([
      { key: 'seller', label: '판매사' },
      { key: 'molecule', label: '성분' },
      { key: 'molecule_strength', label: '성분용량' },
    ], [hierarchy]),
    [
      { key: 'seller', label: '판매사' },
      { key: 'molecule_strength_hierarchy', label: '성분 / 성분용량' },
    ],
  )
})

test('keeps orphan parents visible with an empty child list', () => {
  assert.deepEqual(childrenForParent(hierarchy, 'orphan'), [])
})

test('shows a future multi-parent child under every declared parent', () => {
  assert.deepEqual(
    groupSelectedChildrenByParent(hierarchy, ['shared 10mg']),
    [
      { parent: hierarchy.parents[0], children: [hierarchy.children[1]] },
      { parent: hierarchy.parents[1], children: [hierarchy.children[1]] },
    ],
  )
})

test('parent navigation leaves selected leaf keys unchanged', () => {
  const state = navigateHierarchyParent(
    { activeParentKey: 'alpha', selectedChildKeys: ['alpha 5mg'] },
    'beta',
  )
  assert.deepEqual(state, {
    activeParentKey: 'beta',
    selectedChildKeys: ['alpha 5mg'],
  })
})

test('sends molecule-strength leaves but never the molecule navigation key', () => {
  assert.deepEqual(
    analysisLevelForHierarchyRequest('UBIST', {
      molecule: ['alpha'],
      molecule_strength: ['alpha 5mg'],
    }),
    { molecule_strength: ['alpha 5mg'] },
  )
})

test('an empty leaf selection means no molecule or molecule-strength filter', () => {
  assert.deepEqual(
    analysisLevelForHierarchyRequest('UBIST', {
      molecule: ['alpha'],
      molecule_strength: [],
    }),
    {},
  )
})

test('hierarchy reset starts with zero selected leaves and leaves other dimensions unchanged', () => {
  assert.deepEqual(applyHierarchySelectionDefaults({
    seller: ['jw'],
    molecule: ['alpha', 'beta', 'orphan'],
    molecule_strength: ['alpha 5mg', 'shared 10mg'],
    form: [],
    route: [],
    reimbursement: [],
    specialty: [],
    facility: [],
  }, [hierarchy]), {
    seller: ['jw'],
    molecule: [],
    molecule_strength: [],
    form: [],
    route: [],
    reimbursement: [],
    specialty: [],
    facility: [],
  })
})

test('hierarchy option reconciliation preserves valid leaves and prunes stale leaves', () => {
  assert.deepEqual(reconcileHierarchySelections({
    seller: ['jw'],
    molecule: ['navigation-only'],
    molecule_strength: ['alpha 5mg', 'removed'],
  }, [hierarchy]), {
    seller: ['jw'],
    molecule: [],
    molecule_strength: ['alpha 5mg'],
  })
})

test('ATC4-scoped hierarchy options prune leaves outside the new market', () => {
  const scopedHierarchy: DimensionHierarchy = {
    ...hierarchy,
    parents: hierarchy.parents.slice(0, 1),
    children: hierarchy.children.slice(0, 1),
  }
  assert.deepEqual(reconcileHierarchySelections({
    molecule: [],
    molecule_strength: ['alpha 5mg', 'removed from scoped ATC4'],
  }, [scopedHierarchy]).molecule_strength, ['alpha 5mg'])
})

test('the hierarchy UI removes the old bidirectional request path', () => {
  assert.doesNotMatch(filterBarSource, /dependentDimensionFor|buildDependentSelections|clearDependentSelectionOnEmptyParent/)
  assert.match(filterPanelSource, /MoleculeStrengthDrilldown/)
  assert.match(filterPanelSource, /MOLECULE_STRENGTH_HIERARCHY_KEY/)
})
