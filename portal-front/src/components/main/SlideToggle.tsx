// 공용 슬라이드 토글 — 세그먼트 스타일 + 선택 시 pill 배경 슬라이드 애니메이션
// N지선다 지원: 활성 인덱스를 CSS 변수(--st-index)로 주입해 thumb를 이동.
// 외형은 .slide-toggle CSS(공용)와 className 변형(예: slide-toggle--source 헤더 필 룩)으로 제어.
import type { CSSProperties } from 'react'

export default function SlideToggle<T extends string>({
  options,
  value,
  onChange,
  className,
}: {
  options: readonly { value: T; label: string; disabled?: boolean; title?: string }[]
  value: T
  onChange: (v: T) => void
  className?: string
}) {
  const activeIndex = Math.max(0, options.findIndex(o => o.value === value))
  const style = { '--st-index': activeIndex } as CSSProperties
  return (
    <div className={`slide-toggle${className ? ` ${className}` : ''}`} style={style}>
      <span className="slide-toggle-thumb" aria-hidden="true" />
      {options.map(opt => (
        <button
          key={opt.value}
          type="button"
          className={`slide-toggle-btn${value === opt.value ? ' is-active' : ''}`}
          disabled={opt.disabled}
          title={opt.title}
          onClick={() => { if (!opt.disabled) onChange(opt.value) }}
        >{opt.label}</button>
      ))}
    </div>
  )
}
