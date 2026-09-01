export interface ForecastModelPayload {
  name?: string | null
}

export interface ForecastModelBrand {
  brand: string
  forecast_model?: ForecastModelPayload | null
}

export function hasReadableNewsBody(body: string | null | undefined): boolean {
  return typeof body === 'string' && body.trim().length > 0
}

function modelDescription(rawName: string | null | undefined): string {
  const name = (rawName ?? '').trim()
  const normalized = name.toLowerCase().replace(/[\s_-]+/g, '')

  if (normalized === 'holtwinters' || normalized === 'exponentialsmoothing') {
    return 'Holt-Winters - 추세와 계절성을 함께 반영한 지수평활 방식'
  }
  if (normalized === 'prophet') {
    return 'Prophet - 추세와 반복되는 계절 패턴을 분리해 예측하는 방식'
  }
  if (normalized === 'sarimax' || normalized === 'sarima') {
    return 'SARIMAX - 시계열의 자기상관과 계절 패턴을 함께 반영하는 방식'
  }
  if (normalized === 'linearregression' || normalized === 'linear') {
    return '선형회귀 - 과거 변화 추세를 직선으로 연장해 예측하는 방식'
  }
  if (normalized === 'mean' || normalized === 'average') {
    return '데이터가 부족해 관측값 평균을 사용합니다'
  }
  if (!name) {
    return '선택된 예측 모델 정보가 없습니다'
  }
  return `${name} - 현재 응답에서 선택된 예측 방식`
}

export function buildForecastModelExplanation(input: {
  source: string
  historyPeriodCount: number
  brands: ForecastModelBrand[]
}): string {
  const source = input.source.trim() || '선택 출처'
  const periodCount = Math.max(0, Math.trunc(input.historyPeriodCount))
  const header = `${source} 이력 ${periodCount}개 구간을 기준으로 선택된 모델입니다.`
  const rows = input.brands.map(
    brand => `• ${brand.brand || '브랜드'} : ${modelDescription(brand.forecast_model?.name)}`,
  )

  return rows.length > 0 ? `${header}\n\n${rows.join('\n')}` : `${header}\n\n선택된 모델 정보가 없습니다.`
}
