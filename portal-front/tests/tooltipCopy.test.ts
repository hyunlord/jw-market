import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import {
  BRAND_ACTIVITY_TOOLTIPS,
  CHART_TOOLTIPS,
  DEEP_ANALYSIS_TOOLTIPS,
  buildDynamicChartTooltips,
  buildMarketSizeGrowthTooltip,
  tooltipOrFallback,
} from '../src/utils/tooltipCopy.ts'

test('approved static tooltip copy is preserved verbatim', () => {
  assert.match(BRAND_ACTIVITY_TOOLTIPS.keywordShare, /COUNT|전체 행 수\(중복 제거\)/)
  assert.match(BRAND_ACTIVITY_TOOLTIPS.keywordShare, /가로 합이 100% 를 넘습니다/)
  assert.match(BRAND_ACTIVITY_TOOLTIPS.keywordCross, /선택 조건을 만족하는 그 브랜드의 전체 행 수/)
  assert.notEqual(BRAND_ACTIVITY_TOOLTIPS.keywordShare, BRAND_ACTIVITY_TOOLTIPS.keywordCross)
  assert.match(DEEP_ANALYSIS_TOOLTIPS.reference, /최대 24시간 재사용/)
  assert.doesNotMatch(DEEP_ANALYSIS_TOOLTIPS.forecast, /Phase|Prophet|SARIMAX/)
})

test('growth matrix and forecast copy correct the public factual errors', () => {
  assert.match(CHART_TOOLTIPS.growthMatrix, /세로축 : 성장 기여율\(%\)/)
  assert.match(CHART_TOOLTIPS.growthMatrix, /금액이 아니라 비율/)
  assert.match(CHART_TOOLTIPS.growthMatrix, /버블 크기 : 최근 시점 매출/)
  assert.match(CHART_TOOLTIPS.growthMatrix, /버블 색 : 두 축이 만드는 네 개 영역 구분/)
  assert.match(CHART_TOOLTIPS.growthMatrix, /가로 기준선 : 성장 기여율 0%/)
  assert.doesNotMatch(CHART_TOOLTIPS.growthMatrix, /최신값과 1년 전|Momentum Score를 사용/)

  assert.match(DEEP_ANALYSIS_TOOLTIPS.forecast, /예측값 자체를 이은 선/)
  assert.match(DEEP_ANALYSIS_TOOLTIPS.forecast, /오차 범위가 아닙니다/)
  assert.match(DEEP_ANALYSIS_TOOLTIPS.forecast, /Simulation 카드/)
  assert.match(DEEP_ANALYSIS_TOOLTIPS.forecast, /과거 5년과 미래 5년/)
  assert.match(DEEP_ANALYSIS_TOOLTIPS.simulation, /과거 5년과 미래 5년으로 고정/)
  assert.doesNotMatch(DEEP_ANALYSIS_TOOLTIPS.simulation, /과거·미래 표시 기간/)
  assert.doesNotMatch(DEEP_ANALYSIS_TOOLTIPS.forecast, /예측 구간/)
})

