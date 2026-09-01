import type { JsonValue } from '../../utils/answerInspection'
import {
  displayParameterLabel,
  patentJsonEntries,
  sortedJsonEntries,
} from '../../utils/portalDisplayLabels'
import { projectTracePayload } from '../../utils/traceToolResults'
import { StructuredValueTree } from './StructuredValueTree'

export const LONG_TRACE_VALUE_THRESHOLD = 160

export default function TracePayloadView({ source, payload }: { source: string; payload: JsonValue }) {
  const projected = projectTracePayload(source, payload)
  return (
    <div className="trace-output-view">
      <StructuredValueTree
        value={projected.value}
        labelFor={key => ({ primary: displayParameterLabel(key) })}
        entriesFor={source === 'patent' ? patentJsonEntries : sortedJsonEntries}
        showEveryArrayItem={source === 'patent'}
        recordSource={source}
      />
      {projected.hiddenFieldCount > 0 && <p className="trace-output-hidden">기타 {projected.hiddenFieldCount}개 필드</p>}
    </div>
  )
}
