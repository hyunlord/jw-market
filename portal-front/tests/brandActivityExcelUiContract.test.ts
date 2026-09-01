import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const source = readFileSync(new URL('../src/components/main/BrandActivityTab.tsx', import.meta.url), 'utf8')

test('keyword mode selection refreshes data and exports the applied mode', () => {
  // Given: the keyword cross control can switch between two API filters.
  // When/Then: selection applies the matching filter and export receives that mode.
  assert.match(source, /applyKeywordMode\(v\)/)
  assert.match(source, /keywordCrossDomain\(matrixData, appliedEvo20\)/)
  assert.match(source, /fetchKeywordCrossDatasets/)
  assert.match(source, /mode: appliedEvo20/)
  assert.match(source, /t20\.data\?\.scope\.period_start \?\? d20\.from/)
  assert.match(source, /선택한 기준의 엑셀 데이터를 불러오지 못했습니다/)
  assert.doesNotMatch(source, /excelTopics\('키워드 X Prescription Evolution', t20\.data\)/)
})

test('keyword cross reset applies the API default period without stale date state', () => {
  assert.match(source, /t20\.apply\(evo20 === 'interest' \? \{ interest: '전체' \} : \{ prescription_evolution: '전체' \}\)/)
})
