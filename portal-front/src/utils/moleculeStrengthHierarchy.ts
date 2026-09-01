import type {
  DimensionHierarchy,
  DimensionHierarchyChild,
  DimensionHierarchyNode,
} from '../types/market'

export const MOLECULE_STRENGTH_HIERARCHY_KEY = 'molecule_strength_hierarchy'

export interface HierarchyNavigationState {
  activeParentKey: string
  selectedChildKeys: string[]
}

export interface SelectedHierarchyGroup {
  parent: DimensionHierarchyNode
  children: DimensionHierarchyChild[]
}

export function findMoleculeStrengthHierarchy(
  hierarchies: readonly DimensionHierarchy[],
): DimensionHierarchy | null {
  return hierarchies.find(item => (
    item.parent_dimension === 'molecule'
    && item.child_dimension === 'molecule_strength'
  )) ?? null
}

export function hierarchyDimensionDefinitions(
  dimensions: readonly { key: string; label: string }[],
  hierarchies: readonly DimensionHierarchy[],
): { key: string; label: string }[] {
  if (!findMoleculeStrengthHierarchy(hierarchies)) return [...dimensions]

  const result: { key: string; label: string }[] = []
  for (const dimension of dimensions) {
    if (dimension.key === 'molecule') {
      result.push({
        key: MOLECULE_STRENGTH_HIERARCHY_KEY,
        label: '성분 / 성분용량',
      })
    } else if (dimension.key !== 'molecule_strength') {
      result.push(dimension)
    }
  }
  return result
}

export function childrenForParent(
  hierarchy: DimensionHierarchy,
  parentKey: string,
): DimensionHierarchyChild[] {
  return hierarchy.children.filter(child => child.parent_keys.includes(parentKey))
}

export function groupSelectedChildrenByParent(
  hierarchy: DimensionHierarchy,
  selectedChildKeys: readonly string[],
): SelectedHierarchyGroup[] {
  const selected = new Set(selectedChildKeys)
  return hierarchy.parents.flatMap(parent => {
    const children = childrenForParent(hierarchy, parent.key)
      .filter(child => selected.has(child.key))
    return children.length > 0 ? [{ parent, children }] : []
  })
}

export function navigateHierarchyParent(
  state: HierarchyNavigationState,
  activeParentKey: string,
): HierarchyNavigationState {
  return {
    activeParentKey,
    selectedChildKeys: [...state.selectedChildKeys],
  }
}

export function applyHierarchySelectionDefaults(
  selections: Readonly<Record<string, string[]>>,
  hierarchies: readonly DimensionHierarchy[],
): Record<string, string[]> {
  const hierarchy = findMoleculeStrengthHierarchy(hierarchies)
  if (!hierarchy) return Object.fromEntries(
    Object.entries(selections).map(([key, values]) => [key, [...values]]),
  )
  return {
    ...selections,
    [hierarchy.parent_dimension]: [],
    [hierarchy.child_dimension]: [],
  }
}

export function reconcileHierarchySelections(
  selections: Readonly<Record<string, string[]>>,
  hierarchies: readonly DimensionHierarchy[],
): Record<string, string[]> {
  const hierarchy = findMoleculeStrengthHierarchy(hierarchies)
  if (!hierarchy) return Object.fromEntries(
    Object.entries(selections).map(([key, values]) => [key, [...values]]),
  )
  const validChildren = new Set(hierarchy.children.map(child => child.key))
  return {
    ...selections,
    [hierarchy.parent_dimension]: [],
    [hierarchy.child_dimension]: (selections[hierarchy.child_dimension] ?? [])
      .filter(value => validChildren.has(value)),
  }
}

export function analysisLevelForHierarchyRequest(
  source: 'UBIST' | 'IQVIA',
  analysisLevel: Readonly<Record<string, string[]>>,
): Record<string, string[]> {
  return Object.fromEntries(
    Object.entries(analysisLevel)
      .filter(([key, values]) => (
        values.length > 0
        && !(source === 'UBIST' && key === 'molecule')
      ))
      .map(([key, values]) => [key, [...values]]),
  )
}
