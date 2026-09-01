import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile } from 'node:fs/promises'

import {
  recordHeaderFor,
  shouldShowRecordIndex,
} from '../src/utils/structuredRecordTree.ts'

const tree = await readFile(new URL('../src/components/main/StructuredValueTree.tsx', import.meta.url), 'utf8')
const evidence = await readFile(new URL('../src/components/main/EvidencePopover.tsx', import.meta.url), 'utf8')

test('builds a numbered clinical record header from identifier and summary fields', () => {
  const header = recordHeaderFor({
    nct_id: 'NCT06317051',
    brief_title: 'Phase 3 study of amlitelimab',
    overall_status: 'RECRUITING',
  }, 0, '임상', 'clinicaltrials')

  assert.deepEqual(header, {
    ordinal: '#1',
    identifier: 'NCT06317051',
    summary: 'Phase 3 study of amlitelimab · RECRUITING',
  })
})

test('opens one or two compact records and only the first of three records', () => {
  assert.match(tree, /initialOpenRecordCount/)
  assert.match(tree, /Math\.min\(initialOpenRecordCount, Array\.isArray\(value\) \? value\.length : 0\)/)
})

test('falls back without inventing an identifier or summary', () => {
  assert.deepEqual(recordHeaderFor({ value: 42 }, 2, '항목'), {
    ordinal: '#3',
    identifier: '항목 3',
    summary: '상세 필드 1개',
  })
})

test('shows the identifier navigator only above twenty records', () => {
  assert.equal(shouldShowRecordIndex(20), false)
  assert.equal(shouldShowRecordIndex(21), true)
  assert.equal(shouldShowRecordIndex(244), true)
})

test('renders arrays as collapsed record blocks with controls instead of a flat table', () => {
  assert.match(tree, /<details[^>]+className="trace-record-block"/)
  assert.match(tree, /총 \{value\.length\}건/)
  assert.match(tree, /전체 펼치기/)
  assert.match(tree, /전체 접기/)
  assert.match(tree, /trace-record-index/)
  assert.match(tree, /data-record-identifier/)
  assert.doesNotMatch(tree, /ARRAY_PAGE_SIZE/)
  assert.doesNotMatch(tree, /더 보기/)
})

test('uses the same tree for the compact evidence record', () => {
  assert.match(evidence, /StructuredValueTree/)
  assert.match(evidence, /compactEvidenceRecord/)
  assert.doesNotMatch(evidence, /className="answer-evidence-record"/)
})
