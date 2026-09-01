export type AnswerSectionKind = 'answer' | 'insight' | 'facts'
export type AnswerSectionStatus = 'pending' | 'streaming' | 'complete' | 'failed'

export interface EvidenceDisplayRecord {
  readonly evidence_id: string
  readonly source_name: string
  readonly identifier: string
  readonly query: string
  readonly counts: {
    readonly received: number
    readonly direct_related: number | null
  }
  readonly record: Readonly<Record<string, string>>
}

export type EvidenceDisplayCatalog = Readonly<Record<string, EvidenceDisplayRecord>>

export interface EvidenceGroupMember {
  readonly evidenceId: string
  readonly label: string
  readonly sourceKey: string
  readonly sourceLabel: string
}

export interface EvidenceSourceBreakdown {
  readonly sourceKey: string
  readonly sourceLabel: string
  readonly count: number
}

export interface EvidenceGroup {
  readonly schema: 'jw.evidence-group.v1'
  readonly groupId: string
  readonly primary: EvidenceGroupMember
  readonly members: readonly EvidenceGroupMember[]
  readonly sourceBreakdown: readonly EvidenceSourceBreakdown[]
}

export type AnswerSectionPart =
  | { readonly type: 'text'; readonly text: string }
  | { readonly type: 'evidence'; readonly evidenceId: string; readonly label: string; readonly group?: EvidenceGroup }

export interface AnswerSectionState {
  readonly id: string
  readonly order: number
  readonly kind: AnswerSectionKind
  readonly title?: string
  readonly status: AnswerSectionStatus
  readonly parts: readonly AnswerSectionPart[]
  readonly evidenceCatalog?: EvidenceDisplayCatalog
}

interface SectionMetadataEnvelope {
  readonly schema: 'jw.answer-sections.v1'
  readonly sections: readonly unknown[]
  readonly evidence_catalog?: Readonly<Record<string, unknown>>
}

interface SectionDeltaEnvelope {
  readonly schema: 'jw.answer-section-delta.v1'
  readonly section_id: string
  readonly delta?: string
  readonly evidence?: readonly unknown[]
  readonly evidence_group?: unknown
  readonly status?: AnswerSectionStatus
}

