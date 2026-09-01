import { Fragment, useState, type MouseEvent, type ReactNode } from 'react'
import type { MarketSourceItem } from '../../utils/marketSources'
import { findMarketSourceIndex, getMarketSourceCount } from '../../utils/marketSources'

interface MarketSourcesSectionProps {
  sources: readonly MarketSourceItem[]
  anchorPrefix: string
  hideTitle?: boolean
}

interface MarketSourceCitationTextProps extends MarketSourcesSectionProps {
  text: string
  onInspectionSourceOpen?: (sourceLabel: string) => void
}

const INLINE_SOURCE_RE = /\[출처\s*:\s*([^\]]+)]/g

function sourceAnchorId(anchorPrefix: string, index: number): string {
  return `${anchorPrefix}-${index}`
}

function handleAnchorClick(event: MouseEvent<HTMLAnchorElement>, anchorId: string): void {
  const target = document.getElementById(anchorId)
  if (!target) return
  event.preventDefault()
  const collapsedToggle = target.querySelector<HTMLButtonElement>('.market-source-toggle[aria-expanded="false"]')
  collapsedToggle?.click()
  target.scrollIntoView({ behavior: 'smooth', block: 'center' })
  target.classList.remove('is-highlighted')
  requestAnimationFrame(() => target.classList.add('is-highlighted'))
  window.setTimeout(() => target.classList.remove('is-highlighted'), 1000)
}

export function MarketSourceCitationText({ text, sources, anchorPrefix, onInspectionSourceOpen }: MarketSourceCitationTextProps) {
  const nodes: ReactNode[] = []
  let last = 0
  let match: RegExpExecArray | null
  const re = new RegExp(INLINE_SOURCE_RE.source, 'g')

  while ((match = re.exec(text)) !== null) {
    const matchIndex = match.index
    const matchLength = match[0].length
    if (matchIndex > last) nodes.push(text.slice(last, matchIndex))
    const labels = match[1].split(/\s*[,，]\s*/).map(label => label.trim()).filter(Boolean)
    nodes.push('[출처: ')
    labels.forEach((label, labelIndex) => {
      if (labelIndex > 0) nodes.push(', ')
      const sourceIndex = findMarketSourceIndex(label, sources)
      if (sourceIndex === -1) {
        nodes.push(label)
        return
      }
      const source = sources[sourceIndex]
      if (onInspectionSourceOpen) {
        nodes.push(
          <a
            className="market-source-reference"
            href="#answer-inspection-panel"
            data-inspection-source-label={label}
            onClick={event => {
              event.preventDefault()
              onInspectionSourceOpen(source.label)
            }}
            key={`${matchIndex}-${labelIndex}`}
          >
            {label}
          </a>,
        )
        return
      }
      const externalLinks = source.links
      if (externalLinks.length === 1) {
        nodes.push(
          <a
            className="market-source-reference"
            href={externalLinks[0].href}
            target="_blank"
            rel="noopener noreferrer"
            key={`${matchIndex}-${labelIndex}`}
          >
            {label}
          </a>,
        )
        return
      }
      const anchorId = sourceAnchorId(anchorPrefix, sourceIndex)
      nodes.push(
        <a
          className="market-source-reference"
          href={`#${anchorId}`}
          onClick={event => handleAnchorClick(event, anchorId)}
          key={`${matchIndex}-${labelIndex}`}
        >
          {label}
        </a>,
      )
    })
    nodes.push(']')
    last = matchIndex + matchLength
  }

  if (last < text.length) nodes.push(text.slice(last))
  return <>{nodes.map((node, index) => <Fragment key={index}>{node}</Fragment>)}</>
}

export function MarketSourcesSection({ sources, anchorPrefix, hideTitle = false }: MarketSourcesSectionProps) {
  const [expanded, setExpanded] = useState<Record<number, boolean>>({})
  if (sources.length === 0) return null

  const collapsibleIndexes = sources.flatMap((source, index) => source.links.length > 0 ? [index] : [])
  const allExpanded = collapsibleIndexes.length > 0
    && collapsibleIndexes.every(index => expanded[index] === true)
  const setAllExpanded = (nextExpanded: boolean) => {
    setExpanded(Object.fromEntries(collapsibleIndexes.map(index => [index, nextExpanded])))
  }

  return (
    <section className="market-sources" aria-label="출처">
      {!hideTitle && (
        <div className="market-sources-title">
          출처 <span>{sources.length}</span>
        </div>
      )}
      {collapsibleIndexes.length > 0 && (
        <div className="market-sources-toolbar">
          <button type="button" onClick={() => setAllExpanded(!allExpanded)}>
            {allExpanded ? '출처 모두 접기' : '출처 모두 펼치기'}
          </button>
        </div>
      )}
      <ul className="market-sources-list">
        {sources.map((source, sourceIndex) => {
          const panelId = `${sourceAnchorId(anchorPrefix, sourceIndex)}-items`
          const isCollapsible = source.links.length > 0
          const isExpanded = expanded[sourceIndex] ?? false
          const count = getMarketSourceCount(source)
          const firstLink = source.links[0]
          const additionalLinkCount = Math.max(0, source.links.length - 1)

          return (
            <li
              className={`market-source-item${isExpanded ? ' is-open' : ''}`}
              id={sourceAnchorId(anchorPrefix, sourceIndex)}
              key={`${source.label}-${sourceIndex}`}
            >
              <div className="market-source-heading">
                {isCollapsible ? (
                  <button
                    className="market-source-toggle"
                    type="button"
                    aria-label={`${source.label} ${isExpanded ? '접기' : '펼치기'}`}
                    aria-controls={panelId}
                    aria-expanded={isExpanded}
                    onClick={() => setExpanded(current => ({ ...current, [sourceIndex]: !isExpanded }))}
                  >
                    <span className="market-source-chevron" aria-hidden="true" />
                  </button>
                ) : null}
                <span className="market-source-heading-text"><strong>{source.label}</strong></span>
                {firstLink && <a className="market-source-preview-link" href={firstLink.href} target="_blank" rel="noopener noreferrer">{firstLink.displayText}</a>}
                {additionalLinkCount > 0 && <span className="market-source-more">외 {additionalLinkCount}건</span>}
                {count && <span className="market-source-count" data-count-origin={count.origin}>{count.value}건</span>}
              </div>
              {isCollapsible && (
                <ul className="market-source-links" id={panelId} hidden={!isExpanded}>
                  {(source.query || source.detail) && <li className="market-source-query">{source.query && <span>검색어 “{source.query}”</span>}{source.detail && <span> {source.detail}</span>}</li>}
                  {source.links.map((link, linkIndex) => (
                    <li key={`${link.href}-${linkIndex}`}>
                      <a href={link.href} target="_blank" rel="noopener noreferrer">
                        {link.displayText}
                      </a>
                      {link.publishedAt && <time dateTime={link.publishedAt}>{link.publishedAt}</time>}
                    </li>
                  ))}
                </ul>
              )}
            </li>
          )
        })}
      </ul>
    </section>
  )
}
