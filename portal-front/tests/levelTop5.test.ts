import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

import { isLevelTop5Others, levelTop5BrandLabel, levelTop5EmptyMessage } from '../src/utils/levelTop5.ts'

test('keeps a named brand when rank is unavailable', () => {
  const brand = {
    brand: '페바로젯',
    data_quality: { available: false, reason: 'no_data_in_widget_scope' },
  }

  assert.equal(isLevelTop5Others(brand), false)
  assert.equal(levelTop5BrandLabel(brand), '페바로젯 (데이터 없음)')
})

test('uses the others label only for an explicit others row', () => {
  const brand = { brand: '기타', is_others: true }

  assert.equal(isLevelTop5Others(brand), true)
  assert.equal(levelTop5BrandLabel(brand), '기타')
})

test('does not show an error before the selected value is available', () => {
  assert.equal(levelTop5EmptyMessage(undefined), null)
})

test('explains a missing selected period', () => {
  assert.equal(levelTop5EmptyMessage({
    brands_in_value: [],
    data_quality: { available: false, reason: 'dimension_period_missing' },
  }), '선택한 기간에 분석 데이터가 없습니다.')
})

test('explains a selected condition with no data', () => {
  assert.equal(levelTop5EmptyMessage({
    brands_in_value: [],
    data_quality: { available: false, reason: 'no_data' },
  }), '선택한 조건에 분석 데이터가 없습니다.')
})

test('uses a neutral message when an empty response has no reason', () => {
  assert.equal(levelTop5EmptyMessage({ brands_in_value: [] }), '데이터를 표시할 수 없습니다. 조회 조건을 확인해 주세요.')
})

test('explains filtered member scope suppression without calling it no-data', () => {
  assert.equal(levelTop5EmptyMessage({
    brands_in_value: [],
    data_quality: { available: false, reason: 'filtered_member_scope_unavailable' },
  }), '필터 적용 시 축별 분해를 제공하지 않습니다. 전체 금액과 전체 시장점유율은 선택한 필터 기준으로 표시됩니다.')
})

test('keeps a real zero-valued series renderable', () => {
  assert.equal(levelTop5EmptyMessage({
    brands_in_value: [{ brand: '리바로젯', value_series_10pt: [0], ms_series_10pt: [0] }],
  }), null)
})

test('renders the empty-state contract in the analysis-level Top5 chart', () => {
  const source = readFileSync(new URL('../src/pages/AnalyzePage.tsx', import.meta.url), 'utf8')
  const chartStart = source.indexOf('분석 레벨별 Top5 브랜드 매출 추이 및 M/S')
  const chartEnd = source.indexOf('시장 매출변화 기여도', chartStart)
  const chartSource = source.slice(chartStart, chartEnd)

  assert.ok(chartStart >= 0)
  assert.ok(chartEnd > chartStart)
  assert.match(chartSource, /lvTop5EmptyMessage/)
  assert.match(chartSource, /className="chart-empty-state" role="status"/)
  assert.match(source, /levelTop5TerminalState/)
  assert.match(source, /isFilteredMemberScopeUnavailable/)
  assert.match(source, /filtered_member_scope_unavailable/)
  assert.match(source, /isFilteredMemberScopeUnavailable \? '축별 분해 미제공' : '데이터 없음'/)
  const emptyBranch = chartSource.indexOf('lvTop5EmptyMessage ?')
  const salesSkeleton = chartSource.indexOf('<ChartSkeleton legendItems={6}')
  const shareSkeleton = chartSource.indexOf('<ChartSkeleton legendItems={0}')
  assert.ok(emptyBranch >= 0)
  assert.ok(salesSkeleton > emptyBranch)
  assert.ok(shareSkeleton > emptyBranch)
})
