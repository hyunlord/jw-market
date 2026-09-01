// 차트 5 "분석 Level별 매출 추이 및 M/S" + 신규 "주요 고객별 분석 Level 시장 현황" 공용 컴포넌트
// 좌측 라인(매출/처방량 추이) + 우측 PolarArea(M/S). 데이터 소스만 다른 두 차트가 재사용.
// 내부 상태(level/channel/period/from/to)는 각 인스턴스 독립. 날짜는 data prop timeline으로 sync.
import { useState } from 'react'
import { Chart as ChartJS } from 'chart.js'
import { Line, PolarArea } from 'react-chartjs-2'
import DateRangePicker from './DateRangePicker'
import ChartSkeleton from './ChartSkeleton'
import MeasureToggle from './MeasureToggle'
import SelectBox from '../ui/SelectBox'
import type { AnalysisLevels, AnalysisItem, SelectOption } from '../../types/market'
import {
  CHART_COLORS, TARGET_COLOR, OTHERS_COLOR,
  fmtPeriodKor, fmtBaekman, ymStartOf, ymEndOf,
  commonOpts, legendBottom, legendHoverPointer,
  opaqueLabelsForArc, onPeriodInputChange,
  aggregatePeriodsAndItems, type PeriodUnit,
} from '../../utils/chartHelpers'
import { TooltipBody } from '../../utils/chartTooltips'
import { formatAnalysisLevelTrendTooltip } from '../../utils/analysisLevelTooltip'
import * as analyzeExcel from '../../utils/analyzeExcel'

// 차트 컨트롤 셀렉트 (디자인시스템 SelectBox — sm 40px / weight 400)
function ChartSelect(props: {
  options: SelectOption[]
  value: string
  onChange: (value: string) => void
}) {
  return <SelectBox {...props} size="sm" weight={400} />
}

function InfoTooltip({ text }: { text: string }) {
  return (
    <div className="btn-icon btn-icon-info">
      <div className="chart-tooltip"><TooltipBody text={text} /></div>
    </div>
  )
}

// 소수점 유지 — Math.round 쓰면 작은 처방량(예: 0.72)이 0/1로 양극화되어 면적이 부풀려 보임
const toYUnit = (raw: number, measure: 'sales' | 'volume'): number =>
  measure === 'sales' ? raw / 1e8 : raw / 1e4
const yTitleFor = (measure: 'sales' | 'volume'): string =>
  measure === 'sales' ? '매출 (억원)' : '처방량 (만)'

interface Props {
  title: string
  analysisLv: AnalysisLevels | undefined   // 해당 measure로 resolved된 데이터
  measure: 'sales' | 'volume'
  onMeasureChange: (m: 'sales' | 'volume') => void
  isLoading: boolean
  productName: string                       // target 색상용
  sourceToggle: 'UBIST' | 'IQVIA'
  unitMeasure: 'unit' | 'dosage_unit' | 'counting_unit'
  onUnitChange: (u: 'unit' | 'dosage_unit' | 'counting_unit') => void
  unitOptions: SelectOption[]
  periodOptions: SelectOption[]
  initialPeriod: string
  ttTrend: string                           // 라인 차트 info 툴팁
  ttMs: string                              // 폴라 차트 info 툴팁
  sectionTitle?: string                     // 세션 제목
  showSectionTitle?: boolean                // 위젯 박스 밖 큰 섹션 제목 노출 여부 (기본 true)
}

type DisplayAnalysisItem = AnalysisItem & {
  readonly seriesPctAvailable: readonly boolean[]
}

