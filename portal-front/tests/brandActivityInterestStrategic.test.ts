import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import {
  buildInterestSeriesRequest,
  interestErrorReason,
  readStrategicInterestMarkets,
  selectInterestDisplayItem,
} from '../src/utils/brandActivityInterest.ts'
import { buildCsdMarketOptions } from '../src/utils/brandActivityCsdMarket.ts'
import type { StrategicInterestMarket } from '../src/utils/brandActivityInterest.ts'

const read = (path: string) => readFileSync(new URL(path, import.meta.url), 'utf8')

test('wires the strategic catalog identity into the INTEREST request path', () => {
  const page = read('../src/pages/AnalyzePage.tsx')
  const tab = read('../src/components/main/BrandActivityTab.tsx')
  const request = buildInterestSeriesRequest({
    selectedBrand: '리바로젯',
    market: { viewKind: 'strategic_ml', marketId: 'ml_006', marketName: '리바로 리바로젯' },
    visit: 'HOSPITAL',
    specialty: '전체',
    csdMarket: 'LIVALOZET',
  })

  assert.match(page, /strategicMarkets=/)
  assert.match(tab, /buildCsdMarketOptions\(interestData\?\.scope\?\.csd_markets \?\? \[\]\)/)
  assert.equal(request.view, 'strategic_ml')
  assert.equal(request.market_id, 'ml_006')
  assert.deepEqual(request.visit_location, ['HOSPITAL'])
  assert.deepEqual(request.specialty, [])
  assert.equal(request.csd_market, 'LIVALOZET')
})

test('uses the selected catalog context kind for ML and CD requests', () => {
  const mlRequest = buildInterestSeriesRequest({
    selectedBrand: '리바로젯',
    market: { viewKind: 'strategic_ml', marketId: 'ml_006', marketName: '리바로 리바로젯' },
  })
  const cdRequest = buildInterestSeriesRequest({
    selectedBrand: '리바로젯',
    market: { viewKind: 'strategic_cd', marketId: 'cd_006', marketName: '리바로 리바로젯' },
  })

  assert.equal(mlRequest.view, 'strategic_ml')
  assert.equal(cdRequest.view, 'strategic_cd')
})

test('rejects an unknown market identity without catalog kind instead of inferring from its prefix', () => {
  assert.throws(
    () => buildInterestSeriesRequest({
      selectedBrand: '리바로젯',
      market: { marketId: 'unknown_006', marketName: '알 수 없는 시장' } as StrategicInterestMarket,
    }),
    /market identity does not match catalog view kind/,
  )
})

test('sends visit location and specialty as arrays without source selection changes', () => {
  const request = read('../src/utils/brandActivityInterest.ts')
  const page = read('../src/pages/AnalyzePage.tsx')
  const tab = read('../src/components/main/BrandActivityTab.tsx')
  const brandActivityProps = page.match(/<BrandActivityTab[\s\S]*?\/>/)?.[0] ?? ''

  assert.match(request, /visit_location:\s*readonly string\[\]/)
  assert.match(request, /specialty:\s*readonly string\[\]/)
  assert.doesNotMatch(request, /allowsSourceSelection|readonly source\??:/)
  assert.doesNotMatch(brandActivityProps, /sourceToggle/)
  assert.doesNotMatch(tab, /sourceToggle:/)
})

test('uses the shared visit-location contract for INTEREST without changing channel values', () => {
  const tab = read('../src/components/main/BrandActivityTab.tsx')

  assert.match(tab, /const VISIT_LOCATION_VALUES = \['HOSPITAL', 'PRIV\. PRACTICE'\] as const/)
  assert.match(tab, /const INT_VISIT_OPTS = visitLocationOptions\('종별 전체'\)/)
  assert.match(tab, /const CATEGORY_OPTS = visitLocationOptions\('전체 종별'\)/)
  assert.match(tab, /const CHANNEL_OPTS:[\s\S]*?value: 'GH'[\s\S]*?value: 'SHPPI'[\s\S]*?value: 'CPPI'/)
})

