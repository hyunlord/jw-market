import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { stripTypeScriptTypes } from 'node:module'
import test from 'node:test'

type ToggleContext = {
  readonly selected: string[]
  readonly selectedInList: string[]
  readonly allVals: string[]
}

type ToggleOne = (value: string, checked: boolean) => void
type ToggleAll = (checked: boolean) => void

const panelSource = readFileSync(
  new URL('../src/components/main/MarketFilterPanel.tsx', import.meta.url),
  'utf8',
)

function extractFunction(name: 'toggleAll' | 'toggleOne', nextName: 'toggleOne' | 'countForDim') {
  const match = panelSource.match(
    new RegExp(`  const ${name} = ([\\s\\S]*?)\\n\\n  const ${nextName}`),
  )
  assert.ok(match?.[1], `${name} source must be present`)
  return stripTypeScriptTypes(`const ${name} = ${match[1]}`)
}

function instantiateToggle<T extends ToggleAll | ToggleOne>(
  name: 'toggleAll' | 'toggleOne',
  nextName: 'toggleOne' | 'countForDim',
  context: ToggleContext,
  onNext: (next: string[]) => void,
): T {
  const body = extractFunction(name, nextName)
  const create = new Function(
    'selected',
    'selectedInList',
    'allVals',
    'activeDimEff',
    'setDimSelected',
    `${body}; return ${name};`,
  )
  return create(
    context.selected,
    context.selectedInList,
    context.allVals,
    'molecule',
    (_dimension: string, next: string[]) => onNext(next),
  ) as T
}

function visibleSelection(selected: string[], visible: string[]) {
  return selected.filter(value => visible.includes(value))
}

test('accumulates selections across two molecule search terms', () => {
  // Given
  const atorvastatin = ['atorvastatin 10mg', 'atorvastatin 20mg']
  const pitavastatin = ['pitavastatin 1mg', 'pitavastatin 2mg', 'pitavastatin 4mg']
  let selected = [...atorvastatin]

  // When
  for (const value of pitavastatin) {
    const toggleOne = instantiateToggle<ToggleOne>(
      'toggleOne',
      'countForDim',
      {
        selected,
        selectedInList: visibleSelection(selected, pitavastatin),
        allVals: pitavastatin,
      },
      next => { selected = next },
    )
    toggleOne(value, true)
  }

  // Then
  assert.deepEqual(selected, [...atorvastatin, ...pitavastatin])
  const applyPayload = { molecule: [...selected] }
  assert.deepEqual(applyPayload.molecule, [...atorvastatin, ...pitavastatin])
})

test('select all and clear all preserve selections hidden by the search term', () => {
  // Given
  const hidden = 'atorvastatin 10mg'
  const visible = ['pitavastatin 1mg', 'pitavastatin 2mg', 'pitavastatin 4mg']
  let selected = [hidden]

  // When
  const selectAll = instantiateToggle<ToggleAll>(
    'toggleAll',
    'toggleOne',
    { selected, selectedInList: [], allVals: visible },
    next => { selected = next },
  )
  selectAll(true)

  // Then
  assert.deepEqual(selected, [hidden, ...visible])

  // When
  const clearAll = instantiateToggle<ToggleAll>(
    'toggleAll',
    'toggleOne',
    {
      selected,
      selectedInList: visibleSelection(selected, visible),
      allVals: visible,
    },
    next => { selected = next },
  )
  clearAll(false)

  // Then
  assert.deepEqual(selected, [hidden])
})
