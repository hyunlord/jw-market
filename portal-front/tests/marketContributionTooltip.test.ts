import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const analyzePage = readFileSync(
  new URL('../src/pages/AnalyzePage.tsx', import.meta.url),
  'utf8',
)

test('Market Contribution popup shows signed amount and the existing contribution ratio', () => {
  assert.match(
    analyzePage,
    /`- 성장기여액 : \$\{sign\}\$\{fmtBaekman\(delta\)\}`/,
  )
  assert.match(analyzePage, /const pct = label === '기타' \? othersPct/)
  assert.match(analyzePage, /contribs\[idx - 1\]\?\.contribution_pct/)
  assert.match(analyzePage, /pct == null \? '-' : `\$\{pct >= 0 \? '\+' : ''\}/)
  assert.match(analyzePage, /pct\.toFixed\(1\)/)
  assert.match(analyzePage, /`- 성장기여도 : \$\{pctStr\}`/)
  assert.doesNotMatch(analyzePage, /- 매출변동 :/)
  assert.match(analyzePage, /const sign = delta >= 0 \? '\+' : ''/)
})
