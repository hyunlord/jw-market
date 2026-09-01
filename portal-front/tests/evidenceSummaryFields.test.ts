import assert from 'node:assert/strict'
import test from 'node:test'

import {
  evidenceSummaryRows,
  evidenceSummarySourceKey,
  recordSummaryForSource,
} from '../src/utils/evidenceSummaryFields.ts'

test('selects clinical summary fields in the approved order including dates', () => {
  const record = {
    nct_id: 'NCT06553157',
    brief_title: 'Statins Effect on Incidence of Diabetes',
    overall_status: 'COMPLETED',
    phases: ['PHASE3'],
    sponsor: 'JW Pharmaceutical',
    start_date: '2024-01-11',
    completion_date: '2026-07-31',
    internal_note: 'preserved in full detail only',
  }

  assert.equal(evidenceSummarySourceKey('ClinicalTrials.gov', 'ct:NCT06553157', record), 'clinicaltrials')
  assert.deepEqual(evidenceSummaryRows('clinicaltrials', record), [
    { key: 'nct_id', label: 'NCT 번호', value: 'NCT06553157', missing: false },
    { key: 'brief_title', label: '시험명', value: 'Statins Effect on Incidence of Diabetes', missing: false },
    { key: 'overall_status', label: '상태', value: 'COMPLETED', missing: false },
    { key: 'phases', label: '단계', value: ['PHASE3'], missing: false },
    { key: 'sponsor', label: '스폰서', value: 'JW Pharmaceutical', missing: false },
    { key: 'start_date', label: '시작일', value: '2024-01-11', missing: false },
    { key: 'completion_date', label: '완료일', value: '2026-07-31', missing: false },
  ])
})

test('keeps approved date rows visible when the source did not provide a value', () => {
  const rows = evidenceSummaryRows('patent', {
    DOMESTIC_PATENT_NO: '10-0777553',
    DOMESTIC_INVN_NM: 'PROCESS FOR PRODUCING OPTICALLY ACTIVE ETHYL',
    PATENTEE: '닛산 가가쿠',
    DOMESTIC_PATENT_STATUS: '소멸',
  })

  assert.deepEqual(rows.map(row => row.label), ['특허번호', '명칭', '출원일', '등록일', '권리자', '상태'])
  assert.equal(rows.find(row => row.label === '출원일')?.value, '원천 미제공')
  assert.equal(rows.find(row => row.label === '등록일')?.missing, true)
})

test('uses the same approved fields for concise clinical record headers', () => {
  assert.deepEqual(recordSummaryForSource('clinicaltrials', {
    nct_id: 'NCT06553157',
    brief_title: 'Statins Effect on Incidence of Diabetes',
    overall_status: 'COMPLETED',
  }), {
    identifier: 'NCT06553157',
    summary: 'Statins Effect on Incidence of Diabetes · COMPLETED',
  })
})

test('supports the localized clinical catalog shape without promoting the title to identifier', () => {
  assert.deepEqual(evidenceSummaryRows('clinicaltrials', {
    시험명: 'Pitavastatin study',
    상태: 'COMPLETED',
  }).slice(0, 3), [
    { key: 'nct_id', label: 'NCT 번호', value: '원천 미제공', missing: true },
    { key: '시험명', label: '시험명', value: 'Pitavastatin study', missing: false },
    { key: '상태', label: '상태', value: 'COMPLETED', missing: false },
  ])

  assert.deepEqual(recordSummaryForSource('clinicaltrials', {
    시험명: 'Pitavastatin study',
    상태: 'COMPLETED',
  }), {
    identifier: undefined,
    summary: 'Pitavastatin study · COMPLETED',
  })
})

test('returns no summary rows for an unregistered source so the current renderer remains the fallback', () => {
  assert.equal(evidenceSummarySourceKey('새 원천', 'new:1', { id: '1' }), undefined)
  assert.deepEqual(evidenceSummaryRows(undefined, { id: '1', value: 'kept' }), [])
})
