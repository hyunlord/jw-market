import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import * as csdMarket from '../src/utils/brandActivityCsdMarket.ts'

test('uses the Keyword source bounds instead of another card axis', () => {
  const resolveTopicMonths = Reflect.get(csdMarket, 'resolveTopicMonths')
  assert.equal(typeof resolveTopicMonths, 'function')
  if (typeof resolveTopicMonths !== 'function') return

  assert.deepEqual(
    resolveTopicMonths({ available_start: '2026-04', available_end: '2026-05' }),
    ['2026-04', '2026-05'],
  )
})

test('keeps the Keyword axis empty until its own source bounds arrive', () => {
  const resolveTopicMonths = Reflect.get(csdMarket, 'resolveTopicMonths')
  assert.equal(typeof resolveTopicMonths, 'function')
  if (typeof resolveTopicMonths !== 'function') return

  assert.deepEqual(resolveTopicMonths(null), [])
})

test('rejects a reversed Keyword source range', () => {
  const resolveTopicMonths = Reflect.get(csdMarket, 'resolveTopicMonths')
  assert.equal(typeof resolveTopicMonths, 'function')
  if (typeof resolveTopicMonths !== 'function') return

  assert.deepEqual(
    resolveTopicMonths({ available_start: '2026-05', available_end: '2026-04' }),
    [],
  )
})

test('wires the Keyword source period into both controls without channel or Interest fallback', () => {
  const source = readFileSync(new URL('../src/components/main/BrandActivityTab.tsx', import.meta.url), 'utf8')

  assert.match(source, /const \{ months: topicMonths, error: topicPeriodError \} = useTopicSourceMonths\(productName, atcKey, activityScope\)/)
  assert.doesNotMatch(source, /resolveTopicMonths\(s17\.fullMonths, intMonths\)/)
  assert.match(source, /const d18 = useDateRange\(\[\.\.\.topicMonths\]\)/)
  assert.match(source, /const d20 = useDateRange\(\[\.\.\.topicMonths\]\)/)
  assert.match(source, /data-brand-activity-topic-period="keyword-source"/)
})
