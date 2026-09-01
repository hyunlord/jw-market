// 로딩 중 자리표시용 스켈레톤 바 — chart-skel-shimmer 클래스로 shimmer 애니메이션.
// 심층분석/원인분석 상단 "기준" 날짜 등 단일 텍스트 자리에 사용.
export default function SkelBar({ w, h = 12, r = 6, mb = 0, inline = false }: {
  w: number | string
  h?: number
  r?: number
  mb?: number
  inline?: boolean
}) {
  return (
    <span
      className="chart-skel-shimmer"
      style={{ display: inline ? 'inline-block' : 'block', width: w, height: h, borderRadius: r, marginBottom: mb, verticalAlign: 'middle' }}
    />
  )
}
