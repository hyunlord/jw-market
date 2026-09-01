import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { runInNewContext } from 'node:vm'
import { ModuleKind, ScriptTarget, transpileModule } from 'typescript'

type TopicFilters = {
  readonly visit_location?: string
  readonly specialty?: string
  readonly interest?: string
  readonly prescription_evolution?: string
  readonly period_start?: string
  readonly period_end?: string
}

type MarketScope = {
  readonly view: 'general'
} | {
  readonly view: 'strategic_ml'
  readonly marketId: string
}

type FetchCall = {
  readonly selectedBrand: string
  readonly atc4: readonly string[]
  readonly scope: MarketScope
  readonly filters: TopicFilters
}

type QueryResult = {
  readonly data: unknown
  readonly isLoading: boolean
  readonly apply: (filters: TopicFilters | null) => void
}

type HookHarness = {
  readonly calls: FetchCall[]
  readonly render: (scope?: MarketScope) => QueryResult
  readonly flush: () => Promise<void>
}

const componentSource = readFileSync(
  new URL('../src/components/main/BrandActivityTab.tsx', import.meta.url),
  'utf8',
)

function extractTopicsHook(): string {
  const start = componentSource.indexOf('function useTopicsQuery(')
  const end = componentSource.indexOf('\nfunction TopicsQueryResult', start)
  assert.notEqual(start, -1, 'useTopicsQuery must exist')
  assert.notEqual(end, -1, 'TopicsQueryResult must follow useTopicsQuery')
  return componentSource.slice(start, end)
}

function readQueryResult(value: unknown): QueryResult {
  assert.equal(typeof value, 'object')
  assert.notEqual(value, null)
  const apply = Reflect.get(value, 'apply')
  const isLoading = Reflect.get(value, 'isLoading')
  assert.equal(typeof apply, 'function')
  assert.equal(typeof isLoading, 'boolean')
  return {
    data: Reflect.get(value, 'data'),
    isLoading,
    apply: filters => {
      Reflect.apply(apply, undefined, [filters])
    },
  }
}

function createHookHarness(): HookHarness {
  const compiled = transpileModule(extractTopicsHook(), {
    compilerOptions: {
      module: ModuleKind.None,
      target: ScriptTarget.ES2023,
    },
  }).outputText
  const states: unknown[] = []
  const calls: FetchCall[] = []
  const defaultScope: MarketScope = { view: 'general' }
  let cursor = 0
  let previousDependencies: readonly unknown[] | undefined
  let pendingEffect: (() => void | (() => void)) | undefined
  let cleanup: (() => void) | undefined

  const useState = (initial: unknown): readonly [unknown, (next: unknown) => void] => {
    const index = cursor
    cursor += 1
    if (!(index in states)) states[index] = initial
    return [
      states[index],
      next => {
        states[index] = typeof next === 'function'
          ? Reflect.apply(next, undefined, [states[index]])
          : next
      },
    ]
  }
  const useEffect = (
    effect: () => void | (() => void),
    dependencies: readonly unknown[],
  ): void => {
    const changed = previousDependencies === undefined
      || dependencies.length !== previousDependencies.length
      || dependencies.some((dependency, index) => !Object.is(dependency, previousDependencies?.[index]))
    if (changed) pendingEffect = effect
    previousDependencies = dependencies
  }
  const fetchTopics = (
    selectedBrand: string,
    atc4: readonly string[],
    _scope: MarketScope,
    filters: TopicFilters,
  ): Promise<{ readonly marker: number }> => {
    calls.push({ selectedBrand, atc4, scope: _scope, filters })
    return Promise.resolve({ marker: calls.length })
  }
  const context: Record<string, unknown> = {
    brandActivityScopeKey: (scope: MarketScope) => (
      scope.view === 'general' ? scope.view : `${scope.view}:${scope.marketId}`
    ),
    fetchTopics,
    useEffect,
    useState,
  }
  runInNewContext(`${compiled}\nresult = useTopicsQuery`, context)
  const hook = context.result
  assert.equal(typeof hook, 'function')

  return {
    calls,
    render: (scope = defaultScope) => {
      cursor = 0
      const query = readQueryResult(Reflect.apply(hook, undefined, [
        '리바로',
        'C10A1',
        scope,
        '2025-06',
        '2026-05',
      ]))
      const effect = pendingEffect
      pendingEffect = undefined
      if (effect) {
        cleanup?.()
        cleanup = effect() ?? undefined
      }
      return query
    },
    flush: async () => {
      await Promise.resolve()
      await Promise.resolve()
    },
  }
}

