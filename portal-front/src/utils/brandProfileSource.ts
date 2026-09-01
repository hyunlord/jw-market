import type { BrandFactorEntry, BrandFactors, BrandFactorValues } from '../types/market'

export type BrandProfileSource = 'IQVIA' | 'UBIST'

export type SourceProfileBrand = {
  readonly brand: string
}

export type SourceProfileRow = {
  readonly key: string
  readonly label: string
  readonly values: readonly string[]
  readonly highlight?: boolean
}

export type SourceNativeBrandProfile = {
  readonly brands: readonly SourceProfileBrand[]
  readonly rows: readonly SourceProfileRow[]
}

const SOURCE_FACTOR_LABELS = {
  IQVIA: {
    mfr_name_kor: '제조사',
    molecule_type: '성분구분',
    molecule_desc: '성분명',
    pack_desc: 'PACK DESC',
    strength: '함량',
    nhi_type: 'NHI 구분',
  },
  UBIST: {
    seller: '판매사',
    molecule_strength: '성분용량',
    form: '제형',
    route: '투여경로',
    reimbursement: '급여 구분',
  },
} as const satisfies Record<BrandProfileSource, Readonly<Record<string, string>>>

const joinValues = (values: readonly string[] | undefined): string | undefined => {
  const nonEmpty = values?.map(value => value.trim()).filter(Boolean) ?? []
  return nonEmpty.length > 0 ? nonEmpty.join(' · ') : undefined
}

const factorKeysInDisplayOrder = (
  source: BrandProfileSource,
  entries: readonly BrandFactorEntry[],
): readonly string[] => {
  const knownKeys = Object.keys(SOURCE_FACTOR_LABELS[source])
  const seen = new Set(knownKeys)
  const extraKeys: string[] = []

  entries.forEach(entry => {
    Object.keys(entry.factors?.values ?? {}).forEach(key => {
      if (!seen.has(key)) {
        seen.add(key)
        extraKeys.push(key)
      }
    })
  })

  return [...knownKeys, ...extraKeys]
}

const factorValue = (entry: BrandFactorEntry, key: string): string | undefined => {
  if (entry.factors?.available === false) return undefined
  const values: BrandFactorValues = entry.factors?.values ?? {}
  return joinValues(values[key])
}

export const buildSourceNativeBrandProfile = (
  source: BrandProfileSource,
  factors: BrandFactors | undefined,
): SourceNativeBrandProfile => {
  const entries = source === 'IQVIA' ? factors?.iqvia ?? [] : factors?.ubist ?? []
  const labels: Readonly<Record<string, string>> = SOURCE_FACTOR_LABELS[source]
  const brands = entries.map(entry => ({ brand: entry.brand || entry.brand_key || '-' }))
  const factorRows = factorKeysInDisplayOrder(source, entries).flatMap(key => {
    const resolved = entries.map(entry => factorValue(entry, key))
    if (!resolved.some(Boolean)) return []

    return [{
      key,
      label: labels[key] ?? key,
      values: resolved.map((value, index) => (
        entries[index]?.factors?.available === false ? '요소 정보 없음' : value ?? '-'
      )),
    }]
  })

  const strengthValues = entries.map(entry => {
    const narratives = [...new Set(
      (entry.strength?.strength_items ?? [])
        .map(item => item.narrative.trim())
        .filter(Boolean),
    )]
    return narratives.length > 0 ? narratives.join('\n\n') : '-'
  })
  const strengthRows: readonly SourceProfileRow[] = strengthValues.some(value => value !== '-')
    ? [{ key: '__strength__', label: '강점 분석', values: strengthValues, highlight: true }]
    : []
  const unavailableRows: readonly SourceProfileRow[] = factorRows.length === 0
    && entries.some(entry => entry.factors?.available === false)
    ? [{
        key: '__availability__',
        label: '요소 정보',
        values: entries.map(entry => entry.factors?.available === false ? '요소 정보 없음' : '-'),
      }]
    : []

  return { brands, rows: [...factorRows, ...unavailableRows, ...strengthRows] }
}
