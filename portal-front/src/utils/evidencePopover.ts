import type { AnswerSectionPart } from './answerSections.ts'
import type { EvidenceDisplayCatalog, EvidenceDisplayRecord, EvidenceGroup } from './answerSections.ts'

export const MAX_VISIBLE_EVIDENCE_MARKERS = 3

type EvidencePart = Extract<AnswerSectionPart, { type: 'evidence' }>

export type PreparedEvidencePart =
  | Extract<AnswerSectionPart, { type: 'text' }>
  | (EvidencePart & { readonly lookupKey: string })

export interface EvidencePopoverTarget {
  readonly evidenceId: string
  readonly evidence: readonly EvidencePart[]
  readonly group?: EvidenceGroup
}

export interface PreparedEvidenceDisplay {
  visibleParts: PreparedEvidencePart[]
  targetsByLookupKey: ReadonlyMap<string, EvidencePopoverTarget>
}

export type EvidencePopoverRecord = EvidenceDisplayRecord

export interface GroupedEvidenceItem {
  readonly evidenceId: string
  readonly label: string
  readonly identifier: string
  readonly available: boolean
}

export interface GroupedEvidenceSource {
  readonly sourceKey: string
  readonly sourceLabel: string
  readonly count: number
  readonly items: readonly GroupedEvidenceItem[]
}

export function evidenceSourceKeyForRecord(
  sources: readonly Pick<GroupedEvidenceSource, 'sourceKey' | 'items'>[],
  evidenceId: string,
): string | undefined {
  return sources.find(source => source.items.some(item => item.evidenceId === evidenceId))?.sourceKey
}

export function evidenceSourceTabIndex(key: string, currentIndex: number, count: number): number | undefined {
  if (count <= 0) return undefined
  if (key === 'Home') return 0
  if (key === 'End') return count - 1
  if (key === 'ArrowRight' || key === 'ArrowDown') return (currentIndex + 1) % count
  if (key === 'ArrowLeft' || key === 'ArrowUp') return (currentIndex - 1 + count) % count
  return undefined
}

function uniqueEvidence(parts: readonly EvidencePart[]): EvidencePart[] {
  const seen = new Set<string>()
  return parts.filter(part => {
    if (seen.has(part.evidenceId)) return false
    seen.add(part.evidenceId)
    return true
  })
}

function availableLookupKey(base: string, targets: ReadonlyMap<string, EvidencePopoverTarget>): string {
  if (!targets.has(base)) return base
  let sequence = 2
  while (targets.has(`${base}#${sequence}`)) sequence += 1
  return `${base}#${sequence}`
}

export function prepareEvidenceDisplay(parts: readonly AnswerSectionPart[]): PreparedEvidenceDisplay {
  const visibleParts: PreparedEvidencePart[] = []
  const targetsByLookupKey = new Map<string, EvidencePopoverTarget>()
  let index = 0
  while (index < parts.length) {
    const part = parts[index]!
    if (part.type === 'text') {
      visibleParts.push(part)
      index += 1
      continue
    }
    if (part.group) {
      const members = part.group.members.map(member => ({
        type: 'evidence' as const,
        evidenceId: member.evidenceId,
        label: member.label,
      }))
      const lookupKey = availableLookupKey(part.group.groupId, targetsByLookupKey)
      targetsByLookupKey.set(lookupKey, { evidenceId: part.evidenceId, evidence: members, group: part.group })
      visibleParts.push({ ...part, lookupKey })
      index += 1
      continue
    }
    const run: EvidencePart[] = []
    while (index < parts.length && parts[index]?.type === 'evidence') {
      run.push(parts[index] as EvidencePart)
      index += 1
    }
    const unique = uniqueEvidence(run)
    for (const evidence of unique.slice(0, MAX_VISIBLE_EVIDENCE_MARKERS)) {
      const lookupKey = availableLookupKey(evidence.evidenceId, targetsByLookupKey)
      targetsByLookupKey.set(lookupKey, { evidenceId: evidence.evidenceId, evidence: unique })
      visibleParts.push({ ...evidence, lookupKey })
    }
  }
  return { visibleParts, targetsByLookupKey }
}

export function evidencePopoverRecord(catalog: EvidenceDisplayCatalog | undefined, evidenceId: string): EvidencePopoverRecord | undefined {
  return catalog?.[evidenceId]
}

export function groupEvidenceSources(group: EvidenceGroup, catalog: EvidenceDisplayCatalog | undefined): GroupedEvidenceSource[] {
  const grouped = new Map<string, { sourceKey: string; sourceLabel: string; count: number; items: GroupedEvidenceItem[] }>()
  for (const source of group.sourceBreakdown) {
    grouped.set(source.sourceKey, { ...source, items: [] })
  }
  for (const member of group.members) {
    const source = grouped.get(member.sourceKey) ?? {
      sourceKey: member.sourceKey,
      sourceLabel: member.sourceLabel,
      count: 0,
      items: [],
    }
    const record = catalog?.[member.evidenceId]
    if (source.items.some(item => item.evidenceId === member.evidenceId)) continue
    source.items.push({
      evidenceId: member.evidenceId,
      label: member.label,
      identifier: record?.identifier || member.evidenceId,
      available: record !== undefined,
    })
    source.count = source.items.length
    grouped.set(member.sourceKey, source)
  }
  return [...grouped.values()].map(source => {
    const identifierCounts = new Map<string, number>()
    for (const item of source.items) identifierCounts.set(item.identifier, (identifierCounts.get(item.identifier) ?? 0) + 1)
    const identifierSequence = new Map<string, number>()
    return {
      ...source,
      count: source.items.length,
      items: source.items.map(item => {
        if ((identifierCounts.get(item.identifier) ?? 0) < 2) return item
        const sequence = (identifierSequence.get(item.identifier) ?? 0) + 1
        identifierSequence.set(item.identifier, sequence)
        return { ...item, identifier: `${item.identifier} (${sequence})` }
      }),
    }
  })
}
