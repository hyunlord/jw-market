// 매출/처방량 토글 — 차트 1/5/7/8/MS 공용
// 공용 SlideToggle(세그먼트 + pill 슬라이드 애니메이션)에 매출/처방량 옵션을 고정
import SlideToggle from './SlideToggle'
type Measure = 'sales' | 'volume'

const MEASURE_OPTIONS = [
  { value: 'sales', label: '매출' },
  { value: 'volume', label: '처방량' },
] as const

export default function MeasureToggle({
  measure,
  onChange,
}: {
  measure: Measure
  onChange: (m: Measure) => void
}) {
  return <SlideToggle options={MEASURE_OPTIONS} value={measure} onChange={onChange} />
}
