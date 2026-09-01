import type { SeriesData, TopicsData } from '../types/market.ts'
import type { CellValue, ExcelColumn, ExcelSheet } from './exportExcel.ts'
import type { KeywordCrossDataset } from './brandActivityKeywordCross.ts'

export type KeywordExportMode = 'interest' | 'prescription_evolution'

export type WorkbookExport = {
  readonly fileName: string
  readonly sheets: ExcelSheet[]
}

export class KeywordExportModeError extends Error {
  readonly value: unknown

  constructor(value: unknown) {
    super('keyword export mode is required')
    this.name = 'KeywordExportModeError'
    this.value = value
  }
}

export function parseKeywordExportMode(value: unknown): KeywordExportMode {
  if (value === 'interest' || value === 'prescription_evolution') return value
  throw new KeywordExportModeError(value)
}

function topicSheet(data: TopicsData, name: string, meta: string[]): ExcelSheet {
  const uniqueCount = data.brands.reduce(
    (maximum, brand) => Math.max(maximum, brand.brand_specific_topics.length),
    0,
  )
  const columns: ExcelColumn[] = [{ header: '브랜드', key: 'brand', width: 14 }]
  for (let rank = 1; rank <= 5; rank++) {
    columns.push({ header: `${rank}위 키워드`, key: `k${rank}`, width: 16 })
    columns.push({ header: `${rank}위 건수`, key: `kc${rank}`, width: 10, numFmt: '#,##0' })
    columns.push({ header: `${rank}위 비율(%)`, key: `kp${rank}`, width: 12, numFmt: '0.0' })
  }
  for (let index = 1; index <= uniqueCount; index++) {
    columns.push({ header: `고유 키워드 ${index}`, key: `uk${index}`, width: 16 })
    columns.push({ header: `고유 건수 ${index}`, key: `ukc${index}`, width: 10, numFmt: '#,##0' })
    columns.push({ header: `고유 비율 ${index}(%)`, key: `ukp${index}`, width: 12, numFmt: '0.0' })
  }

  const rows = data.brands.map(brand => {
    const row: Record<string, CellValue> = {
      brand: brand.brand_name,
      _jw: brand.is_jw ? 1 : 0,
    }
    for (let rank = 1; rank <= 5; rank++) {
      const topic = brand.topic_shares[rank - 1]
      row[`k${rank}`] = topic?.label ?? null
      row[`kc${rank}`] = topic?.row_count ?? null
      row[`kp${rank}`] = topic ? Math.round(topic.share_pct * 10) / 10 : null
    }
    brand.brand_specific_topics.forEach((topic, offset) => {
      const index = offset + 1
      row[`uk${index}`] = topic.label
      row[`ukc${index}`] = topic.row_count
        ?? Math.round((brand.event_count * topic.share_pct) / 100)
      row[`ukp${index}`] = Math.round(topic.share_pct * 10) / 10
    })
    return row
  })

  return {
    name,
    meta: uniqueCount === 0 ? [...meta, '고유 키워드 없음'] : meta,
    columns,
    rows,
    highlightRow: row => row._jw === 1,
  }
}

export function buildKeywordTopicsExport(input: {
  readonly productName: string
  readonly section: string
  readonly data: TopicsData
}): WorkbookExport {
  return {
    fileName: `${input.productName}_원인분석_${input.section}`,
    sheets: [topicSheet(input.data, '키워드 점유', [`${input.productName} · ${input.section}`])],
  }
}

const CROSS_MODE_LABELS = {
  interest: {
    section: '키워드 X INTEREST',
    sheet: '키워드×INTEREST',
  },
  prescription_evolution: {
    section: '키워드 X Prescription Evolution',
    sheet: '키워드×Prescription Evol',
  },
} as const

export function buildKeywordCrossExport(input: {
  readonly productName: string
  readonly mode: KeywordExportMode
  readonly datasets: readonly KeywordCrossDataset[]
}): WorkbookExport {
  const mode = parseKeywordExportMode(input.mode)
  const labels = CROSS_MODE_LABELS[mode]
  if (input.datasets.length === 0) throw new Error(`keyword cross datasets are required: ${mode}`)
  return {
    fileName: `${input.productName}_원인분석_${labels.section}`,
    sheets: input.datasets.map((dataset, index) => topicSheet(
      dataset.data,
      `${mode === 'interest' ? 'INTEREST' : 'Prescription'}_${String(index + 1).padStart(2, '0')}`,
      [
        `${input.productName} · ${labels.section}`,
        `조회 축 값: ${dataset.value}`,
      ],
    )),
  }
}

export function buildActivityUnitExport(input: {
  readonly productName: string
  readonly brandName: string
  readonly data: SeriesData
}): WorkbookExport | null {
  const selected = input.data.brands.find(brand => brand.brand_name === input.brandName)
    ?? input.data.brands.find(brand => brand.is_selected)
    ?? input.data.brands[0]
  if (!selected) return null

  const quarterRows = (input.data.scope.quarters ?? []).map(quarter => ({
    period: quarter,
    unit: Math.round(selected.series.unit.absolute[quarter] ?? 0),
    dosage: Math.round(selected.series.dosage_unit.absolute[quarter] ?? 0),
    count: Math.round(selected.series.counting_unit.absolute[quarter] ?? 0),
  }))
  const monthRows = (input.data.scope.activity_months ?? []).map(month => ({
    period: month,
    activity: Math.round(selected.series.activity.absolute[month] ?? 0),
  }))

  return {
    fileName: `${input.productName}_원인분석_활동량단위별처방량_${selected.brand_name}`,
    sheets: [{
      name: '활동량 & 처방량',
      meta: [`${input.productName} · ${selected.brand_name} · 처방량(분기)`],
      columns: [
        { header: '기간', key: 'period', width: 12 },
        { header: 'UNIT', key: 'unit', width: 16, numFmt: '#,##0' },
        { header: 'DOSAGE UNIT', key: 'dosage', width: 16, numFmt: '#,##0' },
        { header: 'COUNT UNIT', key: 'count', width: 16, numFmt: '#,##0' },
      ],
      rows: quarterRows,
    }, {
      name: '활동량(월별)',
      meta: monthRows.length === 0
        ? [`${input.productName} · ${selected.brand_name}`, '월별 활동량 데이터 없음']
        : [`${input.productName} · ${selected.brand_name} · 활동량(콜 수, 월 단위)`],
      columns: [
        { header: '기간', key: 'period', width: 12 },
        { header: '활동량(콜 수)', key: 'activity', width: 14, numFmt: '#,##0' },
      ],
      rows: monthRows,
    }],
  }
}
