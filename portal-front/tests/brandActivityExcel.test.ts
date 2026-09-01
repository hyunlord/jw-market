import assert from 'node:assert/strict'
import test from 'node:test'

import {
  buildActivityUnitExport,
  buildKeywordCrossExport,
  buildKeywordTopicsExport,
  parseKeywordExportMode,
} from '../src/utils/brandActivityExcelSpec.ts'
import type { SeriesData, TopicsData } from '../src/types/market.ts'

const topicsData: TopicsData = {
  scope: {
    view: 'standard',
    market_id: 'C10A1',
    selected_brand: '리바로젯',
    top_n: 5,
  },
  brands: [{
    brand_name: '리바로젯',
    is_jw: true,
    is_selected: true,
    event_count: 100,
    etc_pct: 10,
    topic_shares: [
      { topic_id: 'topic-1', label: '효과', share_pct: 40, row_count: 40 },
    ],
    brand_specific_topics: [
      { label: '복약편의', share_pct: 12, row_count: 12, definition: '첫 번째 고유키워드' },
      { label: '복합제', share_pct: 8, row_count: 8, definition: '두 번째 고유키워드' },
    ],
  }],
}

const seriesData: SeriesData = {
  scope: {
    view: 'standard',
    market_id: 'C10A1',
    market_name: '지질조절제',
    selected_brand: { brand_key: 'livarozet', product_code: 'livarozet' },
    quarters: ['2026-Q1'],
    activity_months: ['2026-01', '2026-02', '2026-03'],
    measures: ['activity', 'unit', 'dosage_unit', 'counting_unit'],
  },
  brands: [{
    brand_name: '리바로젯',
    is_jw: true,
    is_selected: true,
    series: {
      activity: {
        source: 'brand_activity',
        absolute: { '2026-01': 10, '2026-02': 20, '2026-03': 30 },
        ratio: {},
      },
      unit: { source: 'iqvia', absolute: { '2026-Q1': 100 }, ratio: {} },
      dosage_unit: { source: 'iqvia', absolute: { '2026-Q1': 200 }, ratio: {} },
      counting_unit: { source: 'iqvia', absolute: { '2026-Q1': 300 }, ratio: {} },
    },
  }],
}

test('exports every brand-specific keyword group from the response', () => {
  // Given: one brand has two backend-provided unique keywords.
  // When: the keyword workbook contract is built.
  const result = buildKeywordTopicsExport({
    productName: '리바로젯',
    section: '브랜드별 키워드 점유 구조',
    data: topicsData,
  })

  // Then: both groups have their own label/count/share columns and values.
  const sheet = result.sheets[0]
  assert.ok(sheet)
  assert.deepEqual(
    sheet.columns.slice(-6).map(column => column.header),
    ['고유 키워드 1', '고유 건수 1', '고유 비율 1(%)', '고유 키워드 2', '고유 건수 2', '고유 비율 2(%)'],
  )
  assert.equal(sheet.rows[0]?.uk1, '복약편의')
  assert.equal(sheet.rows[0]?.uk2, '복합제')
})

test('exports monthly activity without inventing monthly prescription values', () => {
  // Given: activity is monthly while prescription measures are quarterly.
  // When: the activity workbook contract is built.
  const result = buildActivityUnitExport({
    productName: '리바로젯',
    brandName: '리바로젯',
    data: seriesData,
  })

  // Then: the existing quarterly prescription sheet remains and monthly activity is explicit.
  assert.deepEqual(result.sheets.map(sheet => sheet.name), ['활동량 & 처방량', '활동량(월별)'])
  assert.deepEqual(result.sheets[1]?.rows, [
    { period: '2026-01', activity: 10 },
    { period: '2026-02', activity: 20 },
    { period: '2026-03', activity: 30 },
  ])
})

test('separates keyword cross exports by the applied mode', () => {
  // Given: each mode has catalog-derived slices with different topic data.
  const interestData = structuredClone(topicsData)
  interestData.brands[0]!.topic_shares[0]!.label = '복약 편의성'
  const prescriptionData = structuredClone(topicsData)
  prescriptionData.brands[0]!.topic_shares[0]!.label = '처방 증가 의향'

  // When: both workbook contracts are built.
  const interest = buildKeywordCrossExport({
    productName: '리바로젯',
    mode: 'interest',
    datasets: [{ value: 'VERY USEFUL', data: interestData }],
  })
  const prescription = buildKeywordCrossExport({
    productName: '리바로젯',
    mode: 'prescription_evolution',
    datasets: [{ value: 'increase', data: prescriptionData }],
  })

  // Then: labels and the actual exported topic rows identify different data contracts.
  assert.notEqual(interest.fileName, prescription.fileName)
  assert.notEqual(interest.sheets[0]?.name, prescription.sheets[0]?.name)
  assert.notDeepEqual(interest.sheets[0]?.meta, prescription.sheets[0]?.meta)
  assert.equal(interest.sheets[0]?.rows[0]?.k1, '복약 편의성')
  assert.equal(prescription.sheets[0]?.rows[0]?.k1, '처방 증가 의향')
})

test('keeps every catalog-derived mode slice in a separate sheet', () => {
  const result = buildKeywordCrossExport({
    productName: '리바로젯',
    mode: 'interest',
    datasets: [
      { value: 'VERY USEFUL', data: topicsData },
      { value: 'SOMEWHAT USEFUL', data: topicsData },
    ],
  })

  assert.deepEqual(result.sheets.map(sheet => sheet.name), [
    'INTEREST_01',
    'INTEREST_02',
  ])
  assert.match(result.sheets[0]?.meta?.[1] ?? '', /VERY USEFUL/)
  assert.match(result.sheets[1]?.meta?.[1] ?? '', /SOMEWHAT USEFUL/)
})

test('fails closed when a keyword cross export has no mode', () => {
  // Given: a JavaScript caller omits the runtime mode.
  // When/Then: export construction fails instead of silently returning the other mode.
  assert.throws(
    () => parseKeywordExportMode(undefined),
    /keyword export mode is required/,
  )
})

test('states explicitly when no brand-specific keyword exists', () => {
  const noUnique = structuredClone(topicsData)
  noUnique.brands[0]!.brand_specific_topics = []
  const result = buildKeywordTopicsExport({
    productName: '리바로젯',
    section: '브랜드별 키워드 점유 구조',
    data: noUnique,
  })

  assert.match(result.sheets[0]?.meta?.join('\n') ?? '', /고유 키워드 없음/)
})

test('states explicitly when monthly activity is unavailable', () => {
  const noMonths = structuredClone(seriesData)
  noMonths.scope.activity_months = []
  noMonths.brands[0]!.series.activity.absolute = {}
  const result = buildActivityUnitExport({
    productName: '리바로젯',
    brandName: '리바로젯',
    data: noMonths,
  })

  assert.match(result?.sheets[1]?.meta?.join('\n') ?? '', /월별 활동량.*없음/)
})
