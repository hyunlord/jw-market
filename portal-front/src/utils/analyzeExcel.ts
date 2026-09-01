// 원인분석(AnalyzePage) 차트별 엑셀 내보내기 — 화면에 보이는 데이터를 시트로 변환.
// AnalyzePage 렌더 스코프가 비대해지지 않도록 핸들러 바디를 여기로 분리. 페이지는 얇은 호출부만 유지.
// 값은 원본값(매출=원 / 처방량=Rx / M/S=%) 그대로 — 환산 안 함.
import { downloadExcel, round1, type ExcelColumn, type CellValue } from './exportExcel'
import type { RankItem, GrowthContributor, CompanyContributor } from '../types/market'

type Source = 'UBIST' | 'IQVIA'
type Measure = 'sales' | 'volume'

// 차트1: Market Size & Growth — 기간 / 시장 규모(원) / 성장률(UBIST=CMGR / IQVIA=CQGR, mom_growth_pct)
export function exportChart1(p: {
  productName: string; sourceToggle: Source; w1Measure: Measure; w1Period: string
  w1Series: { period: string; value: number }[]; yoyData: (number | null)[]
}): void {
  const measureLabel = p.w1Measure === 'sales' ? '매출' : '처방량'
  const valueHeader = p.w1Measure === 'sales' ? '시장 규모(원)' : '시장 규모(처방량)'
  const growthHeader = p.sourceToggle === 'IQVIA' ? 'CQGR(%)' : 'CMGR(%)'
  const unitLabel = p.w1Period === 'yearly' ? '년' : p.w1Period === 'quarterly' ? '분기' : '월'
  const rows = p.w1Series.map((pt, i) => ({ period: pt.period, value: Math.round(pt.value), yoy: round1(p.yoyData[i]) }))
  downloadExcel(`${p.productName}_원인분석_MarketSizeGrowth_${p.sourceToggle}_${measureLabel}`, [{
    name: 'Market Size & Growth',
    meta: [`${p.productName} · 출처 ${p.sourceToggle} · ${measureLabel}`, `기간 단위: ${unitLabel}`],
    columns: [
      { header: '기간', key: 'period', width: 14 },
      { header: valueHeader, key: 'value', width: 22, numFmt: '#,##0' },
      { header: growthHeader, key: 'yoy', width: 16, numFmt: '0.0' },
    ],
    rows,
  }])
}

// 차트2: HHI + 경쟁순위 (브랜드/회사 토글 반영) — 2시트
export function exportChart2(p: {
  productName: string; sourceToggle: Source; rankingToggle: '브랜드' | '회사'
  w2HhiFiltered: { year: string; hhi: number }[]; rankYears: string[]; rankKeyArr: string[]
  lastDisplayYear: string
  itemIn: (year: string, key: string) => RankItem | undefined
  rankLabelOf: (key: string) => string
  hiddenNormalKeys?: string[]
}): void {
  const hiddenKeys = p.hiddenNormalKeys ?? []
  const isComp = p.rankingToggle === '회사'
  const suffix = isComp ? '회사' : '브랜드'
  const meta = [`${p.productName} · 출처 ${p.sourceToggle} · ${suffix} 기준`]
  const rankCols: ExcelColumn[] = [{ header: suffix, key: 'name', width: 16 }]
  if (!isComp) rankCols.push({ header: '회사', key: 'company', width: 16 })
  p.rankYears.forEach(y => {
    rankCols.push({ header: `${y} 순위`, key: `r_${y}`, width: 9, numFmt: '0' })
    rankCols.push({ header: `${y} M/S(%)`, key: `m_${y}`, width: 12, numFmt: '0.0' })
    rankCols.push({ header: `${y} 매출(원)`, key: `v_${y}`, width: 18, numFmt: '#,##0' })
  })
  const rankRows = p.rankKeyArr.map(key => {
    const it0 = p.itemIn(p.lastDisplayYear, key) ?? p.itemIn(p.rankYears[0]!, key)
    const isOthers = it0?.is_others === true
    const row: Record<string, CellValue> = { name: p.rankLabelOf(key), _t: it0?.is_target ? 1 : 0 }
    if (!isComp) row.company = it0?.company ?? ''
    p.rankYears.forEach(y => {
      const it = p.itemIn(y, key)
      const hMs = isOthers ? hiddenKeys.reduce((s, hk) => s + (p.itemIn(y, hk)?.ms_pct ?? 0), 0) : 0
      const hVal = isOthers ? hiddenKeys.reduce((s, hk) => s + (p.itemIn(y, hk)?.value ?? 0), 0) : 0
      row[`r_${y}`] = it?.rank ?? null
      row[`m_${y}`] = isOthers ? round1((it?.ms_pct ?? 0) + hMs) : round1(it?.ms_pct)
      row[`v_${y}`] = isOthers ? Math.round((it?.value ?? 0) + hVal) : (it?.value != null ? Math.round(it.value) : null)
    })
    return row
  })
  downloadExcel(`${p.productName}_원인분석_HHI경쟁순위_${p.sourceToggle}_${suffix}`, [
    {
      name: `HHI_${suffix}`, meta,
      columns: [{ header: '연도', key: 'year', width: 10 }, { header: 'HHI', key: 'hhi', width: 12, numFmt: '0.00' }],
      rows: p.w2HhiFiltered.map(h => ({ year: h.year, hhi: h.hhi })),
    },
    { name: `경쟁순위_${suffix}`, meta, columns: rankCols, rows: rankRows, highlightRow: r => r._t === 1 },
  ])
}