test('market analysis copy exposes the verified scale, interval, and denominator', () => {
  assert.match(CHART_TOOLTIPS.brandTrajectory, /버블 크기 : 최근 시점 매출/)
  assert.match(CHART_TOOLTIPS.brandTrajectory, /매출의 제곱근에 비례하며 최소 크기가 있습니다/)
  assert.match(CHART_TOOLTIPS.brandTrajectory, /버블 색상 : 네 가지 구분/)
  assert.match(CHART_TOOLTIPS.brandTrajectory, /강한 가속 · 완만한 가속 · 완만한 감속 · 강한 감속/)
  assert.match(CHART_TOOLTIPS.brandTrajectory, /세로 기준선 : 화면에 표시된 브랜드들의 평균 시장점유율/)
  assert.match(CHART_TOOLTIPS.brandTrajectory, /한 관측 간격당 점유율 변화\(%p\)/)
  assert.match(CHART_TOOLTIPS.brandTrajectory, /UBIST는 월, IQVIA NSA는 분기/)
  assert.match(CHART_TOOLTIPS.brandTrajectory, /시장이 역성장 중이면 부호가 뒤집혀/)
  assert.match(CHART_TOOLTIPS.marketContributionBrand, /전 브랜드를 더하면 100%/)
  assert.match(CHART_TOOLTIPS.marketContributionBrand, /상위 6개와 나머지 합계/)
  assert.match(CHART_TOOLTIPS.marketContributionCompany, /상위 6개와 나머지 합계/)
  assert.doesNotMatch(CHART_TOOLTIPS.marketContributionBrand, /Evolution Index/)
  assert.doesNotMatch(CHART_TOOLTIPS.marketContributionCompany, /Evolution Index/)
  assert.match(CHART_TOOLTIPS.brandTrajectory, /Evolution Index/)
  assert.doesNotMatch(CHART_TOOLTIPS.brandTrajectory, /버블 색상 진하기|브랜드 CAGR \(초록|가로 기준선/)

  const marketSize = buildMarketSizeGrowthTooltip('매출', 'CMGR')
  assert.match(marketSize, /표시 구간 : UBIST는 최근 60개 월 구간, IQVIA NSA는 최근 20개 분기 구간/)
  assert.match(marketSize, /최초값 : 표시 구간에서 가장 오래된 유효값/)
  assert.match(marketSize, /정확히 5년 전 값이 있으면 그 값을 사용/)
  assert.match(marketSize, /실제 경과 월 또는 분기 수/)
  assert.doesNotMatch(marketSize, /기준 시점의 5년 전 값으로 고정|구간 수/)

  const tooltips = buildDynamicChartTooltips({
    referenceLabel: '2026년 2분기',
    rankingToggle: '브랜드',
    m5Label: '처방량',
    m7Label: '매출',
    m8Label: '처방량',
  })
  assert.match(tooltips.hhi, /0~10000/)
  assert.match(tooltips.hhi, /1500 미만.*1500~2499.*2500 이상/)
  assert.match(tooltips.hhi, /상위 N개나 '기타'가 아니라 해당 범위의 전 브랜드/)
  assert.match(tooltips.levelSalesMs, /선택한 분석 레벨 값으로 좁혀진 시장 안에서 다시 계산/)
  assert.match(tooltips.levelSalesMs, /같은 필터 범위의 전 브랜드 합/)
  assert.match(tooltips.top5CustomerTrend, /표시 구간 : 최근 10개 구간만 표시합니다/)
  assert.match(tooltips.top5CustomerTrend, /UBIST는 10개월, IQVIA NSA는 10분기/)
  assert.match(tooltips.top5CustomerTrend, /기간 선택도 이 범위 안에서만 가능합니다/)
  assert.match(tooltips.levelTop5Trend, /표시 구간 : 최근 10개 구간만 표시합니다/)
})

test('brand activity copy exposes source labels, weights, and scoped denominators', () => {
  assert.match(BRAND_ACTIVITY_TOOLTIPS.channelShare, /선택한 CSD 시장과 선택한 채널 안/)
  assert.match(BRAND_ACTIVITY_TOOLTIPS.channelShare, /전 채널 합이 아닙니다/)
  assert.match(BRAND_ACTIVITY_TOOLTIPS.interest, /VERY USEFUL.*SOMEWHAT USEFUL.*NOT AT ALL/)
  assert.doesNotMatch(BRAND_ACTIVITY_TOOLTIPS.interest, /NOT USEFUL/)
  assert.match(BRAND_ACTIVITY_TOOLTIPS.perception, /현재 선택 브랜드의 일반뷰 ATC4 범위/)
  assert.match(BRAND_ACTIVITY_TOOLTIPS.perception, /종별과 진료과 전체/)
  assert.match(BRAND_ACTIVITY_TOOLTIPS.perception, /Keyword와 CSD 데이터가 함께 존재하는 전체 월 구간/)
  assert.match(BRAND_ACTIVITY_TOOLTIPS.perception, /FREQUENTLY 1.*OCCASIONALLY 0\.6.*LAPSED 0\.3.*NEVER\/NEW TO ME 0/s)
  assert.match(BRAND_ACTIVITY_TOOLTIPS.perception, /INCREASE 1.*UNCHANGED 0\.5.*DECREASE 0/s)
  assert.match(BRAND_ACTIVITY_TOOLTIPS.perception, /응답 수의 제곱근에 비례하며 최소 크기가 있습니다/)
  assert.match(BRAND_ACTIVITY_TOOLTIPS.perception, /버블 색상 : 브랜드를 구분합니다 \(값의 크기와는 무관합니다\)/)
  assert.match(BRAND_ACTIVITY_TOOLTIPS.perception, /십자선 : 화면에 표시된 브랜드들의 평균/)
  assert.doesNotMatch(BRAND_ACTIVITY_TOOLTIPS.perception, /Interest|선택한 시장·브랜드·종별·진료과·기간/)
})

test('dynamic tooltip substitutions use explicit labels', () => {
  const tooltips = buildDynamicChartTooltips({
    referenceLabel: '2026년 2분기',
    rankingToggle: '브랜드',
    m5Label: '처방량',
    m7Label: '매출',
    m8Label: '처방량',
  })

  assert.match(tooltips.hhi, /완전한 연도\(calendar year\)별 집계/)
  assert.match(tooltips.rank, /2026년 2분기/)
  assert.match(tooltips.rank, /선택 브랜드와 경쟁 상위 5개를 고정해/)
  assert.match(tooltips.rank, /선택 브랜드 \+ 경쟁 상위 5개 \+ 기타/)
  assert.match(tooltips.rank, /선택 브랜드와 경쟁 상위 5개를 제외한 모든 브랜드의 연도별 합/)
  assert.doesNotMatch(
    tooltips.rank,
    /조회 범위 전체 합계로 상위 5개 브랜드를 고정해|• 대상 : 상위 5개 브랜드 \+ 기타/,
  )
  assert.match(tooltips.levelSalesTrend, /처방량/)
  assert.doesNotMatch(Object.values(tooltips).join('\n'), /undefined/)
  assert.doesNotMatch(Object.values(tooltips).join('\n'), /브랜드을|회사을/)
})

test('all seven M/S tooltip surfaces explain the filtered denominator', () => {
  const primary = buildDynamicChartTooltips({
    referenceLabel: '2026년 7월',
    rankingToggle: '브랜드',
    m5Label: '매출',
    m7Label: '매출',
    m8Label: '매출',
  })
  const customerLevel = buildDynamicChartTooltips({
    referenceLabel: '2026년 7월',
    rankingToggle: '브랜드',
    m5Label: '처방량',
    m7Label: '매출',
    m8Label: '매출',
  })
  const surfaces = [
    CHART_TOOLTIPS.brandTrajectory,
    CHART_TOOLTIPS.growthMatrix,
    primary.rank,
    primary.levelSalesMs,
    customerLevel.levelSalesMs,
    primary.top5CustomerMs,
    primary.levelTop5Ms,
  ]

  assert.equal(surfaces.length, 7)
  for (const copy of surfaces) {
    assert.match(copy, /M\/S는 선택한 분석 레벨 값으로 좁혀진 시장 안에서 다시 계산합니다/)
    assert.match(copy, /분모는 같은 필터 범위의 전 브랜드 합입니다/)
    assert.match(copy, /필터를 바꾸면 M\/S도 달라질 수 있습니다/)
  }
})

test('empty dynamic substitutions never render undefined', () => {
  const tooltips = buildDynamicChartTooltips({
    referenceLabel: undefined,
    rankingToggle: '',
    m5Label: undefined,
    m7Label: '',
    m8Label: undefined,
  })
  const marketSize = buildMarketSizeGrowthTooltip(undefined, undefined)
  const all = [...Object.values(tooltips), marketSize].join('\n')

  assert.doesNotMatch(all, /undefined/)
  assert.match(all, /기준 시점/)
  assert.match(all, /지표/)
})

test('missing tooltip keys have a visible non-empty fallback', () => {
  assert.equal(tooltipOrFallback(undefined), '설명 정보가 없습니다.')
  assert.equal(tooltipOrFallback('   '), '설명 정보가 없습니다.')
})

test('long approved copy remains newline-delimited for the fixed tooltip width', () => {
  const longCopies = [
    BRAND_ACTIVITY_TOOLTIPS.keywordShare,
    buildMarketSizeGrowthTooltip('매출', 'CMGR'),
  ]

  for (const copy of longCopies) {
    assert.ok(copy.length > 300)
    assert.ok(copy.split('\n').length >= 8)
    assert.ok(copy.split('\n').every(line => line.length < 100))
  }

  for (const copy of [BRAND_ACTIVITY_TOOLTIPS.perception, CHART_TOOLTIPS.growthMatrix]) {
    assert.ok(copy.length > 200)
    assert.ok(copy.split('\n').length >= 8)
    assert.ok(copy.split('\n').every(line => line.length < 100))
  }
})

test('market contribution copy follows the selectable window and six-item payload', () => {
  assert.match(CHART_TOOLTIPS.marketContributionBrand, /선택한 1~5년 구간/)
  assert.equal(CHART_TOOLTIPS.marketContributionBrand.match(/상위 6개/g)?.length, 2)
  assert.equal(CHART_TOOLTIPS.marketContributionCompany.match(/상위 6개/g)?.length, 2)
  assert.doesNotMatch(
    `${CHART_TOOLTIPS.marketContributionBrand}\n${CHART_TOOLTIPS.marketContributionCompany}`,
    /상위 8개/,
  )
  assert.doesNotMatch(CHART_TOOLTIPS.marketContributionBrand, /최신 시점과 1년 전/)
})

test('chart tooltips use a viewport-bounded width', () => {
  const css = readFileSync(new URL('../src/styles/common.css', import.meta.url), 'utf8')
  const rule = css.match(/\.chart-section \.btn-icon-info \.chart-tooltip \{([\s\S]*?)\n\}/)?.[1] ?? ''
  assert.match(rule, /width:\s*min\(448px, calc\(100vw - 64px\)\)/)
  assert.match(rule, /min-width:\s*0/)
  assert.match(rule, /overflow-wrap:\s*anywhere/)
})
