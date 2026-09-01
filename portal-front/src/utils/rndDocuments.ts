import { apiFetch } from './apiFetch'
import { isRndOk } from './rndApi'


export interface RndDocument {
  id: number
  fileName: string
  regDate: string   // "YYYY-MM-DDTHH:mm:ss"
  chunkCnt: number
  status: string   
}

interface RawDoc {
  id?: number
  file_name?: string
  org_file_name?: string
  reg_date?: string
  chunk_cnt?: number
  status?: string
}

interface DocListResponse {
  result?: { data?: { list?: unknown; total_count?: number } }
}

const FORM_HEADERS = { 'Content-Type': 'application/x-www-form-urlencoded' }

function normalize(d: RawDoc): RndDocument {
  return {
    id: typeof d.id === 'number' ? d.id : -1,
    fileName: (d.file_name ?? d.org_file_name ?? '').trim(),
    regDate: d.reg_date ?? '',
    chunkCnt: typeof d.chunk_cnt === 'number' ? d.chunk_cnt : 0,
    status: d.status ?? '',
  }
}

async function fetchAllDocs(endpoint: string): Promise<RndDocument[]> {
  const PAGE_SIZE = 1000
  const all: RndDocument[] = []
  let pg = 1
  let total = Infinity

  while (all.length < total && pg <= 50) {
    const res = await apiFetch(endpoint, {
      method: 'POST',
      headers: FORM_HEADERS,
      body: new URLSearchParams({ pg: String(pg), pgSize: String(PAGE_SIZE) }),
    })
    const data = await res.json() as DocListResponse
    if (!isRndOk(data)) throw new Error('문서 목록 조회 실패')

    const d = data.result?.data
    const list = Array.isArray(d?.list) ? (d?.list as RawDoc[]) : []
    total = typeof d?.total_count === 'number' ? d.total_count : all.length + list.length
    all.push(...list.map(normalize))

    if (list.length < PAGE_SIZE) break   // 마지막 페이지
    pg++
  }
  return all
}

export function fetchRndInternalDocs(): Promise<RndDocument[]> {
  return fetchAllDocs('/api/v1/rnd/admin/vector/document/list/in')
}

export function fetchRndThesisDocs(): Promise<RndDocument[]> {
  return fetchAllDocs('/api/v1/rnd/admin/vector/document/list/thesis')
}

// 상태 코드 라벨 — JS0003=완료(백엔드 확인). 나머지는 코드 미문서화라 raw 노출.
export function docStatusLabel(status: string): string {
  return status === 'JS0003' ? '완료' : (status || '-')
}
