import { useState, useRef, useEffect } from 'react'
import type { CSSProperties } from 'react'

// from/to는 항상 "YYYY-MM" 형식으로 저장 — 모드별 변환은 내부에서 처리
const Q_START: Record<string, string> = { Q1: '01', Q2: '04', Q3: '07', Q4: '10' }
const Q_END: Record<string, string>   = { Q1: '03', Q2: '06', Q3: '09', Q4: '12' }
const MM_TO_Q: Record<string, string> = {
  '01': 'Q1', '02': 'Q1', '03': 'Q1',
  '04': 'Q2', '05': 'Q2', '06': 'Q2',
  '07': 'Q3', '08': 'Q3', '09': 'Q3',
  '10': 'Q4', '11': 'Q4', '12': 'Q4',
}

const NAV_BTN: CSSProperties = {
  background: 'none', border: 'none', cursor: 'pointer',
  fontSize: 22, color: '#333', lineHeight: 1, padding: '0 4px',
}

interface Props {
  from: string   // "YYYY-MM"
  to: string
  mode: 'monthly' | 'quarterly' | 'yearly'
  // 데이터의 시작 월("YYYY-MM"). monthly/quarterly 모드에서 이전 월/분기 disabled.
  minYM?: string
  // 데이터의 마지막 월("YYYY-MM"). yearly 모드에서 종료년이 이 값의 연도와 같으면 12월 대신 maxYM의 월로 confirm (데이터 없는 미래 월 방지).
  // monthly/quarterly 모드에서는 이후 월/분기 disabled.
  maxYM?: string
  onFromChange: (v: string) => void
  onToChange: (v: string) => void
}

