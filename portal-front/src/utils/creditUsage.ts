// 프로필 팝업 "총 사용/잔여 토큰" — GET /api/v1/rnd/admin/credit (BACK_API §1-0, 로그인 사용자 본인)
//    base_used_amount=총 사용, base_remaining_amount=잔여. 노출 안 되면 null → '-'.
import { useEffect, useState } from 'react'
import { apiFetch } from './apiFetch'

interface CreditResponse {
  status?: string
  result?: {
    base_used_amount?: number | null       
    base_remaining_amount?: number | null  
    additional_remaining_amount?: number | null
  }
}

export interface CreditUsage {
  used: number | null
  remaining: number | null
}

export function formatCredit(v: number | null): string {
  return v != null ? v.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : '-'
}

export async function fetchCreditUsed(): Promise<CreditUsage> {
  try {
    const res = await apiFetch('/api/v1/rnd/admin/credit')  // portalToken만으로 동작(@AccessUser)
    const json = (await res.json()) as CreditResponse
    if (json.status !== 'SUCCESS' || !json.result) return { used: null, remaining: null }
    const num = (v: unknown): number | null => (typeof v === 'number' ? v : null)
    return { used: num(json.result.base_used_amount), remaining: num(json.result.base_remaining_amount) }
  } catch {
    return { used: null, remaining: null }
  }
}

export function useCreditUsed(open: boolean): CreditUsage {
  const [usage, setUsage] = useState<CreditUsage>({ used: null, remaining: null })
  useEffect(() => {
    if (!open) return
    let alive = true
    fetchCreditUsed().then(v => { if (alive) setUsage(v) })
    return () => { alive = false }
  }, [open])
  return usage
}
