// 심층분석(DeepAnalyzePage) 탭별 엑셀 내보내기 — 시트 빌드 로직 분리 (analyzeExcel.ts와 대칭).
// 페이지는 현재 탭/토글의 데이터만 넘기고, 시트 생성은 여기서. 값은 원본값(매출=원 / 처방량=Rx / M/S=%).
import { downloadExcel, round1, type ExcelColumn, type CellValue } from './exportExcel'
import type { ForecastCombo, SimBrandData } from '../types/market'

type Source = 'UBIST' | 'IQVIA'

// 매출 예측 / 처방량 예측 탭: 과거+예측 시계열, 브랜드별 매출 + M/S. 과거 5년/미래 5년(가용분).
export function exportForecast(p: {
  productName: string; sourceToggle: Source; isRevenue: boolean; referenceLabel: string
  unitMeasure?: string; fc: ForecastCombo
}): void {
  const { fc } = p
  const histLastP = fc.history_periods[fc.history_periods.length - 1]
  const fcStart = fc.forecast_periods[0] === histLastP ? 1 : 0
  const perYear = fc.period_unit === '분기' ? 4 : 12
  const histStart = Math.max(0, fc.history_periods.length - 5 * perYear)
  const histPeriods = fc.history_periods.slice(histStart)
  const fcPeriods = fc.forecast_periods.slice(fcStart, fcStart + 5 * perYear)
  const mLabel = p.isRevenue ? '매출' : '처방량'
  const valSuffix = p.isRevenue ? '매출(원)' : '처방량'
  const cols: ExcelColumn[] = [
    { header: '기간', key: 'period', width: 14 },
    { header: '구분', key: 'kind', width: 8 },
  ]
  fc.brands.forEach(b => {
    cols.push({ header: `${b.brand} ${valSuffix}`, key: `v_${b.brand}`, width: 18, numFmt: '#,##0' })
    cols.push({ header: `${b.brand} M/S(%)`, key: `m_${b.brand}`, width: 12, numFmt: '0.0' })
  })
  const rows: Record<string, CellValue>[] = []
  histPeriods.forEach((period, i) => {
    const gi = histStart + i
    const row: Record<string, CellValue> = { period, kind: '과거' }
    fc.brands.forEach(b => {
      row[`v_${b.brand}`] = b.history_values[gi] != null ? Math.round(b.history_values[gi]!) : null
      row[`m_${b.brand}`] = round1(b.history_ms_pct?.[gi])
    })
    rows.push(row)
  })
  fcPeriods.forEach((period, i) => {
    const gi = fcStart + i
    const row: Record<string, CellValue> = { period, kind: '예측' }
    fc.brands.forEach(b => {
      row[`v_${b.brand}`] = b.forecast_values[gi] != null ? Math.round(b.forecast_values[gi]!) : null
      row[`m_${b.brand}`] = round1(b.forecast_ms_pct?.[gi])
    })
    rows.push(row)
  })
  const unitTag = !p.isRevenue && p.sourceToggle === 'IQVIA' && p.unitMeasure ? `_${p.unitMeasure}` : ''
  downloadExcel(`${p.productName}_심층분석_${mLabel}예측_${p.sourceToggle}${unitTag}`, [
    { name: `${mLabel}예측`, meta: [`${p.productName} · 출처 ${p.sourceToggle} · ${mLabel}`, `기준 ${p.referenceLabel}`], columns: cols, rows },
  ])
}

// Simulation 탭: 선택 브랜드 1개 — 과거 + 기본/최고/최저 시나리오(미래 10년 전체).
export function exportSimulation(p: {
  productName: string; referenceLabel: string; simComboKey: string; brand: string; isVolume: boolean
  brandData: SimBrandData
}): void {
  const bd = p.brandData
  const histLastP = bd.history_periods[bd.history_periods.length - 1]
  const fcStart = bd.forecast_periods[0] === histLastP ? 1 : 0
  const fcPeriods = bd.forecast_periods.slice(fcStart)
  const valLabel = p.isVolume ? '처방량' : '매출(원)'
  const cols: ExcelColumn[] = [
    { header: '기간', key: 'period', width: 14 },
    { header: '구분', key: 'kind', width: 8 },
    { header: valLabel, key: 'value', width: 18, numFmt: '#,##0' },
    { header: '기본 예측', key: 'base', width: 18, numFmt: '#,##0' },
    { header: '최고 예측', key: 'upper', width: 18, numFmt: '#,##0' },
    { header: '최저 예측', key: 'lower', width: 18, numFmt: '#,##0' },
  ]
  const rows: Record<string, CellValue>[] = []
  bd.history_periods.forEach((period, i) => {
    rows.push({ period, kind: '과거', value: Math.round(bd.history_values[i] ?? 0), base: null, upper: null, lower: null })
  })
  const sc = bd.scenarios
  fcPeriods.forEach((period, i) => {
    const gi = fcStart + i
    rows.push({
      period, kind: '예측', value: null,
      base: sc.base?.values[gi] != null ? Math.round(sc.base.values[gi]!) : null,
      upper: sc.upper?.values[gi] != null ? Math.round(sc.upper.values[gi]!) : null,
      lower: sc.lower?.values[gi] != null ? Math.round(sc.lower.values[gi]!) : null,
    })
  })
  downloadExcel(`${p.productName}_심층분석_Simulation_${p.brand}_${p.simComboKey}`, [
    { name: 'Simulation', meta: [`${p.productName} · ${p.brand} · 시장 ${p.simComboKey} · 기준 ${p.referenceLabel}`], columns: cols, rows },
  ])
}
