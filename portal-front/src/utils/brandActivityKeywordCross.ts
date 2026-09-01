import type { MatrixData, TopicsData } from '../types/market.ts'
import type { KeywordExportMode } from './brandActivityExcelSpec.ts'

export type KeywordCrossDataset = {
  readonly value: string
  readonly data: TopicsData
}

export class KeywordCrossDomainError extends Error {
  constructor(mode: KeywordExportMode) {
    super(`keyword cross domain is unavailable: ${mode}`)
    this.name = 'KeywordCrossDomainError'
  }
}

export function keywordCrossDomain(
  matrix: MatrixData | null,
  mode: KeywordExportMode,
): string[] {
  const domain = [...new Set(matrix?.levels[mode] ?? [])].filter(Boolean)
  if (domain.length === 0) throw new KeywordCrossDomainError(mode)
  return domain
}

export async function fetchKeywordCrossDatasets(input: {
  readonly mode: KeywordExportMode
  readonly values: readonly string[]
  readonly fetchData: (filter: { interest?: string; prescription_evolution?: string }) => Promise<TopicsData | null>
}): Promise<KeywordCrossDataset[]> {
  return Promise.all(input.values.map(async value => {
    const filter = input.mode === 'interest'
      ? { interest: value }
      : { prescription_evolution: value }
    const data = await input.fetchData(filter)
    if (!data) throw new Error(`keyword cross data is unavailable: ${input.mode}:${value}`)
    return { value, data }
  }))
}
