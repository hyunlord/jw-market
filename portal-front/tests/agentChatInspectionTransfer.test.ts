import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const agentChat = readFileSync(new URL('../src/components/main/AgentChat.tsx', import.meta.url), 'utf8')
const marketChat = readFileSync(new URL('../src/pages/MarketChatPage.tsx', import.meta.url), 'utf8')

test('side chat carries the parsed inspection detail into the full-screen snapshot', () => {
  assert.match(agentChat, /inspectionDetail\?: AnswerInspectionDetail/)
  assert.match(agentChat, /inspectionDetail: result\.inspectionDetail/)
  assert.match(agentChat, /inspectionDetail: m\.inspectionDetail/)
})

test('full-screen navigation restores the exact optional inspection detail without inventing one', () => {
  assert.match(marketChat, /inspectionDetail\?: AnswerInspectionDetail/)
  assert.match(marketChat, /inspectionDetail: msg\.inspectionDetail/)
  assert.doesNotMatch(marketChat, /inspectionDetail:\s*msg\.inspectionDetail\s*\?\?\s*\{\}/)
})

test('side chat consumes structured sections and tables from the live stream', () => {
  assert.match(agentChat, /sections\?: AnswerSectionState\[\]/)
  assert.match(agentChat, /tables\?: MarketTable\[\]/)
  assert.match(agentChat, /onSections: sections =>/)
  assert.match(agentChat, /onTables: tables =>/)
  assert.match(agentChat, /sections: result\.sections/)
  assert.match(agentChat, /tables: result\.tables/)
  assert.match(agentChat, /sections=\{msg\.sections\}/)
  assert.match(agentChat, /tables=\{msg\.tables\}/)
})

test('full-screen transition preserves structured answer state and expanded reasoning', () => {
  assert.match(agentChat, /sections: m\.sections/)
  assert.match(agentChat, /tables: m\.tables/)
  assert.match(agentChat, /reasoningInitiallyExpanded: Boolean\(m\.reasoningSteps\?\.length\)/)
  assert.match(marketChat, /sections: msg\.sections/)
  assert.match(marketChat, /tables: msg\.tables/)
  assert.match(marketChat, /reasoningInitiallyExpanded: msg\.reasoningInitiallyExpanded/)
})
