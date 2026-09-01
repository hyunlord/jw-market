import assert from 'node:assert/strict'
import test from 'node:test'

import {
  DEFAULT_INSPECTION_PANEL_WIDTH,
  INSPECTION_PANEL_WIDTH_STORAGE_KEY,
  clampInspectionPanelWidth,
  readInspectionPanelWidth,
} from '../src/utils/inspectionPanelPreferences.ts'

test('reads a remembered panel width from the established browser storage contract', () => {
  const storage = { getItem: (key: string) => key === INSPECTION_PANEL_WIDTH_STORAGE_KEY ? '744' : null }
  assert.equal(readInspectionPanelWidth(storage), 744)
})

test('rejects malformed persisted values and clamps resizing to the supported range', () => {
  assert.equal(readInspectionPanelWidth({ getItem: () => 'not-a-number' }), DEFAULT_INSPECTION_PANEL_WIDTH)
  assert.equal(clampInspectionPanelWidth(100), 360)
  assert.equal(clampInspectionPanelWidth(2_000), 960)
})
