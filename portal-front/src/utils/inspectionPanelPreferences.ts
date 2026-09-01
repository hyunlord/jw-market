export const INSPECTION_PANEL_WIDTH_STORAGE_KEY = 'jw-market-answer-inspection-width-v1'
export const DEFAULT_INSPECTION_PANEL_WIDTH = 620
export const MIN_INSPECTION_PANEL_WIDTH = 360
export const MAX_INSPECTION_PANEL_WIDTH = 960

interface StorageReader {
  getItem(key: string): string | null
}

export function clampInspectionPanelWidth(value: number): number {
  return Math.min(MAX_INSPECTION_PANEL_WIDTH, Math.max(MIN_INSPECTION_PANEL_WIDTH, Math.round(value)))
}

export function readInspectionPanelWidth(storage: StorageReader): number {
  const parsed = Number(storage.getItem(INSPECTION_PANEL_WIDTH_STORAGE_KEY))
  return Number.isFinite(parsed) ? clampInspectionPanelWidth(parsed) : DEFAULT_INSPECTION_PANEL_WIDTH
}
