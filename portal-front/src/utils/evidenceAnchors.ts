import type { InspectionCall, JsonValue } from './answerInspection.ts'

export const EVIDENCE_ANCHOR_PREFIX = 'inspection-evidence-'

export function evidenceAnchorId(evidenceId: string): string {
  return `${EVIDENCE_ANCHOR_PREFIX}${[...evidenceId].map(character => character.codePointAt(0)?.toString(16) ?? '').join('-')}`
}

export function evidenceIdFromAnchor(anchor: string): string | undefined {
  if (!anchor.startsWith(EVIDENCE_ANCHOR_PREFIX)) return undefined
  const encoded = anchor.slice(EVIDENCE_ANCHOR_PREFIX.length)
  if (!encoded) return undefined
  try {
    return encoded.split('-').map(part => String.fromCodePoint(Number.parseInt(part, 16))).join('')
  } catch {
    return undefined
  }
}

export function jsonEvidenceId(value: JsonValue | undefined): string | undefined {
  if (value === null || value === undefined || Array.isArray(value) || typeof value !== 'object') return undefined
  const candidate = value.evidence_id ?? value.record_id ?? value.chunk_id
  return typeof candidate === 'string' && candidate.trim() ? candidate : undefined
}

export function inspectionCallEvidenceId(call: InspectionCall): string {
  return call.evidence_id ?? `inspection:call:${call.trace_sequence ?? call.sequence}`
}

export function inspectionCallContainsEvidence(call: InspectionCall, evidenceId: string): boolean {
  if (inspectionCallEvidenceId(call) === evidenceId) return true
  const visit = (value: JsonValue | undefined): boolean => {
    if (value === undefined || value === null) return false
    if (Array.isArray(value)) return value.some(visit)
    if (typeof value !== 'object') return false
    if (jsonEvidenceId(value) === evidenceId) return true
    return Object.values(value).some(visit)
  }
  return visit(call.request_parameters) || visit(call.output)
}
