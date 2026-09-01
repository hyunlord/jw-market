import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'

const read = (path: string) => readFileSync(new URL(path, import.meta.url), 'utf8')

test('brand activity charts preserve the backend cohort without a client-side top-five cut', () => {
  const source = read('../src/components/main/BrandActivityTab.tsx')
  const chartHelpers = read('../src/utils/chartHelpers.ts')

  assert.doesNotMatch(source, /callTop5WithTarget/)
  assert.match(source, /const brands = data\.brands \?\? \[\]/)
  assert.match(source, /brands\.map\(\(b, i\) => \(\{/)
  assert.match(source, /labels: brands\.map\(b => b\.brand_name\)/)
  assert.match(source, /ci\+\+ % COMPETITOR_PALETTE\.length/)

  const palette = chartHelpers.match(/COMPETITOR_PALETTE = \[([^\]]+)\]/)?.[1] ?? ''
  assert.equal(palette.match(/#[0-9A-F]{6}/gi)?.length, 5)
  assert.match(chartHelpers, /TARGET_COLOR = '#[0-9A-F]{6}'/i)
})

test('keyword rows surface the backend data status instead of silently showing only dashes', () => {
  const types = read('../src/types/market.ts')
  const grid = read('../src/components/main/KeywordShareGrid.tsx')
  const styles = read('../src/styles/common.css')

  assert.match(types, /data_status\?: TopicDataStatus/)
  assert.match(types, /identity_mismatch/)
  assert.match(grid, /brand\.data_status/)
  assert.match(grid, /dataStatus && statusLabel/)
  assert.match(grid, /brand-data-status/)
  assert.match(styles, /\.brand-data-status/)
})
