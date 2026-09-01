import { useState } from 'react'
import { createRoot } from 'react-dom/client'

import ChatMessageAI from '../../src/components/main/ChatMessageAI'
import AnswerInspectionPanel from '../../src/components/main/AnswerInspectionPanel'
import type { AnswerSectionState } from '../../src/utils/answerSections'
import type { AnswerInspectionDetail } from '../../src/utils/answerInspection'
import type { MarketTable } from '../../src/utils/marketTables'
import '../../src/styles/reset.css'
import '../../src/styles/common.css'
import './answer-sections.css'

const metadata: AnswerSectionState[] = [
  { id: 'insight', order: 0, kind: 'insight', status: 'pending', parts: [] },
  { id: 'facts', order: 1, kind: 'facts', title: '조사 결과', status: 'pending', parts: [] },
]

const factsFirst: AnswerSectionState[] = [
  metadata[0]!,
  { ...metadata[1]!, status: 'complete', parts: [{ type: 'text', text: '리바로는 선택 시장에서 9.1%의 점유율을 기록했습니다.' }] },
]

const complete: AnswerSectionState[] = [
  {
    ...metadata[0]!, status: 'complete', parts: [
      { type: 'text', text: '점유율이 최근 관측에서 상승해 성장 흐름이 확인됩니다. ' },
      { type: 'evidence', evidenceId: 'document:chunk:1', label: '출처' },
    ],
  },
  factsFirst[1]!,
]

const table: MarketTable = {
  table_id: 'harness-table', title: '브랜드 점유율', source_label: '내부 데이터마트',
  columns: [{ key: 'brand', label: '브랜드', type: 'string', unit: null, align: 'left' }, { key: 'share', label: 'M/S', type: 'number', unit: '%', align: 'right' }],
  rows: [{ record_id: 'mart:row:1', cells: { brand: '리바로', share: 9.1 } }],
  row_count: 1, omitted_columns: [],
}

const detail: AnswerInspectionDetail = {
  schema: 'r12.5.inspect.v1', question: '리바로 시장 흐름', expansion: null,
  calls: [{
    sequence: 1, evidence_id: 'document:call:1', tool: 'document_rag', source_label: '첨부 문서', status: '완료', elapsed_seconds: 0.4,
    request_parameters: { query: '리바로 성장 흐름' }, counts: { returned: 1, narrated: 1 }, unused_count: 0, dropped_count: 0,
    output: { chunks: [{ evidence_id: 'document:chunk:1', record_id: 'chunk-1', document_name: '시장자료.pdf', page: 7, section: '시장 요약', distance: 0.18, score_kind: 'vector', content_excerpt: '최근 관측 시점에서 리바로의 시장점유율이 상승했고 성장 기여율도 양수로 나타났습니다.', selected: true }] },
    drop_reasons: [],
  }],
}

export function Harness() {
  const [phase, setPhase] = useState(1)
  const [evidenceId, setEvidenceId] = useState<string>()
  const sections = phase === 1 ? metadata : phase === 2 ? factsFirst : complete
  return <main className="harness-shell">
    <header className="harness-toolbar">
      <strong>섹션 이벤트 하니스</strong>
      <button type="button" onClick={() => { setPhase(1); setEvidenceId(undefined) }}>1 메타</button>
      <button type="button" onClick={() => setPhase(2)}>2 하단 delta</button>
      <button type="button" onClick={() => setPhase(3)}>3 상단 delta</button>
      <span data-testid="phase">현재 {phase}단계</span>
    </header>
    <div className={`work-split${evidenceId ? ' inspection-open' : ''}`}>
      <div className="chat-col">
        <ChatMessageAI
          id="section-harness"
          planContent=""
          isGenerating={phase < 3}
          headerLabel="AI 분석 결과"
          sections={sections}
          tables={[table]}
          onInspectionOpen={() => setEvidenceId('document:call:1')}
          onEvidenceOpen={setEvidenceId}
          inspectionOpen={evidenceId !== undefined}
        />
      </div>
      <AnswerInspectionPanel
        open={evidenceId !== undefined}
        answerLabel="리바로 시장 흐름"
        detail={detail}
        initiallyExpandedSequences={[]}
        focusEvidenceId={evidenceId}
        focusRequestId={phase}
        onClose={() => setEvidenceId(undefined)}
      />
    </div>
  </main>
}

createRoot(document.getElementById('root')!).render(<Harness />)
