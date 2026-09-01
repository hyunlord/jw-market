import { useMemo } from 'react'
import ReactMarkdown, { type Components } from 'react-markdown'

import type { AnswerSectionState, EvidenceDisplayCatalog, EvidenceGroup } from '../../utils/answerSections'
import type { MarketTable } from '../../utils/marketTables'
import type { SelectionPolicy } from '../../utils/traceToolResults'
import { evidenceAnchorId, evidenceIdFromAnchor } from '../../utils/evidenceAnchors'
import { prepareEvidenceDisplay, type PreparedEvidencePart } from '../../utils/evidencePopover'
import { portalMarkdownRehypePlugins, portalMarkdownRemarkPlugins } from '../../utils/markdownSanitize'
import MarketTables from './MarketTables'

interface Props {
  sections: readonly AnswerSectionState[]
  components: Components
  onEvidenceOpen?: (
    evidenceId: string,
    evidence: readonly { evidenceId: string; label: string }[],
    catalog: EvidenceDisplayCatalog | undefined,
    group: EvidenceGroup | undefined,
  ) => void
  tables?: readonly MarketTable[]
  tableError?: string
  selectionPolicy?: SelectionPolicy
}

function sectionMarkdown(parts: readonly PreparedEvidencePart[]): string {
  return parts.map(part => part.type === 'text'
    ? part.text
    : `[${part.label}](#${evidenceAnchorId(part.lookupKey)})`).join('')
}

export default function SectionSlotAnswer({
  sections,
  components,
  onEvidenceOpen,
  tables = [],
  tableError,
  selectionPolicy,
}: Props) {
  const ordered = useMemo(() => [...sections].sort((left, right) => left.order - right.order), [sections])
  const hasFactsSection = ordered.some(section => section.kind === 'facts')

  return <div className="answer-section-slots" data-answer-sections="jw.answer-sections.v1">
    {ordered.map(section => {
      const prepared = prepareEvidenceDisplay(section.parts)
      const markdown = sectionMarkdown(prepared.visibleParts)
      const loading = section.status === 'pending' || section.status === 'streaming'
      return <section
        key={section.id}
        className={`answer-section-slot answer-section-${section.kind}`}
        data-section-id={section.id}
        data-section-order={section.order}
        data-section-status={section.status}
        aria-live="polite"
      >
        {section.kind === 'facts' && <h2>{section.title?.trim() || '조사 결과'}</h2>}
        {markdown && <ReactMarkdown
          remarkPlugins={portalMarkdownRemarkPlugins}
          rehypePlugins={portalMarkdownRehypePlugins}
          components={{ ...components, a: ({ href, children }) => {
            const lookupKey = href?.startsWith('#') ? evidenceIdFromAnchor(href.slice(1)) : undefined
            const target = lookupKey ? prepared.targetsByLookupKey.get(lookupKey) : undefined
            if (!target) {
              const BaseAnchor = components.a
              return BaseAnchor
                ? <BaseAnchor href={href}>{children}</BaseAnchor>
                : <a href={href} target="_blank" rel="noopener noreferrer">{children}</a>
            }
            return <button type="button" className="answer-evidence-marker" data-evidence-id={target.evidenceId} data-evidence-group-id={target.group?.groupId} onClick={() => onEvidenceOpen?.(target.evidenceId, target.evidence, section.evidenceCatalog, target.group)}>[{children}]</button>
          } }}
        >{markdown}</ReactMarkdown>}
        {section.kind === 'facts' && <MarketTables tables={tables} error={tableError} selectionPolicy={selectionPolicy} />}
        {loading && <div className="answer-section-loading" role="status"><span className="fixed-8bar-spinner" aria-hidden="true">{Array.from({ length: 8 }, (_, index) => <span key={index} className={`bar bar${index + 1}`} />)}</span>생성 중</div>}
        {section.status === 'failed' && !markdown && <p className="answer-section-failed" role="status">이 섹션을 생성하지 못했습니다.</p>}
      </section>
    })}
    {!hasFactsSection && <MarketTables tables={tables} error={tableError} selectionPolicy={selectionPolicy} />}
  </div>
}
