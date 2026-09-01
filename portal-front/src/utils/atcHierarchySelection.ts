import type { AtcOptionsTree } from '../types/market'
import type { AtcLevelSelection } from './analyzeFilterSelection'

type AtcLevel = keyof AtcLevelSelection
type UpperAtcLevel = Exclude<AtcLevel, 'atc4'>
type DescendantLevel = Exclude<AtcLevel, 'atc1'>

interface DescendantSnapshot {
  readonly atc2?: readonly string[]
  readonly atc3?: readonly string[]
  readonly atc4?: readonly string[]
}

interface SelectionTransaction {
  readonly baselineParents: readonly string[]
  readonly baselineDescendants: DescendantSnapshot
  readonly touched: boolean
}

export type AtcHierarchySelectionHistory = Readonly<
  Partial<Record<UpperAtcLevel, SelectionTransaction>>
>

export interface AtcHierarchySelectionState {
  readonly selection: AtcLevelSelection
  readonly history: AtcHierarchySelectionHistory
}

const LEVELS = ['atc1', 'atc2', 'atc3', 'atc4'] as const
const UPPER_LEVELS = ['atc1', 'atc2', 'atc3'] as const

export function expandAtcCodesToLeaves(
  codes: readonly string[],
  tree: AtcOptionsTree,
): string[] {
  const leafKeys = new Set((tree.atc4 ?? []).map(option => option.key))
  const descendantsByParent = new Map<string, string[]>()
  for (const level of ['atc2', 'atc3', 'atc4'] as const) {
    for (const option of tree[level] ?? []) {
      if (!option.parent) continue
      const descendants = descendantsByParent.get(option.parent) ?? []
      descendants.push(option.key)
      descendantsByParent.set(option.parent, descendants)
    }
  }

  const leaves: string[] = []
  const seen = new Set<string>()
  const visit = (code: string) => {
    if (leafKeys.has(code)) {
      if (!seen.has(code)) leaves.push(code)
      seen.add(code)
      return
    }
    for (const child of descendantsByParent.get(code) ?? []) visit(child)
  }
  for (const code of codes) visit(code)
  return leaves
}

function descendantLevels(level: UpperAtcLevel): readonly DescendantLevel[] {
  switch (level) {
    case 'atc1': return ['atc2', 'atc3', 'atc4']
    case 'atc2': return ['atc3', 'atc4']
    case 'atc3': return ['atc4']
  }
}

function cloneSelection(selection: AtcLevelSelection): AtcLevelSelection {
  return {
    atc1: [...selection.atc1],
    atc2: [...selection.atc2],
    atc3: [...selection.atc3],
    atc4: [...selection.atc4],
  }
}

function sameValues(left: readonly string[], right: readonly string[]): boolean {
  const leftSet = new Set(left)
  const rightSet = new Set(right)
  return leftSet.size === rightSet.size && [...leftSet].every(value => rightSet.has(value))
}

function descendantsFor(
  tree: AtcOptionsTree,
  level: UpperAtcLevel,
  parentValues: readonly string[],
): DescendantSnapshot {
  const descendants: Record<string, readonly string[]> = {}
  let parents = [...parentValues]

  for (const childLevel of descendantLevels(level)) {
    const options = parents.length === 0
      ? []
      : (tree[childLevel] ?? []).filter(option => (
          !option.parent || parents.includes(option.parent)
        ))
    parents = options.map(option => option.key)
    descendants[childLevel] = parents
  }

  return descendants
}

function snapshotDescendants(
  selection: AtcLevelSelection,
  level: UpperAtcLevel,
): DescendantSnapshot {
  return Object.fromEntries(
    descendantLevels(level).map(childLevel => [childLevel, [...selection[childLevel]]]),
  )
}

function applyDescendants(
  selection: AtcLevelSelection,
  level: UpperAtcLevel,
  descendants: DescendantSnapshot,
): AtcLevelSelection {
  const next = cloneSelection(selection)
  for (const childLevel of descendantLevels(level)) {
    next[childLevel] = [...(descendants[childLevel] ?? [])]
  }
  return next
}

function clearLowerHistory(
  history: AtcHierarchySelectionHistory,
  level: UpperAtcLevel,
): AtcHierarchySelectionHistory {
  const next = { ...history }
  const startIndex = UPPER_LEVELS.indexOf(level)
  for (const childLevel of UPPER_LEVELS.slice(startIndex + 1)) delete next[childLevel]
  return next
}

function markAncestorHistoryTouched(
  history: AtcHierarchySelectionHistory,
  level: AtcLevel,
): AtcHierarchySelectionHistory {
  const next = { ...history }
  const levelIndex = LEVELS.indexOf(level)
  for (const ancestor of UPPER_LEVELS) {
    if (LEVELS.indexOf(ancestor) >= levelIndex) break
    const transaction = next[ancestor]
    if (transaction) next[ancestor] = { ...transaction, touched: true }
  }
  return next
}

export function createAtcHierarchySelectionState(
  selection: AtcLevelSelection,
): AtcHierarchySelectionState {
  return { selection: cloneSelection(selection), history: {} }
}

export function transitionAtcHierarchySelection(
  state: AtcHierarchySelectionState,
  tree: AtcOptionsTree,
  level: AtcLevel,
  values: readonly string[],
): AtcHierarchySelectionState {
  const previousValues = state.selection[level]
  if (sameValues(previousValues, values)) return state

  let history = markAncestorHistoryTouched(state.history, level)
  let selection = cloneSelection(state.selection)
  selection[level] = [...values]
  if (level === 'atc4') return { selection, history }

  history = clearLowerHistory(history, level)
  const additions = values.filter(value => !previousValues.includes(value))
  const transaction = history[level]

  if (additions.length > 0) {
    const activeTransaction = transaction ?? {
      baselineParents: [...previousValues],
      baselineDescendants: snapshotDescendants(state.selection, level),
      touched: false,
    }
    selection = applyDescendants(selection, level, descendantsFor(tree, level, values))
    return { selection, history: { ...history, [level]: activeTransaction } }
  }

  if (!transaction) {
    selection = applyDescendants(selection, level, descendantsFor(tree, level, values))
    return { selection, history }
  }

  if (transaction.touched) {
    selection = applyDescendants(selection, level, {})
    const nextHistory = { ...history }
    delete nextHistory[level]
    return { selection, history: nextHistory }
  }

  if (sameValues(values, transaction.baselineParents)) {
    selection = applyDescendants(selection, level, transaction.baselineDescendants)
    const nextHistory = { ...history }
    delete nextHistory[level]
    return { selection, history: nextHistory }
  }

  selection = applyDescendants(selection, level, descendantsFor(tree, level, values))
  return { selection, history }
}
