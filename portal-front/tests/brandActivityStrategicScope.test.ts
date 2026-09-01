import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import {
  buildBrandActivityScopeRequest,
  resolveBrandActivityScope,
} from '../src/utils/brandActivityScope.ts'

const read = (path: string) => readFileSync(new URL(path, import.meta.url), 'utf8')

test('resolves one market scope for every Brand Activity request', () => {
  const general = resolveBrandActivityScope(undefined)
  const ml = resolveBrandActivityScope({
    viewKind: 'strategic_ml',
    marketId: 'ml_006',
    marketName: '리바로 리바로젯',
  })
  const cd = resolveBrandActivityScope({
    viewKind: 'strategic_cd',
    marketId: 'cd_006',
    marketName: '리바로 리바로젯',
  })

  assert.deepEqual(general, { view: 'general' })
  assert.deepEqual(ml, { view: 'strategic_ml', marketId: 'ml_006' })
  assert.deepEqual(cd, { view: 'strategic_cd', marketId: 'cd_006' })
})

test('keeps ATC filters for general and uses only strategic market identity for ML and CD', () => {
  const filters = {
    atc: { atc4: ['C10C'] },
    channel: { visit_location: [], specialty: [] },
  }

  assert.deepEqual(
    buildBrandActivityScopeRequest({ view: 'general' }, filters),
    { view: 'general', filters },
  )
  assert.deepEqual(
    buildBrandActivityScopeRequest({ view: 'strategic_ml', marketId: 'ml_006' }, filters),
    { view: 'strategic_ml', market_id: 'ml_006', filters: {} },
  )
  assert.deepEqual(
    buildBrandActivityScopeRequest({ view: 'strategic_cd', marketId: 'cd_006' }, filters),
    { view: 'strategic_cd', market_id: 'cd_006', filters: {} },
  )
})

test('routes every non-INTEREST section through the shared page market scope', () => {
  const tab = read('../src/components/main/BrandActivityTab.tsx')
  const api = read('../src/utils/brandActivity.ts')

  assert.match(tab, /resolveBrandActivityScope\(strategicMarkets\[0\]\)/)
  assert.doesNotMatch(tab, /view:\s*'general'/)
  assert.doesNotMatch(api, /view:\s*'general'/)
  assert.doesNotMatch(api, /view\s*=\s*'general'/)
})
