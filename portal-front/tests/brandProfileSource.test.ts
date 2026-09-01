import test from 'node:test'
import assert from 'node:assert/strict'
import { buildSourceNativeBrandProfile } from '../src/utils/brandProfileSource.ts'
import type { BrandFactorEntry } from '../src/types/market.ts'

const entries: BrandFactorEntry[] = [
  {
    brand: '가드렛',
    brand_key: '가드렛',
    factors: {
      available: true,
      values: {
        seller: ['JW중외제약'],
        molecule_strength: ['anagliptin 100mg', 'anagliptin 50mg'],
        form: [],
        custom_factor: ['원천 고유값'],
      },
    },
    strength: {
      profile_display: { headline: '렌더링하면 안 되는 재가공 값' },
      strength_items: [{ axis: 'growth', narrative: 'UBIST 기준 강점' }],
      limitations: ['화면에 노출하면 안 됨'],
    },
  },
  {
    brand: '제미글로',
    factors: {
      available: false,
      reason: '내부 원천 사유',
      values: {},
    },
    strength: {
      strength_items: [{ axis: 'growth', narrative: '두 번째 브랜드 강점' }],
    },
  },
]

const factors = {
  ubist: entries,
  iqvia: [{
    brand: 'IQVIA 전용 브랜드',
    factors: { available: true, values: { mfr_name_kor: ['IQVIA 제조사'] } },
  }],
}

test('renders only the selected source entries in backend order', () => {
  const profile = buildSourceNativeBrandProfile('UBIST', factors)

  assert.deepEqual(profile.brands.map(brand => brand.brand), ['가드렛', '제미글로'])
  assert.equal(profile.brands.some(brand => brand.brand === 'IQVIA 전용 브랜드'), false)
})

test('maps factors.values keys to rows without profile_display remapping', () => {
  const profile = buildSourceNativeBrandProfile('UBIST', factors)

  assert.deepEqual(profile.rows.map(row => row.label), ['판매사', '성분용량', 'custom_factor', '강점 분석'])
  assert.equal(profile.rows[0]?.values[0], 'JW중외제약')
  assert.equal(profile.rows[1]?.values[0], 'anagliptin 100mg · anagliptin 50mg')
  assert.equal(profile.rows[2]?.values[0], '원천 고유값')
  assert.equal(profile.rows.some(row => row.values.includes('렌더링하면 안 되는 재가공 값')), false)
})

test('hides empty factor rows and masks unavailable reasons', () => {
  const profile = buildSourceNativeBrandProfile('UBIST', factors)

  assert.equal(profile.rows.some(row => row.label === '제형'), false)
  assert.equal(profile.rows[0]?.values[1], '요소 정보 없음')
  assert.equal(profile.rows.some(row => row.values.includes('내부 원천 사유')), false)
})

test('uses strength items from the selected source entries only', () => {
  const profile = buildSourceNativeBrandProfile('UBIST', factors)
  const strength = profile.rows.find(row => row.highlight)

  assert.deepEqual(strength?.values, ['UBIST 기준 강점', '두 번째 브랜드 강점'])
})
