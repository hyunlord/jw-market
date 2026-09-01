// R&D 출처 파일 열기 — POST /api/v1/rnd/admin/vectordb/download 호출 → data.file(base64) → Blob
//   - PDF: onPdfView 콜백이 있으면 오른쪽 뷰어 패널에 인라인 표시, 없으면 새 탭(window.open) 폴백
//   - pptx/docx 등 Office 파일: 브라우저가 미리보기 못 함 → 원본 파일명으로 다운로드
//
// 사용처:
//   - ChatMessageAI 출처 패널 (응답 sourceDocuments)
//   - Panel.tsx 카드별 출처 (node.data.output.sourceDocuments)
//
// doc_id 추출 규칙: file_path("/nfs-root/docs/Document-101206.pdf") → "101206" 숫자만.
//   "Document-" prefix 무조건 제거. 매칭 안 되면 null → 호출 스킵.

import { apiFetch } from './apiFetch'
import type { ChunkBbox } from './planSignal'

// 다운로드 진행 중인 doc_id 추적 — download API가 느려 같은 출처를 여러 번 클릭하면 중복 요청이 쌓이는 문제 방지.
// 진행 중인 문서를 다시 클릭하면 무시(응답 올 때까지). 완료/실패 시 해제해 재시도는 가능.
const inFlightDocIds = new Set<number>()

/** Document-XXXX 패턴에서 숫자만 추출 (확장자 무관) */
export function extractDocId(filePath: string | undefined): number | null {
  if (!filePath) return null
  const m = filePath.match(/Document-(\d+)/)
  if (!m) return null
  const n = Number(m[1])
  return Number.isFinite(n) ? n : null
}

interface DocumentResponse {
  result?: {
    code?: number
    data?: {
      file?: unknown
      // 백엔드가 원본 파일 메타를 함께 내려줌 (다운로드 파일명/타입 결정용)
      headers?: { 'Content-Disposition'?: unknown; 'Content-Type'?: unknown }
    } | null
    errMsg?: string
  }
  status?: string
}

// 확장자 → MIME. 목록에 없으면 응답 Content-Type 또는 octet-stream으로 폴백.
const MIME_BY_EXT: Record<string, string> = {
  pdf: 'application/pdf',
  ppt: 'application/vnd.ms-powerpoint',
  pptx: 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
  doc: 'application/msword',
  docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  xls: 'application/vnd.ms-excel',
  xlsx: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
}

// Content-Disposition: attachment; filename='MD Anderson_...docx' → 파일명만 추출
function parseFilename(contentDisposition: unknown): string | null {
  if (typeof contentDisposition !== 'string') return null
  // filename*=UTF-8''... / filename="..." / filename='...' / filename=... 모두 대응
  const m = contentDisposition.match(/filename\*?=(?:UTF-8'')?["']?([^"';]+)["']?/i)
  if (!m) return null
  try { return decodeURIComponent(m[1].trim()) } catch { return m[1].trim() }
}

/**
 * 출처 파일 경로 받아 열기/다운로드.
 * - PDF면 onPdfView 콜백(오른쪽 뷰어 패널)로 전달, 콜백이 없으면 새 탭 인라인 뷰로 폴백
 * - 그 외(pptx/docx 등)는 원본 파일명으로 다운로드
 * - 실패 시 onError 콜백 호출 — 비즈니스 오류는 백엔드 `result.errMsg` 그대로 노출
 *
 * ⚠️ 백엔드는 status:SUCCESS + code:1 + data:null 형태로 비즈니스 오류를 내려보냄.
 *    (예: errMsg = "[Document-48235.pdf]는 기간이 만료되거나 삭제된 파일입니다.")
 *    HTTP 200이라 fetch는 throw 안 함 → result.code/data 직접 검증 필수.
 *
 * ⚠️ onPdfView로 넘긴 Blob URL의 수명은 호출자(뷰어 패널)가 책임진다 — 뷰어를 닫을 때
 *    URL.revokeObjectURL을 호출해야 한다. (이 함수는 PDF 뷰어 경로에서 revoke하지 않음)
 */
