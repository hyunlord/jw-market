interface ChartSkeletonProps {
  /** 차트 캔버스 높이 (실제 차트 `<div style={{height}}>`와 동일하게) */
  height?: number
  /** 좌측 Y축 눈금 placeholder */
  leftAxis?: boolean
  /** 우측 Y축 눈금 placeholder (이중 Y축 차트) */
  rightAxis?: boolean
  /** X축 눈금 placeholder */
  xAxis?: boolean
  /** 범례 placeholder 개수 (0이면 미표시) */
  legendItems?: number
}

export default function ChartSkeleton({
  height = 429,
  leftAxis = true,
  rightAxis = false,
  xAxis = true,
  legendItems = 2,
}: ChartSkeletonProps) {
  return (
    <div className="chart-skel" style={{ height }}>
      <div className="chart-skel__plot-wrap">
        {leftAxis && (
          <div className="chart-skel__yaxis">
            {Array.from({ length: 6 }).map((_, i) => <span key={i} className="chart-skel-shimmer" />)}
          </div>
        )}
        <div className="chart-skel__plot chart-skel-shimmer" />
        {rightAxis && (
          <div className="chart-skel__yaxis chart-skel__yaxis--right">
            {Array.from({ length: 6 }).map((_, i) => <span key={i} className="chart-skel-shimmer" />)}
          </div>
        )}
      </div>
      {xAxis && (
        <div className="chart-skel__xaxis">
          {Array.from({ length: 8 }).map((_, i) => <span key={i} className="chart-skel-shimmer" />)}
        </div>
      )}
      {legendItems > 0 && (
        <div className="chart-skel__legend">
          {Array.from({ length: legendItems }).map((_, i) => (
            <span key={i} className="chart-skel__legend-item">
              <i className="chart-skel-shimmer" />
              <em className="chart-skel-shimmer" />
            </span>
          ))}
        </div>
      )}
    </div>
  )
}
