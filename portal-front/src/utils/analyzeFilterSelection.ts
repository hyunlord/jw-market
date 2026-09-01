export interface AtcLevelSelection {
  atc1: string[]
  atc2: string[]
  atc3: string[]
  atc4: string[]
}

interface OptionLoadSelectionInput {
  sameContext: boolean
  defaults: AtcLevelSelection
}

function unique(values: string[]): string[] {
  return [...new Set(values)]
}

export function isSameAtc4Scope(left: readonly string[], right: readonly string[]): boolean {
  const leftScope = [...new Set(left.map(value => value.trim()).filter(Boolean))].sort()
  const rightScope = [...new Set(right.map(value => value.trim()).filter(Boolean))].sort()
  return leftScope.length === rightScope.length
    && leftScope.every((value, index) => value === rightScope[index])
}

export function atcLevelsFromCanonicalAtc4(atc4: string[]): AtcLevelSelection {
  return {
    atc1: unique(atc4.map(code => code.slice(0, 1))),
    atc2: unique(atc4.map(code => code.slice(0, 3))),
    atc3: unique(atc4.map(code => code.slice(0, 4))),
    atc4: [...atc4],
  }
}

export function resolveOptionLoadAtcSelection({
  sameContext,
  defaults,
}: OptionLoadSelectionInput): AtcLevelSelection | null {
  if (!sameContext) {
    return {
      atc1: [...defaults.atc1],
      atc2: [...defaults.atc2],
      atc3: [...defaults.atc3],
      atc4: [...defaults.atc4],
    }
  }
  return null
}
