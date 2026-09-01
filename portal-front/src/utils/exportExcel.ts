// 차트 데이터 → 엑셀(.xlsx) 다운로드 공용 헬퍼 (exceljs, MIT)
// 각 차트의 "엑셀 다운로드" 버튼이 호출. 시트 스펙(컬럼+행+메타)을 받아 서식 적용 후 브라우저 다운로드.
// 값은 원본값(원/Rx 등) 그대로 담는 정책 — 환산(억/만)은 안 함.
import ExcelJS from 'exceljs'

export type CellValue = string | number | null

// 소수 1자리 반올림 (null/undefined는 빈 칸 유지) — 엑셀 % 값 공용
export const round1 = (v: number | null | undefined): number | null =>
  v == null || !Number.isFinite(v) ? null : Math.round(v * 10) / 10

export interface ExcelColumn {
  header: string
  key: string
  width?: number
  numFmt?: string
}

export interface ExcelSheet {
  name: string
  columns: ExcelColumn[]
  rows: Record<string, CellValue>[]
  meta?: string[]                                       // 표 위에 들어갈 메타 줄 (브랜드/출처/기준 등)
  highlightRow?: (row: Record<string, CellValue>) => boolean   // 강조할 행 (예: 자사 브랜드)
}

const HEADER_FILL = 'FF4472C4'   // 헤더 배경 (파랑)
const HIGHLIGHT_FILL = 'FFFFF2CC' // 강조 행 배경 (연노랑)
const THIN_BORDER = {
  top: { style: 'thin' as const, color: { argb: 'FFD9D9D9' } },
  left: { style: 'thin' as const, color: { argb: 'FFD9D9D9' } },
  bottom: { style: 'thin' as const, color: { argb: 'FFD9D9D9' } },
  right: { style: 'thin' as const, color: { argb: 'FFD9D9D9' } },
}

// 워크북 생성 → 서식 적용 → Blob 다운로드. 시트 여러 개 지원(토글별 시트 분리).
export async function downloadExcel(fileName: string, sheets: ExcelSheet[]): Promise<void> {
  const wb = new ExcelJS.Workbook()
  for (const sheet of sheets) {
    const ws = wb.addWorksheet(sheet.name)
    let r = 1

    // 메타 줄 (표 위 안내) — A열에 텍스트만
    if (sheet.meta && sheet.meta.length > 0) {
      for (const line of sheet.meta) {
        const cell = ws.getCell(r, 1)
        cell.value = line
        cell.font = { italic: true, color: { argb: 'FF666666' } }
        r++
      }
      r++ // 표와 한 줄 띄움
    }

    // 헤더 행
    const headerRow = r
    sheet.columns.forEach((col, ci) => {
      const cell = ws.getCell(headerRow, ci + 1)
      cell.value = col.header
      cell.font = { bold: true, color: { argb: 'FFFFFFFF' } }
      cell.fill = { type: 'pattern', pattern: 'solid', fgColor: { argb: HEADER_FILL } }
      cell.alignment = { vertical: 'middle', horizontal: 'center' }
      cell.border = THIN_BORDER
      ws.getColumn(ci + 1).width = col.width ?? 16
    })
    r++

    // 데이터 행
    for (const row of sheet.rows) {
      const highlight = sheet.highlightRow?.(row) ?? false
      sheet.columns.forEach((col, ci) => {
        const cell = ws.getCell(r, ci + 1)
        const v = row[col.key]
        cell.value = v ?? null   // null/undefined → 빈 칸
        if (col.numFmt && typeof v === 'number') cell.numFmt = col.numFmt
        cell.border = THIN_BORDER
        if (highlight) cell.fill = { type: 'pattern', pattern: 'solid', fgColor: { argb: HIGHLIGHT_FILL } }
      })
      r++
    }

    // 헤더 행까지 고정(스크롤 시 헤더 유지)
    ws.views = [{ state: 'frozen', ySplit: headerRow }]
  }

  const buffer = await wb.xlsx.writeBuffer()
  const blob = new Blob([buffer], {
    type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = fileName.endsWith('.xlsx') ? fileName : `${fileName}.xlsx`
  document.body.appendChild(a)
  a.click()
  a.remove()
  URL.revokeObjectURL(url)
}