async function applyFilterAndAssertRefetch(filter: TopicFilters): Promise<void> {
  const harness = createHookHarness()
  harness.render()
  assert.equal(harness.calls.length, 1)
  await harness.flush()
  let query = harness.render()
  assert.equal(query.isLoading, false)

  query.apply({
    ...filter,
    period_start: '2025-06',
    period_end: '2026-05',
  })
  query = harness.render()

  assert.equal(query.isLoading, true, 'a filter-only change must create a new request key')
  assert.equal(harness.calls.length, 2, 'a filter-only change must issue a second request')
  const forwarded = harness.calls[1]?.filters
  assert.equal(forwarded?.period_start, '2025-06')
  assert.equal(forwarded?.period_end, '2026-05')
  assert.equal(forwarded?.visit_location, filter.visit_location)
  assert.equal(forwarded?.specialty, filter.specialty)
  assert.equal(forwarded?.interest, filter.interest)
  assert.equal(forwarded?.prescription_evolution, filter.prescription_evolution)

  await harness.flush()
  query = harness.render()
  assert.deepEqual(query.data, { marker: 2 }, 'the filtered response must replace the prior data')
}

test('refetches the brand keyword chart when visit location changes without a date change', async () => {
  await applyFilterAndAssertRefetch({
    visit_location: 'HOSPITAL',
    specialty: '전체',
  })
})
test('refetches the brand keyword chart when specialty changes without a date change', async () => {
  await applyFilterAndAssertRefetch({
    visit_location: '전체',
    specialty: 'Cardio',
  })
})

test('refetches the keyword cross chart when interest changes without a date change', async () => {
  await applyFilterAndAssertRefetch({
    interest: 'VERY USEFUL',
  })
})

test('refetches the keyword cross chart when prescription evolution changes without a date change', async () => {
  await applyFilterAndAssertRefetch({
    prescription_evolution: 'decrease',
  })
})

test('resets topic filters to the existing all-value fallback', async () => {
  const harness = createHookHarness()
  harness.render()
  await harness.flush()
  let query = harness.render()

  query.apply({
    visit_location: 'HOSPITAL',
    period_start: '2025-06',
    period_end: '2026-05',
  })
  harness.render()
  await harness.flush()
  query = harness.render()

  query.apply(null)
  query = harness.render()

  assert.equal(query.isLoading, true)
  assert.equal(harness.calls.length, 3)
  assert.equal(harness.calls[2]?.filters.period_start, '2025-06')
  assert.equal(harness.calls[2]?.filters.period_end, '2026-05')
  assert.equal(harness.calls[2]?.filters.visit_location, undefined)
  assert.equal(harness.calls[2]?.filters.specialty, undefined)
  assert.equal(harness.calls[2]?.filters.interest, undefined)
  assert.equal(harness.calls[2]?.filters.prescription_evolution, undefined)
})

test('refetches topics when the page switches from general to strategic ML', async () => {
  const harness = createHookHarness()
  harness.render()
  await harness.flush()
  harness.render()

  const query = harness.render({ view: 'strategic_ml', marketId: 'ml_006' })

  assert.equal(query.isLoading, true)
  assert.equal(harness.calls.length, 2)
  assert.deepEqual(harness.calls[1]?.scope, { view: 'strategic_ml', marketId: 'ml_006' })
})
