const safeLabel = (value: string | undefined, fallback: string): string => {
  const normalized = value?.trim()
  return normalized || fallback
}

const withObjectParticle = (value: string): string => {
  const last = value.charCodeAt(value.length - 1)
  const hasFinalConsonant = last >= 0xac00 && last <= 0xd7a3 && (last - 0xac00) % 28 !== 0
  return `${value}${hasFinalConsonant ? '을' : '를'}`
}

const marketShareScopeNote = (subject: string): string =>
  `M/S는 선택한 분석 레벨 값으로 좁혀진 시장 안에서 다시 계산합니다.
분자는 해당 ${subject} 값, 분모는 같은 필터 범위의 전 브랜드 합입니다.
필터를 바꾸면 M/S도 달라질 수 있습니다.`

export const tooltipOrFallback = (text: string | undefined): string => {
  const normalized = text?.trim()
  return normalized || '설명 정보가 없습니다.'
}

export const CHART_TOOLTIPS = {
  brandTrajectory: `• 버블 크기 : 최근 시점 매출
  (매출의 제곱근에 비례하며 최소 크기가 있습니다)
• X축 : 최신 시점의 시장점유율
• Y축 Evolution Index : 브랜드 연평균 성장률 ÷ 시장 연평균 성장률 × 100
  - 100이면 시장과 같은 속도입니다. 함께 감소하는 경우도 100입니다.
  - 시장이 역성장 중이면 부호가 뒤집혀, 음수가 곧 부진은 아닙니다.
• 버블 색상 : 네 가지 구분
  - 시장 대비 성장 방향(가속 / 감속)과 Momentum Score의 부호(강 / 완만)를 조합합니다.
  - 강한 가속 · 완만한 가속 · 완만한 감속 · 강한 감속
• 세로 기준선 : 화면에 표시된 브랜드들의 평균 시장점유율
• Momentum Score : 최근 4개 시점의 점유율에 직선을 맞춘 기울기
  - 단위는 한 관측 간격당 점유율 변화(%p)입니다.
  - UBIST는 월, IQVIA NSA는 분기가 한 간격입니다.
  - 관측이 비어 있으면 간격이 달라질 수 있습니다.

${marketShareScopeNote('브랜드')}`,
  growthMatrix: `브랜드가 시장 성장에 얼마나 기여했는지를 점유율과 함께 봅니다.

• 가로축 : 시장 점유율(M/S, %)
• 세로축 : 성장 기여율(%)
  - 시장 전체 증감액 대비 그 브랜드 증감액의 비율입니다.
  - 금액이 아니라 비율입니다.
• 버블 크기 : 최근 시점 매출
• 버블 색 : 두 축이 만드는 네 개 영역 구분
• 세로 기준선 : 화면에 표시된 브랜드들의 평균 점유율
• 가로 기준선 : 성장 기여율 0%

→ 오른쪽 위일수록 점유율이 크고 성장에도 많이 기여했다는 뜻입니다.

${marketShareScopeNote('브랜드')}`,
  marketContributionBrand: `• 대상 : 선택한 1~5년 구간의 상위 6개 브랜드와 나머지 합계
• Growth Contribution : 시장 증감액 대비 그 브랜드 증감액의 비율
  - 시장 전체 증감액이 0이 아니면 전 브랜드를 더하면 100%가 됩니다.
  - 표에는 상위 6개와 나머지 합계를 표시합니다.`,
  marketContributionCompany: `• 대상 : 선택한 1~5년 구간의 상위 6개 회사와 나머지 합계
• Growth Contribution : 시장 증감액 대비 그 회사 증감액의 비율
  - 시장 전체 증감액이 0이 아니면 전 회사를 더하면 100%가 됩니다.
  - 표에는 상위 6개와 나머지 합계를 표시합니다.`,
} as const

export interface DynamicTooltipOptions {
  referenceLabel: string | undefined
  rankingToggle: string | undefined
  m5Label: string | undefined
  m7Label: string | undefined
  m8Label: string | undefined
}

export interface DynamicTooltips {
  hhi: string
  rank: string
  levelSalesTrend: string
  levelSalesMs: string
  top5CustomerTrend: string
  top5CustomerMs: string
  levelTop5Trend: string
  levelTop5Ms: string
}

export function buildMarketSizeGrowthTooltip(
  measureLabel: string | undefined,
  growthLabel: string | undefined,
): string {
  const measure = safeLabel(measureLabel, '지표')
  const growth = safeLabel(growthLabel, '평균 성장률')
  return `차트의 각 점에 마우스를 올리면 그 시점까지의 평균 성장률이 표시됩니다.

• Measure : ${measure}
• 표시 구간 : UBIST는 최근 60개 월 구간, IQVIA NSA는 최근 20개 분기 구간입니다.
• 최초값 : 표시 구간에서 가장 오래된 유효값을 사용하며,
  정확히 5년 전 값이 있으면 그 값을 사용합니다.
• 최종값 : 마우스를 올린 그 시점의 값
• 기간 : 최초값과 마우스를 올린 시점 사이의 실제 경과 월 또는 분기 수입니다.
• ${growth} 수식 : (최종값 / 최초값)^(1 / 기간) - 1
• CMGR : 월 평균 성장률 (Compound Monthly Growth Rate)
• CQGR : 분기 평균 성장률 (Compound Quarterly Growth Rate)

두 시점의 단순 증감률이 아니라, 그 사이를 한 달 또는 한 분기당 평균 성장률로 환산한 값입니다.`
}