export default function DateRangePicker({ from, to, mode, minYM, maxYM, onFromChange, onToChange }: Props) {
  const now = new Date()
  const [open, setOpen] = useState(false)
  // viewBase: monthly→해당 연도, quarterly→시작 연도(5년 단위), yearly→시작 연도(10년 단위)
  const [viewBase, setViewBase] = useState(() => parseInt(from.slice(0, 4)))
  const [pendingFrom, setPendingFrom] = useState<string | null>(null)
  const [hovered, setHovered] = useState<string | null>(null)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const handler = (e: MouseEvent) => {
      if (ref.current?.contains(e.target as Node)) return
      setOpen(false); setPendingFrom(null); setHovered(null)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [open])

  const handleOpen = () => {
    if (!open) {
      setPendingFrom(null); setHovered(null)
      const yr = parseInt(from.slice(0, 4))
      if (mode === 'monthly') setViewBase(yr)
      else if (mode === 'quarterly') setViewBase(Math.floor(yr / 5) * 5)
      else setViewBase(Math.floor(yr / 10) * 10)
    }
    setOpen(p => !p)
  }

  const getInputDisplay = (): string => {
    const fy = from.slice(0, 4), fm = from.slice(5, 7)
    const ty = to.slice(0, 4),   tm = to.slice(5, 7)
    if (mode === 'monthly')   return `${fy}.${fm} - ${ty}.${tm}`
    if (mode === 'quarterly') return `${fy}.${MM_TO_Q[fm] ?? 'Q1'} - ${ty}.${MM_TO_Q[tm] ?? 'Q4'}`
    // yearly 모드: 연도 + 월 표시 (기획자: 차트가 월 단위 데이터라 input에도 월 노출)
    // from이 "YYYY-Q*" 형식(IQVIA raw)일 땐 분기 시작/끝 월로 변환 — "2021.Q3" 형태로 새는 버그 방지
    const fmNorm = Q_START[fm] ?? fm
    const tmNorm = Q_END[tm] ?? tm
    return `${fy}.${fmNorm} - ${ty}.${tmNorm}`
  }

  const confirmRange = (f: string, t: string) => {
    onFromChange(f); onToChange(t)
    setPendingFrom(null); setHovered(null); setOpen(false)
  }

  // ─── 월 피커 ───
  const todayYM = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`
  const toYM = (y: number, m: number) => `${y}-${String(m).padStart(2, '0')}`

  const mDispFrom = pendingFrom ?? from
  const mDispTo   = pendingFrom !== null ? (hovered ?? pendingFrom) : to
  const [mRangeFrom, mRangeTo] = mDispFrom <= mDispTo ? [mDispFrom, mDispTo] : [mDispTo, mDispFrom]

  const handleMonthClick = (ym: string) => {
    if (ym > todayYM) return
    if (minYM != null && ym < minYM) return
    if (maxYM != null && ym > maxYM) return
    if (pendingFrom === null) { setPendingFrom(ym); setHovered(null); return }
    const [f, t] = ym < pendingFrom ? [ym, pendingFrom] : [pendingFrom, ym]
    confirmRange(f, t)
  }

  const renderMonthPicker = () => (
    <>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', marginBottom: 16, gap: 24 }}>
        <button type="button" style={NAV_BTN} onClick={() => setViewBase(y => y - 1)}>‹</button>
        <span style={{ fontWeight: 700, fontSize: 18, color: '#1a1a1a', minWidth: 52, textAlign: 'center' }}>{viewBase}</span>
        <button type="button" style={NAV_BTN} onClick={() => setViewBase(y => y + 1)}>›</button>
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(6, 1fr)', rowGap: 6 }}>
        {Array.from({ length: 12 }, (_, i) => {
          const month = i + 1
          const ym = toYM(viewBase, month)
          const disabled = ym > todayYM
            || (minYM != null && ym < minYM)
            || (maxYM != null && ym > maxYM)
          const isCircle = ym === mRangeFrom || ym === mRangeTo
          const isStart  = ym === mRangeFrom && mRangeFrom !== mRangeTo
          const isEnd    = ym === mRangeTo   && mRangeFrom !== mRangeTo
          const inRange  = ym > mRangeFrom && ym < mRangeTo
          let wrapBg = 'transparent'
          if (!disabled && mRangeFrom !== mRangeTo) {
            if (isStart)      wrapBg = 'linear-gradient(to right, transparent 50%, rgba(0,169,229,0.12) 50%)'
            else if (isEnd)   wrapBg = 'linear-gradient(to left,  transparent 50%, rgba(0,169,229,0.12) 50%)'
            else if (inRange) wrapBg = 'rgba(0,169,229,0.12)'
          }
          return (
            <div key={month} style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: 44, background: wrapBg }}>
              <button type="button" disabled={disabled}
                onClick={() => handleMonthClick(ym)}
                onMouseEnter={() => { if (pendingFrom !== null) setHovered(ym) }}
                onMouseLeave={() => setHovered(null)}
                style={{
                  width: 44, height: 44, border: 'none', borderRadius: '50%',
                  cursor: disabled ? 'default' : 'pointer',
                  fontWeight: isCircle ? 700 : 400, fontSize: 13,
                  background: isCircle && !disabled ? '#00A9E5' : 'transparent',
                  color: disabled ? '#D1D2D7' : isCircle ? '#ffffff' : inRange ? '#00A9E5' : '#060B11',
                }}
              >{month}월</button>
            </div>
          )
        })}
      </div>
    </>
  )

  // ─── 분기 피커 ───
  const currentQ  = Math.ceil((now.getMonth() + 1) / 3)
  const todayYQ   = `${now.getFullYear()}-Q${currentQ}`
  const fromYQ    = `${from.slice(0, 4)}-${MM_TO_Q[from.slice(5, 7)] ?? 'Q1'}`
  const toYQ      = `${to.slice(0, 4)}-${MM_TO_Q[to.slice(5, 7)] ?? 'Q4'}`
  const qDispFrom = pendingFrom ?? fromYQ
  const qDispTo   = pendingFrom !== null ? (hovered ?? pendingFrom) : toYQ
  const [qRangeFrom, qRangeTo] = qDispFrom <= qDispTo ? [qDispFrom, qDispTo] : [qDispTo, qDispFrom]
  // 데이터 범위 → 분기 변환 (월 → 해당 분기)
  const minYQ = minYM ? `${minYM.slice(0, 4)}-${MM_TO_Q[minYM.slice(5, 7)] ?? 'Q1'}` : null
  const maxYQ = maxYM ? `${maxYM.slice(0, 4)}-${MM_TO_Q[maxYM.slice(5, 7)] ?? 'Q4'}` : null

  const handleQuarterClick = (yq: string) => {
    if (yq > todayYQ) return
    if (minYQ != null && yq < minYQ) return
    if (maxYQ != null && yq > maxYQ) return
    if (pendingFrom === null) { setPendingFrom(yq); setHovered(null); return }
    const [f, t] = yq < pendingFrom ? [yq, pendingFrom] : [pendingFrom, yq]
    const [fy, fq] = f.split('-'); const [ty, tq] = t.split('-')
    confirmRange(`${fy}-${Q_START[fq ?? 'Q1'] ?? '01'}`, `${ty}-${Q_END[tq ?? 'Q4'] ?? '12'}`)
  }

  const renderQuarterPicker = () => {
    const years = Array.from({ length: 5 }, (_, i) => viewBase + i)
    return (
      <>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', marginBottom: 16, gap: 16 }}>
          <button type="button" style={NAV_BTN} onClick={() => setViewBase(y => y - 5)}>‹</button>
          <span style={{ fontWeight: 700, fontSize: 16, color: '#1a1a1a', textAlign: 'center' }}>{viewBase} - {viewBase + 4}</span>
          <button type="button" style={NAV_BTN} onClick={() => setViewBase(y => y + 5)}>›</button>
        </div>
        <div style={{ display: 'flex', flexDirection: 'column' }}>
          {years.map((yr, idx) => (
            <div key={yr}>
              <div style={{ display: 'grid', gridTemplateColumns: '56px repeat(4, 1fr)', alignItems: 'center', padding: '0 4px' }}>
                <span style={{ fontSize: 13, color: '#060B11', fontWeight: 500 }}>{yr}</span>
                {([1, 2, 3, 4] as const).map(qNum => {
                  const yq = `${yr}-Q${qNum}`
                  const disabled = yq > todayYQ
                    || (minYQ != null && yq < minYQ)
                    || (maxYQ != null && yq > maxYQ)
                  const isCircle = yq === qRangeFrom || yq === qRangeTo
                  const inRange  = yq > qRangeFrom && yq < qRangeTo
                  return (
                    <button key={qNum} type="button" disabled={disabled}
                      onClick={() => handleQuarterClick(yq)}
                      onMouseEnter={() => { if (pendingFrom !== null) setHovered(yq) }}
                      onMouseLeave={() => setHovered(null)}
                      style={{
                        height: 36, border: 'none', borderRadius: 20,
                        cursor: disabled ? 'default' : 'pointer',
                        fontWeight: isCircle ? 700 : 400, fontSize: 13,
                        background: isCircle && !disabled ? '#00A9E5' : inRange && !disabled ? 'rgba(0,169,229,0.12)' : 'transparent',
                        color: disabled ? '#D1D2D7' : isCircle ? '#ffffff' : inRange ? '#00A9E5' : '#060B11',
                      }}
                    >Q{qNum}</button>
                  )
                })}
              </div>
              {idx < 4 && <div style={{ height: 1, background: '#F0F0F3', margin: '4px 0' }} />}
            </div>
          ))}
        </div>
      </>
    )
  }

  // ─── 연도 피커 ───
  const fromYear  = from.slice(0, 4)
  const toYear    = to.slice(0, 4)
  const yDispFrom = pendingFrom ?? fromYear
  const yDispTo   = pendingFrom !== null ? (hovered ?? pendingFrom) : toYear
  const [yRangeFrom, yRangeTo] = yDispFrom <= yDispTo ? [yDispFrom, yDispTo] : [yDispTo, yDispFrom]

  const minYr = minYM?.slice(0, 4)
  const minMm = minYM?.slice(5, 7)
  const maxYr = maxYM?.slice(0, 4)
  const maxMm = maxYM?.slice(5, 7)

  const handleYearClick = (yr: string) => {
    if (parseInt(yr) > now.getFullYear()) return
    if (minYr != null && yr < minYr) return
    if (maxYr != null && yr > maxYr) return
    if (pendingFrom === null) { setPendingFrom(yr); setHovered(null); return }
    const [f, t] = yr < pendingFrom ? [yr, pendingFrom] : [pendingFrom, yr]
    // 시작년이 데이터 시작 월의 연도와 같으면 그 월로, 아니면 1월
    const fMm = minYr && minMm && f === minYr ? minMm : '01'
    // 종료년이 데이터 마지막 월의 연도와 같으면 그 월로, 아니면 12월
    const tMm = maxYr && maxMm && t === maxYr ? maxMm : '12'
    confirmRange(`${f}-${fMm}`, `${t}-${tMm}`)
  }

  const renderYearPicker = () => {
    const years = Array.from({ length: 10 }, (_, i) => viewBase + i)
    return (
      <>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', marginBottom: 16, gap: 16 }}>
          <button type="button" style={NAV_BTN} onClick={() => setViewBase(y => y - 10)}>‹</button>
          <span style={{ fontWeight: 700, fontSize: 16, color: '#1a1a1a', textAlign: 'center' }}>{viewBase} - {viewBase + 9}</span>
          <button type="button" style={NAV_BTN} onClick={() => setViewBase(y => y + 10)}>›</button>
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(5, 1fr)', rowGap: 6 }}>
          {years.map(yr => {
            const yrStr    = String(yr)
            const disabled = yr > now.getFullYear()
              || (minYr != null && yrStr < minYr)
              || (maxYr != null && yrStr > maxYr)
            const isCircle = yrStr === yRangeFrom || yrStr === yRangeTo
            const isStart  = yrStr === yRangeFrom && yRangeFrom !== yRangeTo
            const isEnd    = yrStr === yRangeTo   && yRangeFrom !== yRangeTo
            const inRange  = yrStr > yRangeFrom && yrStr < yRangeTo
            let wrapBg = 'transparent'
            if (!disabled && yRangeFrom !== yRangeTo) {
              if (isStart)      wrapBg = 'linear-gradient(to right, transparent 50%, rgba(0,169,229,0.12) 50%)'
              else if (isEnd)   wrapBg = 'linear-gradient(to left,  transparent 50%, rgba(0,169,229,0.12) 50%)'
              else if (inRange) wrapBg = 'rgba(0,169,229,0.12)'
            }
            return (
              <div key={yr} style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: 44, background: wrapBg }}>
                <button type="button" disabled={disabled}
                  onClick={() => handleYearClick(yrStr)}
                  onMouseEnter={() => { if (pendingFrom !== null) setHovered(yrStr) }}
                  onMouseLeave={() => setHovered(null)}
                  style={{
                    width: 54, height: 44, border: 'none', borderRadius: 22,
                    cursor: disabled ? 'default' : 'pointer',
                    fontWeight: isCircle ? 700 : 400, fontSize: 13,
                    background: isCircle && !disabled ? '#00A9E5' : 'transparent',
                    color: disabled ? '#D1D2D7' : isCircle ? '#ffffff' : inRange ? '#00A9E5' : '#060B11',
                  }}
                >{yr}</button>
              </div>
            )
          })}
        </div>
      </>
    )
  }

  return (
    <div ref={ref} style={{ position: 'relative' }}>
      <div className="date-picker-wrap" style={{ cursor: 'pointer' }} onClick={handleOpen}>
        <input type="text" readOnly value={getInputDisplay()} style={{ cursor: 'pointer' }} />
        <div className="icon-date" />
      </div>
      {open && (
        <div style={{
          position: 'absolute', top: '110%', left: 0, zIndex: 300,
          background: '#fff', borderRadius: 12, boxShadow: '0 4px 20px rgba(0,0,0,0.15)',
          padding: '20px 12px', width: mode === 'quarterly' ? 300 : 340,
        }}>
          {mode === 'monthly'   && renderMonthPicker()}
          {mode === 'quarterly' && renderQuarterPicker()}
          {mode === 'yearly'    && renderYearPicker()}
        </div>
      )}
    </div>
  )
}
