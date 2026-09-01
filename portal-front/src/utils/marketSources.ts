import { displayBackendText, displaySourceLabel } from './portalDisplayLabels.ts'

export interface MarketSourceLink {
  href: string
  displayText: string
  publishedAt?: string
}

export interface MarketSourceItem {
  label: string
  query?: string
  detail?: string
  recordCount?: number
  links: MarketSourceLink[]
}

export interface MarketSourceCount {
  value: number
  origin: 'backend' | 'derived'
}

export interface ParsedMarketAnswerSources {
  bodyMarkdown: string
  sources: MarketSourceItem[]
  sourceSectionIndex?: number
}

const SOURCE_HEADING_RE = /^#{1,6}[ \t]+출처[ \t]*$/
const SOURCE_ITEM_RE = /^-\s+(.+?)(?:\s+—\s+(.+))?$/
const SOURCE_LINK_RE = /^\s+-\s+\[(.+)]\((https?:\/\/.+)\)\s*$/
const TOP_LEVEL_SOURCE_LINK_RE = /^\[(.+)]\((https?:\/\/.+)\)$/

function levelTwoHeadingOffsets(markdown: string): number[] {
  const offsets: number[] = []
  let offset = 0
  let fence: { character: '`' | '~'; length: number } | undefined
  while (offset < markdown.length) {
    const newline = markdown.indexOf('\n', offset)
    const lineEnd = newline === -1 ? markdown.length : newline
    const line = markdown.slice(offset, lineEnd).replace(/\r$/, '')
    const marker = line.match(/^ {0,3}(`{3,}|~{3,})/)?.[1]
    if (fence) {
      if (marker && marker[0] === fence.character && marker.length >= fence.length) fence = undefined
    } else if (marker) {
      fence = { character: marker[0] as '`' | '~', length: marker.length }
    } else if (/^##[ \t]+\S/.test(line)) {
      offsets.push(offset)
    }
    if (newline === -1) break
    offset = newline + 1
  }
  return offsets
}

const SOURCE_ALIASES: readonly (readonly string[])[] = [
  ['hira', '건강보험심사평가원', '심평원'],
  ['mfds', '식품의약품안전처', '식약처', 'nedrug', '의약품안전나라'],
  ['mart', '내부데이터마트', '데이터마트', 'ubist', 'iqvia'],
  ['patent', '특허자료', '특허'],
  ['web', '웹자료', '외부데이터원천'],
  ['fda', 'openfda'],
  ['clinicaltrials', 'clinicaltrials.gov'],
]

function compactLabel(value: string): string {
  return value.toLocaleLowerCase().replace(/[\s._()·/:-]+/g, '')
}

function canonicalLabel(value: string): string {
  const compact = compactLabel(value)
  const group = SOURCE_ALIASES.find(aliases => aliases.some(alias => compact === compactLabel(alias)))
  return group?.[0] ?? compact
}

function decodeDisplayText(value: string): string {
  try {
    return decodeURIComponent(value)
  } catch {
    return value.replace(/(?:%[0-9A-Fa-f]{2})+/g, encoded => {
      try { return decodeURIComponent(encoded) } catch { return encoded }
    })
  }
}

function normalizeDisplayQuotes(value: string): string {
  return value
    .replace(/"{2,}/g, '"')
    .replace(/“{2,}/g, '“')
    .replace(/”{2,}/g, '”')
    .replace(/\s+/g, ' ')
    .trim()
}

function isPrivateIpv4(hostname: string): boolean {
  const octets = hostname.split('.').map(Number)
  if (octets.length !== 4 || octets.some(value => !Number.isInteger(value) || value < 0 || value > 255)) return false
  return octets[0] === 10
    || octets[0] === 127
    || (octets[0] === 192 && octets[1] === 168)
    || (octets[0] === 172 && octets[1] >= 16 && octets[1] <= 31)
}

function externalHttpUrl(value: string): URL | null {
  try {
    const url = new URL(value)
    if (url.protocol !== 'http:' && url.protocol !== 'https:') return null
    const hostname = url.hostname.toLocaleLowerCase()
    if (
      hostname === 'localhost'
      || !hostname.includes('.')
      || hostname.endsWith('.local')
      || hostname.endsWith('.svc')
      || hostname.endsWith('.svc.cluster.local')
      || isPrivateIpv4(hostname)
    ) return null
    return url
  } catch {
    return null
  }
}

function formatLinkText(rawText: string, url: URL): string {
  const decoded = normalizeDisplayQuotes(decodeDisplayText(rawText))
  const hostname = url.hostname.replace(/^www\./, '')
  if (!decoded || /^https?:\/\//i.test(decoded)) {
    const lastPath = url.pathname.split('/').filter(Boolean).at(-1)
    const title = lastPath ? normalizeDisplayQuotes(decodeDisplayText(lastPath)) : '원문'
    return `${hostname} · ${title}`
  }
  return compactLabel(decoded).includes(compactLabel(hostname)) ? decoded : `${hostname} · ${decoded}`
}

function sourceLabelForUrl(url: URL): string {
  const hostname = url.hostname.toLocaleLowerCase().replace(/^www\./, '')
  if (hostname === 'clinicaltrials.gov') return 'ClinicalTrials.gov'
  if (hostname === 'patents.google.com') return '특허'
  if (hostname === 'hira.or.kr') return '건강보험심사평가원'
  return hostname
}

function parseSourceDescription(value: string | undefined): Pick<MarketSourceItem, 'query' | 'detail'> {
  if (!value) return {}
  const match = value.trim().match(/^["“](.*)["”]\s*(.*)$/)
  if (!match) return { detail: normalizeDisplayQuotes(value) }
  return {
    query: normalizeDisplayQuotes(match[1]),
    detail: normalizeDisplayQuotes(match[2]) || undefined,
  }
}

export function parseMarketAnswerSources(markdown: string): ParsedMarketAnswerSources {
  let headingIndex = -1
  let headingText = ''
  let offset = 0
  let fence: { character: '`' | '~'; length: number } | undefined
  while (offset < markdown.length) {
    const newline = markdown.indexOf('\n', offset)
    const lineEnd = newline === -1 ? markdown.length : newline
    const rawLine = markdown.slice(offset, lineEnd)
    const line = rawLine.endsWith('\r') ? rawLine.slice(0, -1) : rawLine
    const marker = line.match(/^ {0,3}(`{3,}|~{3,})/)?.[1]

    if (fence) {
      if (
        marker
        && marker[0] === fence.character
        && marker.length >= fence.length
        && new RegExp(`^ {0,3}\\${fence.character}{${fence.length},}[ \\t]*$`).test(line)
      ) fence = undefined
    } else if (marker) {
      fence = { character: marker[0] as '`' | '~', length: marker.length }
    } else if (SOURCE_HEADING_RE.test(line)) {
      headingIndex = offset
      headingText = rawLine
      break
    }

    if (newline === -1) break
    offset = newline + 1
  }
  if (headingIndex === -1) return { bodyMarkdown: displayBackendText(markdown), sources: [] }

  const sources: MarketSourceItem[] = []
  let current: MarketSourceItem | undefined
  const followingHeading = levelTwoHeadingOffsets(markdown)
    .find(candidate => candidate > headingIndex)
  const sectionEnd = followingHeading ?? markdown.length
  const section = markdown.slice(headingIndex + headingText.length, sectionEnd)

  for (const line of section.split('\n')) {
    const linkMatch = line.match(SOURCE_LINK_RE)
    if (linkMatch) {
      const url = externalHttpUrl(linkMatch[2].trim())
      if (current && url) {
        current.links.push({
          href: url.href,
          displayText: formatLinkText(linkMatch[1], url),
        })
      }
      continue
    }

    const itemMatch = line.match(SOURCE_ITEM_RE)
    if (!itemMatch) continue
    const directLinkMatch = itemMatch[1].match(TOP_LEVEL_SOURCE_LINK_RE)
    if (directLinkMatch) {
      const url = externalHttpUrl(directLinkMatch[2].trim())
      if (!url) continue
      const label = sourceLabelForUrl(url)
      current = sources.find(source => source.label === label)
      if (!current) {
        current = { label, links: [] }
        sources.push(current)
      }
      current.links.push({ href: url.href, displayText: formatLinkText(directLinkMatch[1], url) })
      continue
    }
    const description = parseSourceDescription(itemMatch[2])
    current = {
      label: displaySourceLabel(normalizeDisplayQuotes(itemMatch[1])),
      ...description,
      links: [],
    }
    sources.push(current)
  }

  const before = markdown.slice(0, headingIndex).trimEnd()
  const after = markdown.slice(sectionEnd).trimStart()
  return {
    bodyMarkdown: displayBackendText([before, after].filter(Boolean).join('\n\n')),
    sources,
    sourceSectionIndex: levelTwoHeadingOffsets(before).length,
  }
}

export function findMarketSourceIndex(label: string, sources: readonly MarketSourceItem[]): number {
  const exact = compactLabel(label)
  const exactIndex = sources.findIndex(source => compactLabel(source.label) === exact)
  if (exactIndex !== -1) return exactIndex

  const canonical = canonicalLabel(label)
  return sources.findIndex(source => canonicalLabel(source.label) === canonical)
}

export function getMarketSourceCount(source: MarketSourceItem): MarketSourceCount | undefined {
  const backendCount = source.recordCount
  if (backendCount !== undefined && Number.isInteger(backendCount) && backendCount >= 0) {
    return { value: backendCount, origin: 'backend' }
  }
  if (source.links.length > 0) {
    return { value: source.links.length, origin: 'derived' }
  }
  return undefined
}