export function buildDynamicChartTooltips(options: DynamicTooltipOptions): DynamicTooltips {
  const reference = safeLabel(options.referenceLabel, '기준 시점')
  const ranking = safeLabel(options.rankingToggle, '대상')
  const m5 = safeLabel(options.m5Label, '지표')
  const m7 = safeLabel(options.m7Label, '지표')
  const m8 = safeLabel(options.m8Label, '지표')
  const legend = `차트 범례 안내
범례를 클릭하면 해당 항목을 차트에 표시하거나 숨길 수 있습니다.`
  const top5 = (target: string, measure: string) =>
    `조회 범위 전체 합계로 상위 5개 ${withObjectParticle(target)} 한 번 선정하고 모든 시점에 동일하게 표시합니다. 시점마다 다시 선정하지 않습니다.\n\n• 대상 : 상위 5개 ${target} + 기타\n• Measure : ${measure}\n• 기타 : 상위 5개를 제외한 모든 ${target}의 시점별 합\n\n${legend}`

  return {
    hhi: `HHI(허핀달-허쉬만 지수)는 시장 집중도를 나타냅니다. 값이 높을수록 소수 브랜드의 점유 비중이 큰 시장입니다.\n\n• Measure : 매출\n• 계산식 : 각 브랜드 점유율(%)을 제곱해 모두 더합니다.\n• 값의 범위 : 0~10000\n• 대상 : 상위 N개나 '기타'가 아니라 해당 범위의 전 브랜드\n• 해석 : 1500 미만 경쟁적 · 1500~2499 부분 집중 · 2500 이상 고집중\n• 기간 : 데이터가 완전한 연도(calendar year)별 집계`,
    rank: `${reference}까지의 조회 범위 전체 합계로 ${withObjectParticle(`선택 ${ranking}와 경쟁 상위 5개`)} 고정해 연도별 매출과 시장점유율을 표시합니다.\n\n• 대상 : 선택 ${ranking} + 경쟁 상위 5개 + 기타\n• 기타 : ${withObjectParticle(`선택 ${ranking}와 경쟁 상위 5개`)} 제외한 모든 ${ranking}의 연도별 합\n\n${marketShareScopeNote(ranking)}\n\n${legend}`,
    levelSalesTrend: top5('대상', m5),
    levelSalesMs: `${marketShareScopeNote(`대상 ${m5}`)}\n일반뷰·Market Landscape·Competitive Dynamics는 같은 식을 서로 다른 범위에 적용합니다.\n\n• 기준 : ${reference}\n• 대상 : 조회 범위 전체 합계로 선정한 상위 5개 대상 + 기타`,
    top5CustomerTrend: `${top5('브랜드', m7)}

• 표시 구간 : 최근 10개 구간만 표시합니다.
  - UBIST는 10개월, IQVIA NSA는 10분기입니다.
  - 기간 선택도 이 범위 안에서만 가능합니다.`,
    top5CustomerMs: `${marketShareScopeNote(`브랜드 ${m7}`)}\n\n• 기준 : ${reference}\n• 대상 : 조회 범위 전체 합계로 선정한 상위 5개 브랜드 + 기타`,
    levelTop5Trend: `${top5('브랜드', m8)}

• 표시 구간 : 최근 10개 구간만 표시합니다.
  - UBIST는 10개월, IQVIA NSA는 10분기입니다.
  - 기간 선택도 이 범위 안에서만 가능합니다.`,
    levelTop5Ms: `${marketShareScopeNote(`브랜드 ${m8}`)}\n\n• 기준 : ${reference}\n• 대상 : 조회 범위 전체 합계로 선정한 상위 5개 브랜드 + 기타`,
  }
}