// 차트7: 주요 고객별 Top5 — 현재 view·측정·단위 반영. 매출 시계열 + 최근 점유율 2시트
export function exportChart7(p: {
  productName: string; sourceToggle: Source; w7Measure: Measure; targetName: string
  custTrendBrands: { brand: string; value_series: number[] }[]
  w7Periods: string[]; w7Idxs: number[]
  compositionMap: Map<string, number>; extraOthersPct: number
}): void {
  const mLabel = p.w7Measure === 'sales' ? '매출' : '처방량'
  const valSuffix = p.w7Measure === 'sales' ? '매출(원)' : '처방량'
  const trendCols: ExcelColumn[] = [{ header: '기간', key: 'period', width: 14 }]
  p.custTrendBrands.forEach(b => trendCols.push({ header: `${b.brand} ${valSuffix}`, key: `b_${b.brand}`, width: 18, numFmt: '#,##0' }))
  const trendRows = p.w7Periods.map((period, pi) => {
    const row: Record<string, CellValue> = { period }
    p.custTrendBrands.forEach(b => {
      const v = b.value_series[p.w7Idxs[pi]!]
      row[`b_${b.brand}`] = v == null ? null : Math.round(v)
    })
    return row
  })
  const msRows = p.custTrendBrands.map(b => ({
    brand: b.brand,
    ms: round1(b.brand === '기타' ? (p.compositionMap.get(b.brand) ?? 0) + p.extraOthersPct : (p.compositionMap.get(b.brand) ?? 0)),
  }))
  downloadExcel(`${p.productName}_원인분석_주요고객Top5_${p.targetName}_${p.sourceToggle}_${mLabel}`, [
    { name: `${mLabel}_시계열`, meta: [`${p.productName} · ${p.targetName} · 출처 ${p.sourceToggle} · ${mLabel}`], columns: trendCols, rows: trendRows },
    { name: '최근 점유율', columns: [{ header: '브랜드', key: 'brand', width: 16 }, { header: 'M/S(%)', key: 'ms', width: 12, numFmt: '0.0' }], rows: msRows, highlightRow: r => r.brand === p.productName },
  ])
}

// 차트8: 분석 레벨별 Top5 — 현재 레벨·sub·측정·단위 반영. 매출+M/S 시계열
export function exportChart8(p: {
  productName: string; sourceToggle: Source; w8Measure: Measure
  levelLabel: string; subValue: string
  lvBrands: { brand: string; value_series_10pt: number[]; ms_series_10pt: number[] }[]
  w8Periods: string[]
}): void {
  const mLabel = p.w8Measure === 'sales' ? '매출' : '처방량'
  const valSuffix = p.w8Measure === 'sales' ? '매출(원)' : '처방량'
  const cols: ExcelColumn[] = [{ header: '기간', key: 'period', width: 14 }]
  p.lvBrands.forEach(b => {
    cols.push({ header: `${b.brand} ${valSuffix}`, key: `v_${b.brand}`, width: 18, numFmt: '#,##0' })
    cols.push({ header: `${b.brand} M/S(%)`, key: `m_${b.brand}`, width: 12, numFmt: '0.0' })
  })
  const rows = p.w8Periods.map((period, pi) => {
    const row: Record<string, CellValue> = { period }
    p.lvBrands.forEach(b => {
      const v = b.value_series_10pt[pi]
      row[`v_${b.brand}`] = v == null ? null : Math.round(v)
      row[`m_${b.brand}`] = round1(b.ms_series_10pt[pi])
    })
    return row
  })
  downloadExcel(`${p.productName}_원인분석_레벨별Top5_${p.levelLabel}_${p.subValue}_${p.sourceToggle}_${mLabel}`, [
    { name: `${mLabel}_시계열`, meta: [`${p.productName} · Level ${p.levelLabel} / ${p.subValue} · 출처 ${p.sourceToggle} · ${mLabel}`], columns: cols, rows },
  ])
}