test('keeps only valid catalog markets for hidden identity and displays CSD market names', () => {
  const markets = readStrategicInterestMarkets(
    '리바로젯',
    'strategic_ml',
    { viewKind: 'strategic_ml', marketId: 'ml_006', marketName: '리바로 리바로젯' },
    {
      getItem: () => JSON.stringify([{
        brand: '리바로젯',
        contexts: [
          { view_kind: 'strategic_ml', market_id: 'ml_006', market_name: '리바로 리바로젯', has_market_data: true },
          { view_kind: 'strategic_ml', market_id: 'ml_999', market_name: '무효 시장', has_market_data: false },
          { view_kind: 'general', market_id: 'C10C', market_name: '일반 시장', has_market_data: true },
        ],
      }]),
    },
  )

  assert.deepEqual(markets, [{ viewKind: 'strategic_ml', marketId: 'ml_006', marketName: '리바로 리바로젯' }])
  assert.deepEqual(buildCsdMarketOptions(['LIVALOZET', 'LIVALO']), [
    { value: 'all', label: '시장 전체' },
    { value: 'LIVALOZET', label: 'LIVALOZET' },
    { value: 'LIVALO', label: 'LIVALO' },
  ])
  assert.match(read('../src/utils/brandActivityInterest.ts'), /context\.view_kind !== viewKind/)
})

test('keeps the Keyword axis independent without changing INTEREST source selection', () => {
  const tab = read('../src/components/main/BrandActivityTab.tsx')
  const topicPeriod = read('./brandActivityTopicPeriod.test.ts')

  assert.match(tab, /useTopicSourceMonths\(productName, atcKey, activityScope\)/)
  assert.doesNotMatch(tab, /resolveTopicMonths\(s17\.fullMonths, intMonths\)/)
  assert.match(topicPeriod, /keyword-source/)
  assert.doesNotMatch(tab, /allowsSourceSelection/)
})

test('surfaces missing identity and the bounded BFF failure reason', () => {
  const tab = read('../src/components/main/BrandActivityTab.tsx')
  const request = read('../src/utils/brandActivity.ts')

  assert.match(tab, /시장 정보를 불러오지 못했습니다/)
  assert.match(tab, /role="alert"/)
  assert.match(request, /interestErrorReason/)
  assert.doesNotMatch(tab, /fetchInterestSeries\([^\n]+\)\.then\([^\n]+\)\.catch\(\(\) => \{\}\)/)
  assert.equal(
    interestErrorReason(400, '{"detail":{"message":"market membership not found"}}'),
    'market membership not found',
  )
  assert.equal(interestErrorReason(502, 'upstream\nfailed'), 'upstream failed')
})

test('switches the rendered INTEREST series when the local company selection changes', () => {
  const jwSeries = { '2026-07': { total_count: 31 } }
  const astraSeries = { '2026-07': { total_count: 8 } }
  const companies = [
    { key: 'JW중외제약', series: jwSeries, is_selected: true },
    { key: '아스트라제네카', series: astraSeries },
  ]

  assert.equal(selectInterestDisplayItem(companies, 'JW중외제약')?.series, jwSeries)
  assert.equal(selectInterestDisplayItem(companies, '아스트라제네카')?.series, astraSeries)
  assert.notEqual(
    selectInterestDisplayItem(companies, 'JW중외제약')?.series,
    selectInterestDisplayItem(companies, '아스트라제네카')?.series,
  )

  const tab = read('../src/components/main/BrandActivityTab.tsx')
  assert.match(tab, /setIntDisp\(\{ rank: intRankDraft, brand: effIntBrandDraft/)
  assert.match(tab, /<InterestPerceptionChart series=\{intSel\?\.series \?\? null\}/)
})