export default function AnalysisLevelChart({
  title,
  analysisLv,
  measure,
  onMeasureChange,
  isLoading,
  productName,
  sourceToggle,
  unitMeasure,
  onUnitChange,
  unitOptions,
  periodOptions,
  initialPeriod,
  ttTrend,
  ttMs,
  sectionTitle = "",
  showSectionTitle = true,
}: Props) {
  // ---- 내부 상태 (각 인스턴스 독립) ----
  const [level, setLevel] = useState('Class')         
  const [channel, setChannel] = useState('전체')      
  const [levelInput, setLevelInput] = useState('Class')  
  const [channelInput, setChannelInput] = useState('전체') 
  const [periodInput, setPeriodInput] = useState(initialPeriod)
  const [fromInput, setFromInput] = useState('')
  const [toInput, setToInput] = useState('')
  const [period, setPeriod] = useState(initialPeriod)
  const [from, setFrom] = useState('')
  const [to, setTo] = useState('')
  const [lastRangeKey, setLastRangeKey] = useState('')

  const applyPeriod = () => { setPeriod(periodInput); setFrom(fromInput); setTo(toInput); setLevel(levelInput); setChannel(channelInput) }

  // ---- 데이터 timeline + 마지막 5년 slice (raw 단위 기준 60월/20분기 고정) ----
  const unitPeriods = (analysisLv?.period_unit === '분기'
    ? analysisLv?.periods_quarterly
    : analysisLv?.periods_monthly) ?? []
  const baseN = analysisLv?.period_unit === '분기' ? 20 : 60
  // sliceLastRange 동등 — from은 시작 시점(ymStartOf), to는 끝 시점(ymEndOf)으로 정규화 (원본 차트 5와 동일)
  const range: { from: string; to: string } | null = unitPeriods.length === 0
    ? null
    : (() => {
        const sliced = unitPeriods.slice(-baseN)
        return { from: ymStartOf(sliced[0]!), to: ymEndOf(sliced[sliced.length - 1]!) }
      })()
  const fromEff = range?.from ?? ''
  const toEff = range?.to ?? ''

  // ---- Adjust state during render (useEffect 금지) ----
  // 1) 날짜 sync — 데이터 범위(range) 변경 시마다 재동기화 (최초 로드 + 출처/뷰 전환). 사용자 편집은 range 불변이라 안 건드림.
  const rangeKey = range ? `${range.from}|${range.to}` : ''
  if (rangeKey && rangeKey !== lastRangeKey) {
    setLastRangeKey(rangeKey)
    setFromInput(range!.from); setToInput(range!.to)
    setFrom(range!.from); setTo(range!.to)
  }
  // 2) IQVIA 분기 강제 (monthly → quarterly)
  if (sourceToggle === 'IQVIA') {
    if (periodInput === 'monthly') setPeriodInput('quarterly')
    if (period === 'monthly') setPeriod('quarterly')
  }

  // ---- 데이터 있는 Level만 노출 ----
  const hasLevelData = (lv: string): boolean => {
    const byChannel = analysisLv?.data?.[lv]?.by_channel
    if (!byChannel) return false
    return Object.values(byChannel).some(items => Array.isArray(items) && items.length > 0)
  }
  // 3) level/channel 디폴트 보정 (데이터 있는 레벨/채널로 자동 정렬)
  const lvKeysAvail = (analysisLv?.levels ?? []).filter(hasLevelData)
  if (lvKeysAvail.length > 0 && !lvKeysAvail.includes(level)) {
    setLevel(lvKeysAvail[0]!)
  }
  if (lvKeysAvail.length > 0 && !lvKeysAvail.includes(levelInput)) {
    setLevelInput(lvKeysAvail[0]!)
  }
  const chKeysAvail = analysisLv?.channels ?? []
  if (chKeysAvail.length > 0 && !chKeysAvail.includes(channel)) {
    setChannel(chKeysAvail[0]!)
  }
  if (chKeysAvail.length > 0 && !chKeysAvail.includes(channelInput)) {
    setChannelInput(chKeysAvail[0]!)
  }

  // ---- 차트 데이터 계산 ----
  // raw monthly 단계에서 먼저 filter (집계 후 filter하면 "2026-Q1" > "2026-04" 문자열 비교 버그)
  const periodsBase = (analysisLv?.period_unit === '분기' ? analysisLv?.periods_quarterly : analysisLv?.periods_monthly) ?? []
  const itemsBase = analysisLv?.data?.[level]?.by_channel?.[channel] ?? []
  const filteredIndices: number[] = []
  periodsBase.forEach((p, i) => {
    const pYM = ymStartOf(p)  // raw "YYYY-Qn" 호환
    if ((!from || pYM >= from) && (!to || pYM <= to)) filteredIndices.push(i)
  })
  const periodsRawFiltered = filteredIndices.map(i => periodsBase[i]!)
  const itemsRawFiltered: DisplayAnalysisItem[] = itemsBase.map(it => ({
    ...it,
    value_series: filteredIndices.map(i => (it.value_series ?? [])[i] ?? 0),
    series_pct: filteredIndices.map(i => (it.series_pct ?? [])[i] ?? 0),
    seriesPctAvailable: filteredIndices.map(i => {
      const sharePct = it.series_pct?.[i]
      return typeof sharePct === 'number' && Number.isFinite(sharePct)
    }),
  }))
  const aggregated = aggregatePeriodsAndItems(periodsRawFiltered, itemsRawFiltered, period as PeriodUnit)
  const periodsAgg = aggregated.periods
  const itemsRaw = aggregated.items
  // The API owns the cohort and emits one explicit remainder row. Never rename a company into "기타".
  const items: DisplayAnalysisItem[] = [...itemsRaw].sort((a, b) => {
    if (a.is_others === true) return 1
    if (b.is_others === true) return -1
    return (a.rank ?? 999) - (b.rank ?? 999)
  })
  const filteredPeriods = periodsAgg
  const isRevenue = measure === 'sales'

  // '기타'는 항상 회색, target 브랜드(productName)는 TARGET_COLOR 강제
  const colorFor = (item: AnalysisItem, i: number): string => {
    if (item.name === '기타') return OTHERS_COLOR
    if (item.name === productName) return TARGET_COLOR
    return CHART_COLORS[i % CHART_COLORS.length]!
  }
  const lineData = {
    labels: filteredPeriods,
    datasets: items.map((item, i) => {
      const color = colorFor(item, i)
      const data = item.value_series.slice(0, filteredPeriods.length).map(v => toYUnit(v, measure))
      if (item.name === '전체') {
        return {
          label: item.name,
          data,
          backgroundColor: '#E5F6FC',
          borderColor: 'transparent',
          borderWidth: 0,
          fill: true,
          tension: 0.3,
          pointRadius: 0,
          pointHoverBackgroundColor: '#7EC9E8',
          pointHoverBorderColor: '#7EC9E8',
          order: 1,
        }
      }
      return {
        label: item.name,
        data,
        borderColor: color,
        backgroundColor: color,
        fill: false,
        tension: 0.3,
        pointRadius: 0,
        order: 0,
      }
    }),
  }
  // ★ 폴라(M/S) 값은 by_channel이 아니라 ms_by_channel에서 가져옴 (백엔드가 폴라 전용으로 분리 제공)
  //   - ms_by_channel: '전체' 없음 + recent_share_pct 정상값(33.26% 등). by_channel은 recent_share_pct가 null이라 우회했었음
  //   - 카테고리·색은 라인과 동일하게 유지(일관성), 값만 ms_by_channel에서 name으로 lookup
  const msItems = analysisLv?.data?.[level]?.ms_by_channel?.[channel] ?? []
  const hasMs = msItems.length > 0
  const msShareByName = new Map(msItems.map(it => [it.name, it.recent_share_pct ?? 0]))
  // '전체'(rank=0) 항목은 폴라 차트·범례 둘 다에서 제외. 색은 원본 인덱스 기준 계산 후 필터 (밀림 방지)
  const polarItems = items
    .map((item, i) => ({ item, color: colorFor(item, i) }))
    .filter(({ item }) => item.name !== '전체')
  const namedInPolar = new Set(polarItems.filter(p => p.item.name !== '기타').map(p => p.item.name))
  // name별 M/S 값 — ms_by_channel 있으면 거기서, 없으면 라인 항목 fallback(recent_share_pct ?? series_pct[-1])
  const polarShareFor = (item: AnalysisItem): number => {
    if (!hasMs) return item.recent_share_pct ?? item.series_pct[item.series_pct.length - 1] ?? 0
    // '기타' = ms_by_channel 중 폴라에 개별 노출되지 않은 나머지 카테고리 합
    if (item.name === '기타') {
      return msItems.filter(it => !namedInPolar.has(it.name)).reduce((s, it) => s + (it.recent_share_pct ?? 0), 0)
    }
    return msShareByName.get(item.name) ?? 0
  }
  const polarData = {
    labels: polarItems.map(p => p.item.name),
    datasets: [{
      data: polarItems.map(p => polarShareFor(p.item)),
      backgroundColor: polarItems.map(p => p.color + '66'),
      hoverBackgroundColor: polarItems.map(p => p.color),
    }],
  }

  // 엑셀: 시트 빌드는 analyzeExcel.ts로 통일. 여기선 컴포넌트 내부 state 기반 데이터만 넘김
  const handleExcel = () => analyzeExcel.exportLevelChart({
    productName, sourceToggle, measure, title, level, channel,
    items, periods: filteredPeriods,
    ms: polarItems.map(p => ({ name: p.item.name, share: polarShareFor(p.item) })),
  })

  // 동적 셀렉트 옵션
  const levelOptions = (analysisLv?.levels ?? ['Class', 'Molecule', '제형/투여경로', '용량', '비/급여', 'Ox/Gx'])
    .filter(hasLevelData)
    .filter(l => l !== 'Brand')
  const channelOptions = analysisLv?.channels ?? ['전체']

  return (
    <>
      {showSectionTitle && <div className="chat-section-title-n">{sectionTitle}</div>}
      <div className="chart-widget chart-widget-full">
        <div className="chart-widget-header">
          <div className="chart-widget-title-group">
            <h2 className="chart-widget-title">{title}</h2>
          </div>
          <div className="chart-widget-controls">
            <div className="control-box control-box-select">
              <ChartSelect
                options={levelOptions.map(l => ({ value: l, label: l }))}
                value={levelInput}
                onChange={v => { setLevelInput(v); setChannelInput('전체') }}
              />
            </div>
            <div className="control-box control-box-select">
              <ChartSelect
                options={channelOptions.map(c => ({ value: c, label: c }))}
                value={channelInput}
                onChange={setChannelInput}
              />
            </div>
            {sourceToggle === 'IQVIA' && measure === 'volume' && (
              <div className="control-box control-box-select">
                <ChartSelect options={unitOptions} value={unitMeasure} onChange={v => onUnitChange(v as 'unit' | 'dosage_unit' | 'counting_unit')} />
              </div>
            )}
            <div className="in-sepa-line" />
            <div className="control-box control-box-select">
              <ChartSelect
                options={periodOptions}
                value={periodInput}
                onChange={v => onPeriodInputChange(v, setPeriodInput, setFromInput, setToInput, fromEff, toEff)}
              />
            </div>
            <div className="control-box control-box-date">
              <DateRangePicker
                from={fromInput} to={toInput}
                mode={periodInput as 'monthly' | 'quarterly' | 'yearly'}
                minYM={fromEff} maxYM={toEff}
                onFromChange={setFromInput} onToChange={setToInput}
              />
            </div>
            <button type="button" className="btn-date-search" onClick={applyPeriod}>조회</button>
            <button type="button" className="btn-date-reset"
              onClick={() => {
                setPeriodInput(initialPeriod)
                setFromInput(range?.from ?? ''); setToInput(range?.to ?? '')
                setPeriod(initialPeriod)
                setFrom(range?.from ?? ''); setTo(range?.to ?? '')
                setLevelInput(''); setLevel('')
                setChannelInput('전체'); setChannel('전체')
              }}
            >새로고침</button>
            <div className="in-sepa-line" />
            <MeasureToggle measure={measure} onChange={onMeasureChange} />
            <div className="in-sepa-line" />
            <button type="button" className="btn-excel-down" onClick={handleExcel} disabled={isLoading}>엑셀다운로드</button>
          </div>
        </div>
        <div className="chart-widget-body">
          <div className="chart-layout-split">
            <div className="chart-layout-split-main">
              <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', gap: 6, fontSize: 16, fontWeight: 600, marginBottom: 8 }}>
                <span>{isRevenue ? '매출 추이' : '처방량 추이'}</span>
                <InfoTooltip text={ttTrend} />
              </div>
              <div style={{ height: 429 }}>
              {isLoading ? <ChartSkeleton legendItems={6} /> : <Line
                data={lineData}
                options={{
                  ...commonOpts,
                  interaction: { mode: 'index', intersect: false },
                  plugins: {
                    legend: {
                      ...legendBottom,
                      ...legendHoverPointer,
                      labels: {
                        ...legendBottom.labels,
                        // '전체'를 범례 맨 왼쪽으로 (datasetIndex 보존 → 클릭 토글 정상)
                        generateLabels: chart => {
                          const labels = ChartJS.defaults.plugins.legend.labels.generateLabels(chart)
                          const idx = labels.findIndex(it => it.text === '전체')
                          if (idx > 0) labels.unshift(labels.splice(idx, 1)[0]!)
                          return labels
                        },
                      },
                    },
                    tooltip: {
                      mode: 'index',
                      intersect: false,
                      itemSort: (a, b) => {
                        if (a.dataset.label === '전체') return -1
                        if (b.dataset.label === '전체') return 1
                        return a.datasetIndex - b.datasetIndex
                      },
                      callbacks: {
                        title: tItems => fmtPeriodKor(tItems[0]?.label ?? ''),
                        label: ctx => {
                          const it = items[ctx.datasetIndex]
                          if (!it) return ''
                          const raw = it.value_series[ctx.dataIndex] ?? 0
                          const valStr = isRevenue ? fmtBaekman(raw) : Math.round(raw).toLocaleString()
                          const valueLabel = `${it.name} (${isRevenue ? '매출' : '처방량'} : ${valStr})`
                          const sharePct = it.seriesPctAvailable[ctx.dataIndex]
                            ? it.series_pct[ctx.dataIndex]
                            : undefined
                          return formatAnalysisLevelTrendTooltip({
                            valueLabel,
                            source: sourceToggle,
                            sourcePeriodUnit: analysisLv?.period_unit ?? '',
                            selectedPeriod: period,
                            isOverall: it.name === '전체',
                            sharePct,
                          })
                        },
                      },
                    },
                  },
                  scales: {
                    x: { grid: { display: false }, ticks: { maxTicksLimit: 10 } },
                    y: {
                      title: { display: true, text: yTitleFor(measure) },
                      ticks: { callback: v => Number(v).toLocaleString() },
                    },
                  },
                }}
              />}
              </div>
            </div>
            <div className="chart-layout-split-sub">
              <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center', gap: 6, fontSize: 16, fontWeight: 600, marginBottom: 8 }}>
                <span>M/S (최근 시점{periodsBase.length > 0 ? ` : ${fmtPeriodKor(periodsBase[periodsBase.length - 1]!)}` : ''} 기준)</span>
                <InfoTooltip text={ttMs} />
              </div>
              <div style={{ height: 429 }}>
              {isLoading ? <ChartSkeleton leftAxis={false} xAxis={false} legendItems={6} /> : <PolarArea
                data={polarData}
                options={{
                  ...commonOpts,
                  scales: {
                    r: {
                      ticks: {
                        z: 1,
                        backdropColor: 'rgba(0,0,0,0)',
                        callback: (v: number | string) => {
                          const n = Number(v)
                          return `${Number.isInteger(n) ? n : n.toFixed(1)}%`
                        },
                      },
                    },
                  },
                  plugins: {
                    legend: { ...legendBottom, labels: { ...legendBottom.labels, generateLabels: opaqueLabelsForArc }, onClick: () => {} },
                    tooltip: {
                      callbacks: {
                        title: () => '',
                        label: ctx => {
                          const it = polarItems[ctx.dataIndex]?.item
                          if (!it) return ''
                          return [it.name, `- M/S : ${polarShareFor(it).toFixed(1)}%`]
                        },
                      },
                    },
                  },
                }}
              />}
              </div>
            </div>
          </div>
        </div>
      </div>
    </>
  )
}
