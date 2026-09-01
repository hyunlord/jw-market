export interface ChatAnswerSection {
  id: string
  title: string
  displayTitle: string
  headingMarkdown: string
  bodyMarkdown: string
  markdown: string
  defaultExpanded: boolean
  kind: 'narrative' | 'data' | 'source'
  countLabel?: string
}

export interface ParsedChatAnswerSections {
  prefixMarkdown: string
  sections: ChatAnswerSection[]
}

export type ChatAnswerSectionState = Record<string, boolean>

export type OrderedChatAnswerItem =
  | { type: 'section'; section: ChatAnswerSection }
  | { type: 'source' }

interface HeadingBoundary {
  start: number
  bodyStart: number
  title: string
}

const HEADING_COUNT_RE = /\((\d[\d,]*)\s*(건|개(?:\s*소스)?)\)\s*$/
const LEGACY_DATA_TITLES = ['조사 범위와 완전성'] as const
const LEGACY_DETAIL_TITLE_RE = /(?:정본|상세|보조표|단계 및 상태 집계|회사 및 제품별 그룹|임상시험 전건|보조 자료$)/

function fenceMarker(line: string): { character: '`' | '~'; length: number } | undefined {
  const match = line.match(/^ {0,3}(`{3,}|~{3,})/)
  if (!match) return undefined
  return {
    character: match[1][0] as '`' | '~',
    length: match[1].length,
  }
}

function findHeadingBoundaries(markdown: string): HeadingBoundary[] {
  const boundaries: HeadingBoundary[] = []
  let offset = 0
  let openFence: { character: '`' | '~'; length: number } | undefined

  while (offset < markdown.length) {
    const newline = markdown.indexOf('\n', offset)
    const lineEnd = newline === -1 ? markdown.length : newline
    const bodyStart = newline === -1 ? markdown.length : newline + 1
    const rawLine = markdown.slice(offset, lineEnd)
    const line = rawLine.endsWith('\r') ? rawLine.slice(0, -1) : rawLine
    const marker = fenceMarker(line)

    if (openFence) {
      if (
        marker
        && marker.character === openFence.character
        && marker.length >= openFence.length
        && new RegExp(`^ {0,3}\\${marker.character}{${openFence.length},}[ \\t]*$`).test(line)
      ) {
        openFence = undefined
      }
    } else if (marker) {
      openFence = marker
    } else {
      const heading = line.match(/^##[ \t]+(.+?)(?:[ \t]+#+[ \t]*)?$/)
      if (heading) {
        boundaries.push({ start: offset, bodyStart, title: heading[1].trim() })
      }
    }

    if (newline === -1) break
    offset = newline + 1
  }

  return boundaries
}

function normalizedTitle(title: string): string {
  return title.replace(HEADING_COUNT_RE, '').trim()
}

function sectionKindFor(
  title: string,
  index: number,
  dataSectionStartIndex: number | undefined,
): ChatAnswerSection['kind'] {
  const normalized = normalizedTitle(title)
  if (normalized === '출처') return 'source'
  if (dataSectionStartIndex !== undefined && index >= dataSectionStartIndex) return 'data'
  if (
    LEGACY_DATA_TITLES.some(value => normalized === value)
    || LEGACY_DETAIL_TITLE_RE.test(normalized)
  ) return 'data'
  return 'narrative'
}

function countLabelFor(title: string): string | undefined {
  const headingCount = title.match(HEADING_COUNT_RE)
  if (headingCount) return `${headingCount[1]}${headingCount[2].replace(/\s+/g, ' ')}`
  return undefined
}

export function parseChatAnswerSections(
  markdown: string,
  options: { dataSectionStartIndex?: number } = {},
): ParsedChatAnswerSections {
  const boundaries = findHeadingBoundaries(markdown)
  if (boundaries.length === 0) return { prefixMarkdown: markdown, sections: [] }
  const dataSectionStartIndex = options.dataSectionStartIndex !== undefined
    && options.dataSectionStartIndex < boundaries.length
    ? options.dataSectionStartIndex
    : undefined

  return {
    prefixMarkdown: markdown.slice(0, boundaries[0].start),
    sections: boundaries.map((boundary, index) => {
      const end = boundaries[index + 1]?.start ?? markdown.length
      const headingMarkdown = markdown.slice(boundary.start, boundary.bodyStart)
      const bodyMarkdown = markdown.slice(boundary.bodyStart, end)
      const kind = sectionKindFor(boundary.title, index, dataSectionStartIndex)
      return {
        id: `answer-section-${index}`,
        title: boundary.title,
        displayTitle: normalizedTitle(boundary.title),
        headingMarkdown,
        bodyMarkdown,
        markdown: markdown.slice(boundary.start, end),
        defaultExpanded: kind !== 'data',
        kind,
        countLabel: countLabelFor(boundary.title),
      }
    }).filter(section => section.bodyMarkdown.trim().length > 0),
  }
}

export function reconstructChatAnswerMarkdown(parsed: ParsedChatAnswerSections): string {
  return parsed.prefixMarkdown + parsed.sections.map(section => section.markdown).join('')
}

export function shouldUseSectionCollapse(parsed: ParsedChatAnswerSections): boolean {
  return parsed.sections.some(section => section.kind === 'data')
}

export function partitionChatAnswerSections(parsed: ParsedChatAnswerSections): {
  narrative: ChatAnswerSection[]
  data: ChatAnswerSection[]
} {
  return {
    narrative: parsed.sections.filter(section => section.kind === 'narrative'),
    data: parsed.sections.filter(section => section.kind === 'data'),
  }
}

export function orderChatAnswerSections(
  parsed: ParsedChatAnswerSections,
  options: {
    hasSources: boolean
    backendOrdered?: boolean
    sourceSectionIndex?: number
  },
): OrderedChatAnswerItem[] {
  const sections = parsed.sections.filter(section => section.kind !== 'source')

  if (!options.backendOrdered) {
    const narrative = sections.filter(section => section.kind === 'narrative')
    const data = sections.filter(section => section.kind === 'data')
    return [
      ...narrative.map(section => ({ type: 'section' as const, section })),
      ...(options.hasSources ? [{ type: 'source' as const }] : []),
      ...data.map(section => ({ type: 'section' as const, section })),
    ]
  }

  const sourceIndex = Math.min(
    Math.max(options.sourceSectionIndex ?? sections.length, 0),
    sections.length,
  )
  const ordered: OrderedChatAnswerItem[] = sections.map(section => ({ type: 'section', section }))
  if (options.hasSources) ordered.splice(sourceIndex, 0, { type: 'source' })
  return ordered
}

export function createInitialSectionState(parsed: ParsedChatAnswerSections): ChatAnswerSectionState {
  return Object.fromEntries(
    parsed.sections.filter(section => section.kind === 'data').map(section => [section.id, false]),
  )
}

export function setAllSectionsExpanded(
  parsed: ParsedChatAnswerSections,
  expanded: boolean,
): ChatAnswerSectionState {
  return Object.fromEntries(
    parsed.sections.filter(section => section.kind === 'data').map(section => [section.id, expanded]),
  )
}

export function hasCollapsedSection(
  parsed: ParsedChatAnswerSections,
  state: ChatAnswerSectionState,
): boolean {
  return parsed.sections.some(section => section.kind === 'data' && state[section.id] === false)
}

export function chatAnswerStateKey(markdown: string, sourceCount: number): string {
  let hash = 2166136261
  for (let index = 0; index < markdown.length; index += 1) {
    hash ^= markdown.charCodeAt(index)
    hash = Math.imul(hash, 16777619)
  }
  return `${sourceCount}-${markdown.length}-${(hash >>> 0).toString(16)}`
}
