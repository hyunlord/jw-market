export type BrandCagrDisplay = Readonly<{
  label: 'CAGR (5Y)' | 'CAGR (3Y)' | 'CAGR (산출 이력 부족)'
  value: number | null
}>

export function selectBrandCagr(
  brandCagr5Y: number | null | undefined,
  brandCagr3Y: number | null | undefined,
): BrandCagrDisplay {
  if (brandCagr5Y != null) return { label: 'CAGR (5Y)', value: brandCagr5Y }
  if (brandCagr3Y != null) return { label: 'CAGR (3Y)', value: brandCagr3Y }
  return { label: 'CAGR (산출 이력 부족)', value: null }
}
