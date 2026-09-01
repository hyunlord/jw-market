import { Fragment, useMemo, useState, type ReactNode } from 'react'
import ReactMarkdown, { type Components } from 'react-markdown'
import {
  createInitialSectionState,
  hasCollapsedSection,
  parseChatAnswerSections,
  orderChatAnswerSections,
  setAllSectionsExpanded,
  shouldUseSectionCollapse,
  type ChatAnswerSection,
  type ChatAnswerSectionState,
} from '../../utils/chatAnswerSections'
import { portalMarkdownRehypePlugins, portalMarkdownRemarkPlugins } from '../../utils/markdownSanitize'

interface CollapsibleAnswerMarkdownProps {
  markdown: string
  components: Components
  idPrefix: string
  sourceCount: number
  sourceSectionIndex?: number
  renderSources: ReactNode
  collapseEnabled: boolean
  /** Set only after the backend's explicit ordering marker contract is known. */
  backendOrdered?: boolean
}

function Markdown({ markdown, components }: Pick<CollapsibleAnswerMarkdownProps, 'markdown' | 'components'>) {
  if (!markdown) return null
  return (
    <ReactMarkdown
      remarkPlugins={portalMarkdownRemarkPlugins}
      rehypePlugins={portalMarkdownRehypePlugins}
      components={components}
    >
      {markdown}
    </ReactMarkdown>
  )
}

export default function CollapsibleAnswerMarkdown({
  markdown,
  components,
  idPrefix,
  sourceCount,
  sourceSectionIndex,
  renderSources,
  collapseEnabled,
  backendOrdered = false,
}: CollapsibleAnswerMarkdownProps) {
  const parsed = useMemo(
    () => parseChatAnswerSections(markdown, { dataSectionStartIndex: sourceSectionIndex }),
    [markdown, sourceSectionIndex],
  )
  const useCollapse = collapseEnabled && shouldUseSectionCollapse(parsed)
  const hasUsableSourceBoundary = sourceSectionIndex !== undefined
    && sourceSectionIndex < parsed.sections.length
  const [expanded, setExpanded] = useState<ChatAnswerSectionState>(() => createInitialSectionState(parsed))

  const sourceNode = sourceCount > 0 ? (
    <section className="answer-source-section" aria-label="출처">
      <h2 className="answer-sources-heading">출처</h2>
      {renderSources}
    </section>
  ) : null

  if (!useCollapse) {
    return (
      <>
        <Markdown markdown={markdown} components={components} />
        {sourceNode}
      </>
    )
  }
  const anyCollapsed = hasCollapsedSection(parsed, expanded)
  const setAll = (nextExpanded: boolean) => {
    setExpanded(setAllSectionsExpanded(parsed, nextExpanded))
  }

  const renderDataSection = (section: ChatAnswerSection) => {
    const sectionId = `${idPrefix}-${section.id}`
    const isExpanded = expanded[section.id] ?? false
    return (
      <section className={`answer-section${isExpanded ? ' is-open' : ' is-closed'}`} key={section.id}>
        <h2 className="answer-section-heading">
          <button
            type="button"
            aria-controls={sectionId}
            aria-expanded={isExpanded}
            onClick={() => setExpanded(current => ({ ...current, [section.id]: !isExpanded }))}
          >
            <span>{section.displayTitle}</span>
            {section.countLabel && <span className="answer-section-count">{section.countLabel}</span>}
            <span className="answer-section-chevron" aria-hidden="true" />
          </button>
        </h2>
        <div id={sectionId} hidden={!isExpanded}>
          <Markdown markdown={section.bodyMarkdown} components={components} />
        </div>
      </section>
    )
  }

  const toolbar = (
    <div className="answer-sections-toolbar">
      <button type="button" onClick={() => setAll(anyCollapsed)}>
        {anyCollapsed ? '모두 펼치기' : '모두 접기'}
      </button>
    </div>
  )
  const finalOrder = orderChatAnswerSections(parsed, {
    hasSources: sourceNode !== null,
    backendOrdered: backendOrdered || hasUsableSourceBoundary,
    sourceSectionIndex,
  })
  let toolbarRendered = false
  const orderedNodes: ReactNode[] = []
  finalOrder.forEach(item => {
    if (item.type === 'source') {
      if (sourceNode) orderedNodes.push(<Fragment key="sources">{sourceNode}</Fragment>)
      return
    }
    const { section } = item
    if (section.kind === 'data') {
      if (!toolbarRendered) {
        orderedNodes.push(<Fragment key="toolbar">{toolbar}</Fragment>)
        toolbarRendered = true
      }
      orderedNodes.push(renderDataSection(section))
    } else {
      orderedNodes.push(<Markdown key={section.id} markdown={section.markdown} components={components} />)
    }
  })

  return (
    <div
      className="answer-sections"
      data-answer-order={backendOrdered || hasUsableSourceBoundary ? 'backend' : 'portal'}
      data-section-policy="source-boundary-v1"
    >
      <Markdown markdown={parsed.prefixMarkdown} components={components} />
      {orderedNodes}
    </div>
  )
}
