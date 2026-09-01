import assert from 'node:assert/strict'
import test from 'node:test'

import { reportArtifactsToMarkdown } from '../src/utils/reportExport.ts'

test('exports every structured table row and chart data without silent omission', () => {
  const markdown = reportArtifactsToMarkdown({
    tables: [{
      table_id: 'sales', title: '매출 표', source_label: '내부 데이터마트', row_count: 2,
      omitted_columns: [],
      columns: [
        { key: 'month', label: '월', type: 'text', unit: null, align: 'left' },
        { key: 'sales', label: '매출', type: 'number', unit: '원', align: 'right' },
      ],
      rows: [
        { record_id: 'r1', cells: { month: '1월', sales: 100 } },
        { record_id: 'r2', cells: { month: '2월', sales: 200 } },
      ],
    }],
    charts: [{
      chart_id: 'trend', chart_type: 'line', title: '매출 추이', source_label: '내부 데이터마트',
      x_label: '월', unit: '원', x: ['1월', '2월'], series: [{ label: '리바로', values: [100, 200] }],
    }],
  })
  assert.match(markdown, /\| 1월 \| 100원 \|/)
  assert.match(markdown, /\| 2월 \| 200원 \|/)
  assert.match(markdown, /차트는 데이터 표로 대체했습니다/)
  assert.match(markdown, /\| 2월 \| 200원 \|/)
  assert.equal((markdown.match(/\| 2월 \| 200원 \|/g) ?? []).length, 2)
})

test('escapes markdown table delimiters and preserves source captions', () => {
  const markdown = reportArtifactsToMarkdown({
    tables: [{
      table_id: 'one', title: '표', source_label: 'A|B', row_count: 1, omitted_columns: [],
      columns: [{ key: 'value', label: '값', type: 'text', unit: null, align: 'left' }],
      rows: [{ record_id: 'r', cells: { value: 'x|y\nnext' } }],
    }],
    charts: [],
  })
  assert.match(markdown, /A\\\|B/)
  assert.match(markdown, /x\\\|y<br>next/)
  assert.match(markdown, /전체 1건/)
})
