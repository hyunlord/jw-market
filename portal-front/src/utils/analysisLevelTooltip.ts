type AnalysisSource = 'UBIST' | 'IQVIA'

type AnalysisLevelTrendTooltipInput = {
  readonly valueLabel: string
  readonly source: AnalysisSource
  readonly sourcePeriodUnit: string
  readonly selectedPeriod: string
  readonly isOverall: boolean
  readonly sharePct: number | undefined
}

export function formatAnalysisLevelTrendTooltip({
  valueLabel,
  source,
  sourcePeriodUnit,
  selectedPeriod,
  isOverall,
  sharePct,
}: AnalysisLevelTrendTooltipInput): string | string[] {
  const isNativePeriod = source === 'UBIST'
    ? sourcePeriodUnit === '월' && selectedPeriod === 'monthly'
    : sourcePeriodUnit === '분기' && selectedPeriod === 'quarterly'

  if (!isNativePeriod || isOverall || sharePct === undefined || !Number.isFinite(sharePct)) {
    return valueLabel
  }

  return [valueLabel, `- M/S : ${sharePct.toFixed(1)}%`]
}