export const BRAND_ACTIVITY_TOOLTIPS = {
  channelShare: `• 출처 : IQVIA CSD Channel Dynamics
• 콜 수 : 선택한 CSD 시장과 선택한 채널 안의 월별 활동량
• Share : 그 달에 반환된 대상들의 콜 수 합계 대비 해당 브랜드 또는 회사의 비율
  - 선택한 채널 안의 합이며 전 채널 합이 아닙니다.
  - 브랜드 뷰와 회사 뷰는 각 화면에 표시된 대상 집합을 분모로 씁니다.
• 오른쪽 Share 차트 : 조회 범위의 마지막 달 기준`,
  keywordShare: `본 표의 키워드는 최근 1년간 가장 많이 언급된 키워드로 고정되어 있으며,
언급 빈도 순위는 설정된 기간 기준으로 집계됩니다.

• 출처 : IQVIA CSD-Keyword
• 대상 : 설정한 기간·종별·진료과 조건에서 그 브랜드가 등장한 행
• 건수 : 그중 해당 키워드가 포함된 행 수
• 비율 : 그 브랜드의 전체 행 수(중복 제거) 대비 비율
  - 100% 기준은 가로 한 줄 — 브랜드마다 분모가 다릅니다.
  - 따라서 세로(브랜드 간) % 직접 비교는 맞지 않습니다.
  - 한 행에 여러 키워드가 포함될 수 있어 가로 합이 100% 를 넘습니다.`,
  keywordCross: `선택한 기간과 INTEREST 또는 Prescription Evolution 조건에 해당하는 브랜드 행을 대상으로 키워드 포함 건수와 비율을 집계합니다.

• 건수 : 선택 조건을 만족하면서 해당 키워드가 포함된 행 수
• 비율 : 선택 조건을 만족하는 그 브랜드의 전체 행 수(중복 제거) 대비 비율
• 100% 기준 : 브랜드별 가로 한 줄
• 한 행에 여러 키워드가 포함되면 가로 합이 100%를 넘을 수 있습니다.`,
  interest: `IQVIA CSD-Keyword의 INTEREST 응답을 VERY USEFUL, SOMEWHAT USEFUL, NOT AT ALL로 구분한 월별 분포입니다. 각 비율의 분모는 필터 적용 후 해당 브랜드·해당 월의 전체 응답 수입니다.`,
  perception: `집계 범위 : 현재 선택 브랜드의 일반뷰 ATC4 범위에서
  종별과 진료과 전체를 대상으로,
  Keyword와 CSD 데이터가 함께 존재하는 전체 월 구간을 집계합니다.

• X축 · Y축 : 응답을 0~1로 가중평균한 점수입니다.
  - 처방 빈도 : FREQUENTLY 1 · OCCASIONALLY 0.6 · LAPSED 0.3 · NEVER/NEW TO ME 0
  - 변화 : INCREASE 1 · UNCHANGED 0.5 · DECREASE 0
• 버블 크기 : 응답 수 (응답 수의 제곱근에 비례하며 최소 크기가 있습니다)
• 버블 색상 : 브랜드를 구분합니다 (값의 크기와는 무관합니다)
• 십자선 : 화면에 표시된 브랜드들의 평균`,
  activityVolume: `• 왼쪽 축 : IQVIA NSA 분기별 처방량(UNIT, DOSAGE UNIT, COUNT UNIT 원단위)
• 오른쪽 축 : IQVIA CSD Channel Dynamics의 월별 콜 수를 분기별로 합산한 활동량
• 두 축은 서로 다른 단위이므로 값의 크기를 직접 비교하지 않습니다.`,
} as const

export const DEEP_ANALYSIS_TOOLTIPS = {
  forecast: `표시 구간 : 과거 5년과 미래 5년을 표시하며,
  실제 데이터가 있는 구간까지만 나타납니다.

실선은 실적, 점선은 예측입니다.

• 점선은 예측값 자체를 이은 선입니다 — 오차 범위가 아닙니다.
• 예측 모델은 보유한 이력 길이에 따라 자동으로 선택됩니다.
• 불확실성 범위는 Simulation 카드에서 확인할 수 있습니다.`,
  issues: `매출 예측에 사용한 시장 브랜드 집합의 관련 뉴스를 최신순으로 최대 50건 표시합니다. 하나의 기사가 여러 브랜드와 관련되면 각 브랜드에 함께 포함될 수 있습니다.

중요도는 뉴스가 선택 시장과 브랜드에 얼마나 관련되는지를 나타내는 0~100 점수입니다. 점수가 높을수록 관련성이 높습니다. 산출 기준과 노출 기준은 뉴스를 처리한 분류 세대에 따라 다를 수 있습니다.`,
  simulation: `표시 구간 : 과거 5년과 미래 5년으로 고정되며,
  실제 데이터가 있는 구간까지만 나타납니다.

선택한 브랜드와 지표의 마지막 실적 시점에서 예측 구간이 시작됩니다. 기본 시나리오는 모델의 점추정값, 최저·최고 시나리오는 95% 예측 구간을 사용합니다.

• 입력 : 브랜드, 매출 또는 처방량, 처방량 단위
• UBIST : 월 단위
• IQVIA NSA : 분기 단위`,
  reference: `기준은 선택한 출처와 지표의 실적 데이터 중 가장 최근 시점입니다.
예측과 Simulation 결과는 요청 시 생성되며 최대 24시간 재사용될 수 있습니다.`,
} as const