interface PersistedSectionParagraph {
  readonly text: string
  readonly paragraph_start: boolean
  readonly evidence: readonly unknown[]
  readonly evidence_group?: unknown
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function isStatus(value: unknown): value is AnswerSectionStatus {
  return value === 'pending' || value === 'streaming' || value === 'complete' || value === 'failed'
}

function parseEvidenceGroupMember(value: unknown): EvidenceGroupMember | undefined {
  if (!isRecord(value)
    || typeof value.evidence_id !== 'string' || !value.evidence_id.trim()
    || typeof value.label !== 'string' || !value.label.trim()
    || typeof value.source_key !== 'string' || !value.source_key.trim()
    || typeof value.source_label !== 'string' || !value.source_label.trim()) return undefined
  return {
    evidenceId: value.evidence_id,
    label: value.label,
    sourceKey: value.source_key,
    sourceLabel: value.source_label,
  }
}

function parseEvidenceGroup(value: unknown): EvidenceGroup | undefined {
  if (!isRecord(value)
    || value.schema !== 'jw.evidence-group.v1'
    || typeof value.group_id !== 'string' || !value.group_id.trim()
    || !Array.isArray(value.members) || value.members.length < 2
    || !Array.isArray(value.source_breakdown) || value.source_breakdown.length === 0) return undefined
  const primary = parseEvidenceGroupMember(value.primary)
  const members = value.members.map(parseEvidenceGroupMember)
  if (!primary || members.some(member => member === undefined)) return undefined
  const uniqueMembers = [...new Map((members as EvidenceGroupMember[]).map(member => [member.evidenceId, member])).values()]
  if (uniqueMembers.length < 2 || !uniqueMembers.some(member => member.evidenceId === primary.evidenceId)) return undefined
  const sourceBreakdown: EvidenceSourceBreakdown[] = []
  for (const raw of value.source_breakdown) {
    if (!isRecord(raw)
      || typeof raw.source_key !== 'string' || !raw.source_key.trim()
      || typeof raw.source_label !== 'string' || !raw.source_label.trim()
      || !Number.isInteger(raw.count) || (raw.count as number) < 0) return undefined
    sourceBreakdown.push({ sourceKey: raw.source_key, sourceLabel: raw.source_label, count: raw.count as number })
  }
  return {
    schema: 'jw.evidence-group.v1',
    groupId: value.group_id,
    primary,
    members: uniqueMembers,
    sourceBreakdown,
  }
}

function evidenceGroupLabel(group: EvidenceGroup): string {
  const sourceLabels = [...new Set(group.sourceBreakdown.filter(source => source.count > 0).map(source => source.sourceLabel))]
  return `출처: ${(sourceLabels.length > 0 ? sourceLabels : group.members.map(member => member.sourceLabel)).join(' + ')}`
}

function parseEvidenceParts(evidence: readonly unknown[], evidenceGroupValue: unknown): AnswerSectionPart[] | undefined {
  const group = parseEvidenceGroup(evidenceGroupValue)
  if (group) return [{ type: 'evidence', evidenceId: group.primary.evidenceId, label: evidenceGroupLabel(group), group }]
  const parts: AnswerSectionPart[] = []
  for (const raw of evidence) {
    if (!isRecord(raw) || typeof raw.evidence_id !== 'string' || !raw.evidence_id.trim()
      || (raw.label !== undefined && typeof raw.label !== 'string')) return undefined
    parts.push({
      type: 'evidence',
      evidenceId: raw.evidence_id,
      label: typeof raw.label === 'string' && raw.label.trim() ? raw.label : '출처',
    })
  }
  return parts
}

function parseEvidenceCatalog(value: unknown): EvidenceDisplayCatalog {
  if (!isRecord(value)) return {}
  const catalog: Record<string, EvidenceDisplayRecord> = {}
  for (const [evidenceId, raw] of Object.entries(value)) {
    if (!evidenceId || !isRecord(raw)
      || raw.evidence_id !== evidenceId
      || typeof raw.source_name !== 'string'
      || typeof raw.identifier !== 'string'
      || typeof raw.query !== 'string'
      || !isRecord(raw.counts)
      || !Number.isInteger(raw.counts.received)
      || (raw.counts.direct_related !== null && !Number.isInteger(raw.counts.direct_related))
      || !isRecord(raw.record)) continue
    const record: Record<string, string> = {}
    for (const [label, item] of Object.entries(raw.record)) {
      if (typeof item === 'string' && label.trim()) record[label] = item
    }
    catalog[evidenceId] = {
      evidence_id: evidenceId,
      source_name: raw.source_name,
      identifier: raw.identifier,
      query: raw.query,
      counts: {
        received: raw.counts.received as number,
        direct_related: raw.counts.direct_related as number | null,
      },
      record,
    }
  }
  return catalog
}

export function parseAnswerSectionMetadata(value: unknown): AnswerSectionState[] | undefined {
  if (!isRecord(value) || value.schema !== 'jw.answer-sections.v1' || !Array.isArray(value.sections) || value.sections.length === 0) return undefined
  const seenIds = new Set<string>()
  const seenOrders = new Set<number>()
  const sections: AnswerSectionState[] = []
  const evidenceCatalog = parseEvidenceCatalog(value.evidence_catalog)
  for (const raw of (value as unknown as SectionMetadataEnvelope).sections) {
    if (!isRecord(raw)
      || typeof raw.id !== 'string'
      || !raw.id.trim()
      || !Number.isInteger(raw.order)
      || (raw.order as number) < 0
      || (raw.kind !== 'answer' && raw.kind !== 'insight' && raw.kind !== 'facts')
      || !isStatus(raw.status)
      || (raw.title !== undefined && raw.title !== null && typeof raw.title !== 'string')
      || seenIds.has(raw.id)
      || seenOrders.has(raw.order as number)) return undefined
    seenIds.add(raw.id)
    seenOrders.add(raw.order as number)
    sections.push({
      id: raw.id,
      order: raw.order as number,
      kind: raw.kind,
      ...(typeof raw.title === 'string' && raw.title.trim() ? { title: raw.title } : {}),
      status: raw.status,
      parts: [],
      evidenceCatalog,
    })
  }
  return sections.sort((left, right) => left.order - right.order)
}

export function parsePersistedAnswerSections(
  value: unknown,
  evidenceCatalogOverride?: unknown,
): AnswerSectionState[] | undefined {
  if (!isRecord(value) || !isRecord(value.paragraphs)) return undefined
  const metadata = parseAnswerSectionMetadata({
    ...value,
    evidence_catalog: evidenceCatalogOverride ?? value.evidence_catalog,
  })
  if (!metadata) return undefined

  const restored: AnswerSectionState[] = []
  for (const section of metadata) {
    const rawParagraphs = value.paragraphs[section.id]
    if (!Array.isArray(rawParagraphs)) return undefined
    const parts: AnswerSectionPart[] = []
    for (const [index, raw] of rawParagraphs.entries()) {
      if (!isRecord(raw)
        || typeof raw.text !== 'string'
        || typeof raw.paragraph_start !== 'boolean'
        || !Array.isArray(raw.evidence)) return undefined
      const paragraph = raw as unknown as PersistedSectionParagraph
      const prefix = index === 0 ? '' : (paragraph.paragraph_start ? '\n\n' : ' ')
      if (paragraph.text) parts.push({ type: 'text', text: `${prefix}${paragraph.text}` })
      const evidenceParts = parseEvidenceParts(paragraph.evidence, paragraph.evidence_group)
      if (!evidenceParts) return undefined
      parts.push(...evidenceParts)
    }
    restored.push({ ...section, status: 'complete', parts })
  }
  return answerSectionsHaveContent(restored) ? restored : undefined
}

export function applyAnswerSectionDelta(
  sections: readonly AnswerSectionState[],
  value: unknown,
): AnswerSectionState[] | undefined {
  if (!isRecord(value)
    || value.schema !== 'jw.answer-section-delta.v1'
    || typeof value.section_id !== 'string'
    || (value.delta !== undefined && typeof value.delta !== 'string')
    || (value.status !== undefined && !isStatus(value.status))
    || (value.evidence !== undefined && !Array.isArray(value.evidence))) return undefined

  const envelope = value as unknown as SectionDeltaEnvelope
  const index = sections.findIndex(section => section.id === envelope.section_id)
  if (index < 0) return undefined
  const additions: AnswerSectionPart[] = []
  if (envelope.delta) additions.push({ type: 'text', text: envelope.delta })
  const evidenceParts = parseEvidenceParts(envelope.evidence ?? [], envelope.evidence_group)
  if (!evidenceParts) return undefined
  additions.push(...evidenceParts)
  const next = sections.map((section, sectionIndex) => sectionIndex === index
    ? {
        ...section,
        status: envelope.status ?? (additions.length > 0 ? 'streaming' : section.status),
        parts: [...section.parts, ...additions],
      }
    : section)
  return next.sort((left, right) => left.order - right.order)
}

export function answerSectionsHaveContent(sections: readonly AnswerSectionState[] | undefined): boolean {
  return sections?.some(section => section.parts.some(part => part.type === 'text' ? part.text.trim() : true)) ?? false
}

export function answerSectionsToPlainMarkdown(sections: readonly AnswerSectionState[] | undefined): string {
  if (!sections) return ''
  return sections.map(section => {
    const body = section.parts.map(part => part.type === 'text' ? part.text : `[${part.label}]`).join('')
    return section.kind === 'facts' ? `## 조사 결과\n\n${body}` : body
  }).filter(Boolean).join('\n\n')
}
