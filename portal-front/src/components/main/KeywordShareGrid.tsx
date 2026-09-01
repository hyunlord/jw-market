// 브랜드별 키워드 점유 구조 표
import { Fragment, useMemo, useState } from 'react'
import type { TopicsData, TopicsBrand } from '../../types/market'

// 퍼블 7색 (common.css의 card-*/box-* 클래스와 1:1)
const COLOR_KEYS = ['diabetes', 'interaction', 'ldl', 'cardio', 'switch', 'generic', 'dyslipidemia'] as const
const MAX_COLORS = COLOR_KEYS.length

const rowCount = (eventCount: number, sharePct: number): number => Math.round((eventCount * sharePct) / 100)

export default function KeywordShareGrid({ data }: { data: TopicsData | null; isLoading?: boolean }) {
  const brands = data?.brands ?? []

  // 유니크 키워드 라벨 수집(등장 순) → 최대 7개까지만 색 부여
  const keywordOrder = useMemo(() => {
    const seen = new Set<string>()
    const order: string[] = []
    for (const b of data?.brands ?? []) {
      for (const t of b.topic_shares) {
        if (!seen.has(t.label)) { seen.add(t.label); order.push(t.label) }
      }
    }
    return order.slice(0, MAX_COLORS)
  }, [data])

  // label → 색 인덱스(-1이면 색 없음 = 기본 회색 카드)
  const colorKeyOf = (label: string): string | null => {
    const i = keywordOrder.indexOf(label)
    return i >= 0 ? COLOR_KEYS[i] : null
  }

  const [checked, setChecked] = useState<Set<string>>(new Set())
  const toggle = (label: string) => setChecked(prev => {
    const next = new Set(prev)
    if (next.has(label)) next.delete(label); else next.add(label)
    return next
  })

  if (brands.length === 0) {
    return (
      <div>
        <div className="chart-legend" />
        <div className="ranking-grid">
          <div />
          <div className="grid-header">1위</div>
          <div className="grid-header">2위</div>
          <div className="grid-header">3위</div>
          <div className="grid-header">4위</div>
          <div className="grid-header">5위</div>
          <div className="grid-header">고유 키워드1</div>
          <div className="grid-header">고유 키워드2</div>
          {Array.from({ length: 6 }).map((_, r) => (
            <Fragment key={r}>
              <div className="brand-cell">-</div>
              {Array.from({ length: 7 }).map((_, c) => <div className="card empty" key={c}>-</div>)}
            </Fragment>
          ))}
        </div>
      </div>
    )
  }

  return (
    <div>
      {/* 범례 — 유니크 키워드 7색. 클릭 시 해당 키워드 카드 강조 */}
      <div className="chart-legend">
        {keywordOrder.map((label, i) => (
          <label className={`legend-item${checked.has(label) ? ' active' : ''}`} key={label}>
            <input
              type="checkbox"
              className="legend-checkbox"
              checked={checked.has(label)}
              onChange={() => toggle(label)}
            />
            <span className={`custom-box box-${COLOR_KEYS[i]}`} />
            {label}
          </label>
        ))}
      </div>

      {/* 데이터 그리드 (8열: 브랜드 + 1~5위 + 고유 키워드1·2) */}
      <div className="ranking-grid">
        {/* 헤더 행 */}
        <div />
        <div className="grid-header">1위</div>
        <div className="grid-header">2위</div>
        <div className="grid-header">3위</div>
        <div className="grid-header">4위</div>
        <div className="grid-header">5위</div>
        <div className="grid-header">고유 키워드1</div>
        <div className="grid-header">고유 키워드2</div>

        {/* 브랜드 행 */}
        {brands.map(b => <BrandRow key={b.brand_name} brand={b} colorKeyOf={colorKeyOf} checked={checked} />)}
      </div>
    </div>
  )
}

// 한 브랜드 행 = 1~5위 카드 5개 + 고유 키워드 카드 1개
function BrandRow({
  brand, colorKeyOf, checked,
}: {
  brand: TopicsBrand
  colorKeyOf: (label: string) => string | null
  checked: Set<string>
}) {
  const ranks = Array.from({ length: 5 }, (_, j) => brand.topic_shares[j])  // 없으면 undefined
  const specifics = brand.brand_specific_topics ?? []
  const dataStatus = brand.data_status
  const statusLabel = !dataStatus || dataStatus.code === 'available' ? null : dataStatus.label
  const statusTitle = dataStatus?.code === 'identity_mismatch'
    ? `${dataStatus.label ?? '재분류 필요'} (소스 ${dataStatus.source_row_count ?? 0}건 / 분류 ${dataStatus.classified_row_count ?? 0}건 / 유효 ${dataStatus.guard_valid_row_count ?? 0}건)`
    : (dataStatus?.label ?? undefined)

  return (
    <>
      <div className="brand-cell">
        {brand.brand_name}
        {brand.is_jw
          ? <span className="brand-sub">JW 자사</span>
          : brand.company_name && <span className="brand-company">({brand.company_name})</span>}
        {dataStatus && statusLabel && (
          <span className={`brand-data-status brand-data-status-${dataStatus.code}`} title={statusTitle}>
            {statusLabel}
          </span>
        )}
      </div>

      {/* 1~5위 */}
      {ranks.map((t, j) => {
        if (!t) return <div className="card empty" key={j}>-</div>
        const colorKey = colorKeyOf(t.label)
        const cls = ['card', colorKey ? `card-${colorKey}` : '', checked.has(t.label) ? 'active' : '']
          .filter(Boolean).join(' ')
        return (
          <div className={cls} key={j}>
            {t.label}
            <div className="count"><span>{t.row_count.toLocaleString()}</span>건 / {t.share_pct.toFixed(1)}%</div>
          </div>
        )
      })}

      {[0, 1].map(k => {
        const s = specifics[k]
        if (!s) return <div className="card empty" key={`spec-${k}`}>-</div>
        const cnt = (s.row_count ?? rowCount(brand.event_count, s.share_pct)).toLocaleString()
        return (
          <div className="card outline" key={`spec-${k}`}>
            {s.label}
            <div className="count"><span>{cnt}</span>건 / {s.share_pct.toFixed(1)}%</div>
          </div>
        )
      })}
    </>
  )
}
