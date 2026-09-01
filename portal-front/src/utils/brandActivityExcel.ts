// 원인분석 > 브랜드 활동 탭 엑셀 내보내기 — 화면 차트 데이터를 시트로 변환.
import { downloadExcel, round1, type ExcelColumn, type CellValue } from './exportExcel'
import type { SeriesData, TopicsData } from '../types/market'
import {
  buildActivityUnitExport,
  buildKeywordCrossExport,
  buildKeywordTopicsExport,
  type KeywordExportMode,
} from './brandActivityExcelSpec'
import type { KeywordCrossDataset } from './brandActivityKeywordCross.ts'

export type { KeywordExportMode } from './brandActivityExcelSpec'

export function exportCallSeries(p: { productName: string; section: string; data: SeriesData }): void {
  const months = p.data.scope.activity_months ?? []
  const brands = p.data.brands
  const columns: ExcelColumn[] = [{ header: '기간', key: 'period', width: 12 }]
  brands.forEach((b, i) => {
    columns.push({ header: `${b.brand_name} 콜 수`, key: `c${i}`, width: 12, numFmt: '#,##0' })
    columns.push({ header: `${b.brand_name} Share(%)`, key: `s${i}`, width: 14, numFmt: '0.0' })
  })
  const rows = months.map(mth => {
    const row: Record<string, CellValue> = { period: mth }
    brands.forEach((b, i) => {
      row[`c${i}`] = Math.round(b.series.activity.absolute[mth] ?? 0)
      row[`s${i}`] = round1(b.series.activity.ratio[mth])
    })
    return row
  })
  downloadExcel(`${p.productName}_원인분석_${p.section}`, [{
    name: '콜 수 Share',
    meta: [`${p.productName} · ${p.section} · 콜 수(활동량, 월 단위)`],
    columns, rows,
  }])
}

type IntArr = { count: (number | null)[]; pct: (number | null)[] }
export function exportInterestSeries(p: {
  productName: string; brandName: string; months: string[]
  very: IntArr; somewhat: IntArr; not: IntArr; total: (number | null)[]
}): void {
  const columns: ExcelColumn[] = [
    { header: '기간', key: 'period', width: 12 },
    { header: '총 콜 수', key: 'total', width: 12, numFmt: '#,##0' },
    { header: 'VERY 콜 수', key: 'vc', width: 12, numFmt: '#,##0' },
    { header: 'VERY 비율(%)', key: 'vp', width: 13, numFmt: '0.0' },
    { header: 'SOMEWHAT 콜 수', key: 'sc', width: 14, numFmt: '#,##0' },
    { header: 'SOMEWHAT 비율(%)', key: 'sp', width: 15, numFmt: '0.0' },
    { header: 'NOT 콜 수', key: 'nc', width: 12, numFmt: '#,##0' },
    { header: 'NOT 비율(%)', key: 'np', width: 13, numFmt: '0.0' },
  ]
  const rows = p.months.map((mth, i) => ({
    period: mth,
    total: p.total[i] ?? null,
    vc: p.very.count[i] ?? null,
    vp: p.very.pct[i] != null ? round1(p.very.pct[i]!) : null,
    sc: p.somewhat.count[i] ?? null,
    sp: p.somewhat.pct[i] != null ? round1(p.somewhat.pct[i]!) : null,
    nc: p.not.count[i] ?? null,
    np: p.not.pct[i] != null ? round1(p.not.pct[i]!) : null,
  }))
  downloadExcel(`${p.productName}_원인분석_INTEREST 응답 분포 추이`, [{
    name: 'INTEREST 응답 분포',
    meta: [`${p.productName} · ${p.brandName} · INTEREST 응답 분포 추이(월 단위)`],
    columns, rows,
  }])
}

export function exportKeywordTopics(p: { productName: string; section: string; data: TopicsData }): void {
  const result = buildKeywordTopicsExport(p)
  void downloadExcel(result.fileName, result.sheets)
}

export function exportKeywordCrossTopics(p: {
  productName: string
  mode: KeywordExportMode
  datasets: readonly KeywordCrossDataset[]
}): void {
  const result = buildKeywordCrossExport(p)
  void downloadExcel(result.fileName, result.sheets)
}

export function exportActivityUnit(p: { productName: string; brandName: string; data: SeriesData }): void {
  const result = buildActivityUnitExport(p)
  if (result) void downloadExcel(result.fileName, result.sheets)
}
