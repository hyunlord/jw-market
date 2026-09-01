export type LevelTop5BrandIdentity = {
  readonly brand: string
  readonly rank?: number
  readonly is_others?: boolean
  readonly value_series_10pt?: readonly number[]
  readonly ms_series_10pt?: readonly number[]
  readonly data_quality?: {
    readonly available?: boolean
    readonly reason?: string
  }
}

export type LevelTop5ValueState = {
  readonly brands_in_value?: readonly LevelTop5BrandIdentity[]
  readonly data_quality?: {
    readonly available?: boolean
    readonly reason?: string
  }
}

export const isLevelTop5Others = (brand: LevelTop5BrandIdentity): boolean =>
  brand.is_others === true || brand.brand === '기타'

export const levelTop5BrandLabel = (brand: LevelTop5BrandIdentity): string =>
  isLevelTop5Others(brand)
    ? '기타'
    : brand.data_quality?.available === false
      ? `${brand.brand} (데이터 없음)`
      : brand.brand

export const levelTop5EmptyMessage = (value: LevelTop5ValueState | undefined): string | null => {
  if (!value) return null
  if ((value?.brands_in_value?.length ?? 0) > 0) return null

  switch (value?.data_quality?.reason) {
    case 'dimension_period_missing':
      return '선택한 기간에 분석 데이터가 없습니다.'
    case 'no_data':
    case 'no_data_in_widget_scope':
      return '선택한 조건에 분석 데이터가 없습니다.'
    case 'filtered_member_scope_unavailable':
      return '필터 적용 시 축별 분해를 제공하지 않습니다. 전체 금액과 전체 시장점유율은 선택한 필터 기준으로 표시됩니다.'
    default:
      return '데이터를 표시할 수 없습니다. 조회 조건을 확인해 주세요.'
  }
}
