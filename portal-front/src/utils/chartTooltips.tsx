import { Fragment, type ReactNode } from 'react'
import { tooltipOrFallback } from './tooltipCopy'
// 원인분석(AnalyzePage) info 아이콘 hover 툴팁 본문 — PDF 수정사항_20260601 기반
// 마커는 PDF 표기 그대로 '•' 사용

// ============ 텍스트 → 퍼블 type-n01 마크업 변환 ============
// - 모든 줄이 '•'로 시작하는 단락 → <ul.s-txt><li>...</li></ul>
// - "차트 범례 안내\n범례를 클릭하면..." 단락 → <div.s-tit> + <div.s-txt><span>범례를 클릭</span>...</div>
// - 그 외 단락 → <div.s-txt>
export function TooltipBody({ text }: { text: string | undefined }): ReactNode {
  const HIGHLIGHT = '범례를 클릭'
  const blocks = tooltipOrFallback(text).split(/\n\n+/)
  return blocks.map((block, i) => {
    const lines = block.split('\n').filter(l => l.length > 0)
    if (lines.length === 0) return null
    if (lines[0]!.startsWith('•')) {
      const items: string[][] = []
      lines.forEach(l => {
        if (l.startsWith('•')) items.push([l.replace(/^•\s*/, '')])
        else if (items.length) items[items.length - 1]!.push(l)
      })
      return (
        <ul key={i} className="s-txt">
          {items.map((parts, j) => (
            <li key={j}>{parts.map((p, k) => <Fragment key={k}>{k > 0 && <br />}{p}</Fragment>)}</li>
          ))}
        </ul>
      )
    }
    if (lines.length === 2 && lines[0] === '차트 범례 안내') {
      const second = lines[1]!
      const idx = second.indexOf(HIGHLIGHT)
      const body: ReactNode = idx === -1 ? second : (
        <>
          {second.slice(0, idx)}
          <span>{HIGHLIGHT}</span>
          {second.slice(idx + HIGHLIGHT.length)}
        </>
      )
      return (
        <Fragment key={i}>
          <div className="s-tit">차트 범례 안내</div>
          <div className="s-txt">{body}</div>
        </Fragment>
      )
    }
    return <div key={i} className="s-txt">{block}</div>
  })
}