export async function openSourcePdf(
  filePath: string | undefined,
  onError?: (msg: string) => void,
  onPdfView?: (url: string, fileName: string, bboxes?: ChunkBbox[], initialPage?: number) => void,
  pageNo?: number | null,
  bboxes?: ChunkBbox[],
): Promise<void> {
  const docId = extractDocId(filePath)
  if (docId === null) {
    onError?.('이 출처의 파일 경로에서 문서 ID를 찾을 수 없습니다.')
    return
  }
  // ★ 이미 이 문서를 다운로드 중이면 무시 — 느린 응답 중 여러 번 클릭 시 중복 요청 방지
  if (inFlightDocIds.has(docId)) return
  inFlightDocIds.add(docId)
  try {
    const res = await apiFetch('/api/v1/rnd/admin/vectordb/download', {
      method: 'POST',
      body: JSON.stringify({ doc_id: docId }),
    })
    const data = await res.json() as DocumentResponse
    // 비즈니스 실패 — code !== 0 또는 data null 또는 file 누락. errMsg 그대로 노출.
    const base64 = data.result?.data?.file
    if (data.result?.code !== 0 || typeof base64 !== 'string' || !base64) {
      const msg = data.result?.errMsg?.trim()
      onError?.(msg || '문서를 불러올 수 없습니다.')
      return
    }

    // 파일명/확장자/MIME 결정 — Content-Disposition 우선, 없으면 도큐먼트 ID로 폴백
    const respHeaders = data.result?.data?.headers
    const filename = parseFilename(respHeaders?.['Content-Disposition']) ?? `document-${docId}`
    const ext = filename.includes('.') ? filename.split('.').pop()!.toLowerCase() : ''
    const mime = MIME_BY_EXT[ext]
      ?? (typeof respHeaders?.['Content-Type'] === 'string' ? respHeaders['Content-Type'] as string : 'application/octet-stream')

    // Base64 → Uint8Array → Blob → Object URL
    const byteCharacters = atob(base64)
    const byteNumbers = new Uint8Array(byteCharacters.length)
    for (let i = 0; i < byteCharacters.length; i++) {
      byteNumbers[i] = byteCharacters.charCodeAt(i)
    }

    // ★ PDF 여부는 확장자뿐 아니라 파일 내용 시그니처(%PDF = 25 50 44 46)로도 확인 →
    //   Content-Disposition 파싱이 실패해도 PDF는 확실히 새 탭으로 열림
    const isPdf =
      ext === 'pdf'
      || mime === 'application/pdf'
      || (byteNumbers[0] === 0x25 && byteNumbers[1] === 0x50 && byteNumbers[2] === 0x44 && byteNumbers[3] === 0x46)

    // PDF면 화면 표시용으로 MIME 강제 (octet-stream으로 오면 뷰어가 안 뜨므로)
    const blob = new Blob([byteNumbers], { type: isPdf ? 'application/pdf' : mime })
    const url = URL.createObjectURL(blob)

    if (isPdf) {
      const initialPage = (typeof pageNo === 'number' && pageNo > 0) ? pageNo : undefined
      if (onPdfView) {
        // 뷰어는 pdfjs로 직접 렌더 → fragment 없는 순수 Blob URL 전달. 페이지 점프/bbox 박스는 인자로 처리
        onPdfView(url, filename, bboxes, initialPage)
      } else {
        // 폴백: 브라우저 뷰어로 새 탭 인라인 표시 (#page=N으로 해당 페이지 이동)
        window.open(url + (initialPage ? `#page=${initialPage}` : ''), '_blank')
      }
    } else {
      // pptx/docx 등은 인라인 미리보기 불가 → 원본 파일명으로 다운로드
      const a = document.createElement('a')
      a.href = url
      a.download = filename
      document.body.appendChild(a)
      a.click()
      a.remove()
      setTimeout(() => URL.revokeObjectURL(url), 10_000)
    }
  } catch {
    onError?.('문서 조회 중 오류가 발생했습니다.\n잠시 후 다시 시도해 주세요.')
  } finally {
    inFlightDocIds.delete(docId)   // 완료/실패 후 해제 → 재시도 가능
  }
}