// 차트5·6: 분석 레벨별 매출 추이 & M/S (공용 컴포넌트 AnalysisLevelChart에서 호출).
// 컴포넌트 내부 state(level/channel/measure) 기반이라 트리거는 컴포넌트에 있지만, 시트 빌드는 여기로 통일.
export function exportLevelChart(p: {
  productName: string; sourceToggle: Source; measure: Measure; title: string; level: string; channel: string
  items: { name: string; value_series: number[] }[]; periods: string[]
  ms: { name: string; share: number | null }[]
}): void {
  const mLabel = p.measure === 'sales' ? '매출' : '처방량'
  const valSuffix = p.measure === 'sales' ? '매출(원)' : '처방량'
  const trendCols: ExcelColumn[] = [{ header: '기간', key: 'period', width: 14 }]
  p.items.forEach(it => trendCols.push({ header: `${it.name} ${valSuffix}`, key: `c_${it.name}`, width: 18, numFmt: '#,##0' }))
  const trendRows = p.periods.map((period, pi) => {
    const row: Record<string, CellValue> = { period }
    p.items.forEach(it => { const v = it.value_series[pi]; row[`c_${it.name}`] = v == null ? null : Math.round(v) })
    return row
  })
  const msRows = p.ms.map(m => ({ name: m.name, ms: round1(m.share) }))
  const safeTitle = p.title.replace(/[\\/?*:[\]]/g, ' ')
  downloadExcel(`${p.productName}_원인분석_${safeTitle}_${p.level}_${p.channel}_${p.sourceToggle}_${mLabel}`, [
    { name: `${mLabel}_시계열`, meta: [`${p.productName} · ${p.title}`, `Level ${p.level} / 채널 ${p.channel} · 출처 ${p.sourceToggle} · ${mLabel}`], columns: trendCols, rows: trendRows },
    { name: '최근 M-S', columns: [{ header: '카테고리', key: 'name', width: 20 }, { header: 'M/S(%)', key: 'ms', width: 12, numFmt: '0.0' }], rows: msRows, highlightRow: r => r.name === p.productName },
  ])
}

// 차트9: 시장 매출변화 기여도 (Waterfall) — 현재 윈도우 반영. 브랜드/회사 2시트
export function exportChart9(p: {
  productName: string; sourceToggle: Source; window: string
  gc: { period_start?: string; period_end?: string; market_start?: number; market_end?: number } | undefined
  gcContribs: GrowthContributor[]; gcOthers: number; gcOthersPct: number; gcBackendHasOthers: boolean
  ccContribs: CompanyContributor[]; ccOthersValue: number; ccOthersPct: number; ccBackendHasOthers: boolean
}): void {
  const winLabel = ({ '1y': '1년', '2y': '2년', '3y': '3년', '4y': '4년', '5y': '5년' } as Record<string, string>)[p.window] ?? p.window
  const cols: ExcelColumn[] = [
    { header: '항목', key: 'item', width: 24 },
    { header: '기여액(원)', key: 'value', width: 20, numFmt: '#,##0' },
    { header: '기여(%)', key: 'pct', width: 12, numFmt: '0.0' },
  ]
  const startRow = { item: `(시작) ${p.gc?.period_start ?? ''} 시장`, value: Math.round(p.gc?.market_start ?? 0), pct: null }
  const endRow = { item: `(끝) ${p.gc?.period_end ?? ''} 시장`, value: Math.round(p.gc?.market_end ?? 0), pct: null }
  const brandRows: Record<string, CellValue>[] = [startRow]
  p.gcContribs.forEach(c => brandRows.push({ item: c.brand, value: Math.round(c.contribution), pct: round1(c.contribution_pct) }))
  if (!p.gcBackendHasOthers) brandRows.push({ item: '기타', value: Math.round(p.gcOthers), pct: round1(p.gcOthersPct) })
  brandRows.push(endRow)
  const compRows: Record<string, CellValue>[] = [startRow]
  p.ccContribs.forEach(c => compRows.push({ item: c.company, value: Math.round(c.contribution), pct: round1(c.contribution_pct) }))
  if (!p.ccBackendHasOthers) compRows.push({ item: '기타', value: Math.round(p.ccOthersValue), pct: round1(p.ccOthersPct) })
  compRows.push(endRow)
  const meta = [`${p.productName} · 출처 ${p.sourceToggle} · ${winLabel}`, `기간: ${p.gc?.period_start ?? ''} ~ ${p.gc?.period_end ?? ''}`]
  downloadExcel(`${p.productName}_원인분석_시장매출변화기여도_${p.sourceToggle}_${winLabel}`, [
    { name: '브랜드 기여도', meta, columns: cols, rows: brandRows },
    { name: '회사 기여도', meta, columns: cols, rows: compRows },
  ])
}
