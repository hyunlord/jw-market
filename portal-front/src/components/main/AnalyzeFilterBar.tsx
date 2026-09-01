import { useEffect, useRef, useState } from 'react'
import type {
  AssayMode,
  AtcOptionsTree,
  DimensionHierarchy,
  DynamicFilterContext,
  FilterDimension,
} from '../../types/market'
import SelectBox from '../ui/SelectBox'
import MarketFilterPanel from './MarketFilterPanel'
import {
  cascadeAtcOptions,
  dimensionValues,
  displayDimensions,
  emptyAnalysisLevel,
  fullAnalysisLevel,
  reconcileAnalysisLevel,
  fetchBrandDefaultScope,
  fetchFilterOptions,
  fetchFullAtcTree,
  mergeFilterOptions,
} from '../../utils/dynamicMarket'
import {
  atcLevelsFromCanonicalAtc4,
  isSameAtc4Scope,
  resolveOptionLoadAtcSelection,
  type AtcLevelSelection,
} from '../../utils/analyzeFilterSelection'
import {
  expandAtcCodesToLeaves,
  transitionAtcHierarchySelection,
  type AtcHierarchySelectionHistory,
} from '../../utils/atcHierarchySelection'

export type { AtcLevelSelection } from '../../utils/analyzeFilterSelection'

const EMPTY_ATC: AtcLevelSelection = { atc1: [], atc2: [], atc3: [], atc4: [] }

interface AppliedTag {
  id: string
  text: string
}

interface AnalyzeFilterBarProps {
  productName: string
  source: 'UBIST' | 'IQVIA'
  measure: string
  assayMode: AssayMode
  fallbackAtc4: string[]
  applied: DynamicFilterContext
  onApply: (next: DynamicFilterContext, atcLevels: AtcLevelSelection) => void
  onReset: () => void
}

function toOpts(items: { key: string; label?: string; value?: string }[]) {
  return items.map(i => ({ value: i.key, label: i.label ?? i.value ?? i.key }))
}

function cloneFilters(src: Record<string, string[]>): Record<string, string[]> {
  return Object.fromEntries(Object.entries(src).map(([k, v]) => [k, [...v]]))
}

/** PDF 10p 1-5 — ATC 최종값 + Filters 선택값 pill 텍스트 */
function buildAppliedTags(
  assayMode: AssayMode,
  atc: AtcLevelSelection,
  filters: Record<string, string[]>,
  dimensions: FilterDimension[],
  source: 'UBIST' | 'IQVIA',
  atc4Fallback: string[],
): AppliedTag[] {
  const tags: AppliedTag[] = []

  const atcVals = atc.atc4.length > 0
    ? atc.atc4
    : assayMode === 'jw'
      ? atc4Fallback
      : (atc.atc3.length > 0 ? atc.atc3 : atc.atc2.length > 0 ? atc.atc2 : atc.atc1)
  if (atcVals.length > 0) {
    tags.push({ id: 'atc', text: atcVals.join(', ') })
  }

  if (assayMode === 'market') {
    for (const d of displayDimensions(source, dimensions)) {
      const sel = filters[d.key] ?? []
      if (sel.length === 0) continue
      const options = dimensionValues(dimensions, d.key)
      const allKeys = options.map(option => option.key)
      if (allKeys.length > 0 && sel.length >= allKeys.length) {
        tags.push({ id: d.key, text: `${d.label} 전체` })
      } else {
        const labels = new Map(options.map(option => [option.key, option.value]))
        tags.push({
          id: d.key,
          text: sel.map(value => labels.get(value) ?? value).join(', '),
        })
      }
    }
  }

  return tags
}

function AtcSelect({
  options,
  values,
  onChange,
  showSelectAll = true,
}: {
  options: { value: string; label: string }[]
  values: string[]
  onChange: (next: string[]) => void
  /** Market Standard ATC 전부 false — 드롭다운「전체」숨김 */
  showSelectAll?: boolean
}) {
  return (
    <SelectBox
      multiple
      size="sm"
      weight={400}
      wrapperClassName="atc-select"
      options={options}
      values={values}
      onChangeValues={onChange}
      showSelectAll={showSelectAll}
    />
  )
}

export default function AnalyzeFilterBar({
  productName,
  source,
  measure,
  assayMode,
  fallbackAtc4,
  applied,
  onApply,
  onReset,
}: AnalyzeFilterBarProps) {
  const [fullAtcTree, setFullAtcTree] = useState<AtcOptionsTree>({})
  const [dimensions, setDimensions] = useState<FilterDimension[]>([])
  const [dimensionHierarchies, setDimensionHierarchies] = useState<DimensionHierarchy[]>([])
  const [draftAtc, setDraftAtc] = useState<AtcLevelSelection>(EMPTY_ATC)
  const atcSelectionHistoryRef = useRef<AtcHierarchySelectionHistory>({})
  const defaultAtcRef = useRef<AtcLevelSelection>(EMPTY_ATC)
  /** 팝업에서 편집 중인 필터 */
  const [draftFilters, setDraftFilters] = useState<Record<string, string[]>>({})
  /** 검색/선택적용으로 확정된 필터 — 재오픈 시 복원 */
  const [committedFilters, setCommittedFilters] = useState<Record<string, string[]>>({})
  const [filterOpen, setFilterOpen] = useState(false)
  /** 필터 옵션(atc/dimensions) 로딩 여부 — 완료 전 스켈레톤 표시 */
  const [loading, setLoading] = useState(true)
  const [filterOptionsError, setFilterOptionsError] = useState(false)
  const [filterOptionsRetry, setFilterOptionsRetry] = useState(0)
  const [bootstrapCtx, setBootstrapCtx] = useState('')
  const [debouncedAtcKey, setDebouncedAtcKey] = useState('')
  /** 스켈레톤을 이미 표시한 컨텍스트(브랜드|소스|모드) — 같은 ctx 내 중복 재로딩은 백그라운드 */
  const loadedCtxRef = useRef<string | null>(null)
  const [appliedTags, setAppliedTags] = useState<AppliedTag[]>([])
  const [tagsOpen, setTagsOpen] = useState(true)
  const filterBtnRef = useRef<HTMLDivElement>(null)
  /** 팝업 열기 직전 스냅샷 — 취소 시 복원 (PDF 1-2-d) */
  const filtersBeforeOpenRef = useRef<Record<string, string[]> | null>(null)

  const apiView = assayMode === 'jw' ? 'strategic' : 'general'
  const fallbackAtcKey = fallbackAtc4.join(',')
  const ctxKey = `${assayMode}|${source}|${productName}`
  const appliedAtcKey = applied.atc4.join(',')
  const draftAtcKey = draftAtc.atc4.join(',')

  useEffect(() => {
    if (bootstrapCtx === ctxKey) return
    let alive = true
    const bootstrap = async () => {
      const [codes, navigationTree] = await Promise.all([
        fetchBrandDefaultScope({
          view: apiView,
          source,
          measure,
          brand: productName,
        }),
        fetchFullAtcTree({
          view: apiView,
          source,
          measure,
          brand: productName,
        }),
      ])
      if (!alive) return
      if (!navigationTree) {
        setFilterOptionsError(true)
        setLoading(false)
        return
      }
      const bootstrapCodes = appliedAtcKey
        ? appliedAtcKey.split(',')
        : codes.length > 0
          ? codes
          : (fallbackAtcKey ? fallbackAtcKey.split(',') : [])
      const bootstrapLeaves = expandAtcCodesToLeaves(bootstrapCodes, navigationTree)
      const levels = atcLevelsFromCanonicalAtc4(bootstrapLeaves)
      defaultAtcRef.current = levels
      atcSelectionHistoryRef.current = {}
      setFullAtcTree(navigationTree)
      setDraftAtc(levels)
      setDebouncedAtcKey(bootstrapLeaves.join(','))
      setBootstrapCtx(ctxKey)
    }
    void bootstrap()
    return () => { alive = false }
  }, [apiView, appliedAtcKey, bootstrapCtx, ctxKey, fallbackAtcKey, filterOptionsRetry, measure, productName, source])

  useEffect(() => {
    if (bootstrapCtx !== ctxKey) return
    const timer = window.setTimeout(() => setDebouncedAtcKey(draftAtcKey), 250)
    return () => window.clearTimeout(timer)
  }, [bootstrapCtx, ctxKey, draftAtcKey])

  // draft ATC4가 안정되면 검색 버튼과 무관하게 차원 옵션을 같은 범위로 좁힌다.
  useEffect(() => {
    const ctx = `${assayMode}|${source}|${productName}`
    if (bootstrapCtx !== ctx) return
    let alive = true
    // 컨텍스트(브랜드/소스/모드)당 1회만 스켈레톤 — 같은 ctx 내 재로딩은 백그라운드
    if (loadedCtxRef.current !== ctx) setLoading(true)
    const load = async () => {
      const result = await fetchFilterOptions({
        view: apiView,
        source,
        measure,
        brand: productName,
        atc4Codes: debouncedAtcKey ? debouncedAtcKey.split(',') : [],
      })
      if (!alive) return
      if (!result.ok) {
        setFilterOptionsError(true)
        setLoading(false)
        return
      }
      const filterOpts = result.data
      setFilterOptionsError(false)
      const fallbackCodes = fallbackAtcKey ? fallbackAtcKey.split(',') : []
      const merged = mergeFilterOptions(filterOpts, fallbackCodes, fullAtcTree)
      setDimensions(merged.dimensions)
      setDimensionHierarchies(merged.dimensionHierarchies)
      // 옵션 재로드(ATC meta 늦게 옴 등) 시 stale 선택 정리 — 159/155 방지
      setCommittedFilters(prev => reconcileAnalysisLevel(
        prev,
        source,
        merged.dimensions,
        merged.dimensionHierarchies,
      ))
      setDraftFilters(prev => reconcileAnalysisLevel(
        prev,
        source,
        merged.dimensions,
        merged.dimensionHierarchies,
      ))
      const def = filterOpts?.default_selections
      const keysOf = (level: 'atc1' | 'atc2' | 'atc3' | 'atc4', fallback: string[]) => {
        const fromDef = def?.[level]
        if (fromDef && fromDef.length > 0) return [...fromDef]
        return fallback
      }
      const nextDefaultAtc: AtcLevelSelection = {
        atc1: keysOf('atc1', defaultAtcRef.current.atc1),
        atc2: keysOf('atc2', defaultAtcRef.current.atc2),
        atc3: keysOf('atc3', defaultAtcRef.current.atc3),
        atc4: keysOf('atc4', defaultAtcRef.current.atc4),
      }
      const nextAtc = resolveOptionLoadAtcSelection({
        sameContext: loadedCtxRef.current === ctx,
        defaults: nextDefaultAtc,
      })
      defaultAtcRef.current = nextDefaultAtc
      if (nextAtc) {
        atcSelectionHistoryRef.current = {}
        setDraftAtc(nextAtc)
      }
      loadedCtxRef.current = ctx
      setLoading(false)
    }
    void load()
    return () => { alive = false }
  }, [
    productName,
    source,
    measure,
    apiView,
    fallbackAtcKey,
    assayMode,
    fallbackAtc4.length,
    filterOptionsRetry,
    debouncedAtcKey,
    bootstrapCtx,
    fullAtcTree,
  ])

  // 컨텍스트 전환은 초기화하고, 같은 컨텍스트의 부모 적용값 변경은 기존 draft 하나에 동기화한다.
  const [lastSelectionSync, setLastSelectionSync] = useState({ ctxKey, appliedAtcKey })
  if (ctxKey !== lastSelectionSync.ctxKey) {
    setLastSelectionSync({ ctxKey, appliedAtcKey })
    setDraftAtc(EMPTY_ATC)
    setFullAtcTree({})
    setDraftFilters(emptyAnalysisLevel(source))
    setCommittedFilters(emptyAnalysisLevel(source))
    setDimensionHierarchies([])
    setFilterOpen(false)
    setAppliedTags([])
    setTagsOpen(true)
    setLoading(true) // 컨텍스트 전환 즉시 스켈레톤 — stale 데이터 노출 방지
  } else if (appliedAtcKey !== lastSelectionSync.appliedAtcKey) {
    setLastSelectionSync({ ctxKey, appliedAtcKey })
    if (appliedAtcKey) {
      setDraftAtc(atcLevelsFromCanonicalAtc4(appliedAtcKey.split(',')))
    }
  }

  useEffect(() => {
    atcSelectionHistoryRef.current = {}
  }, [appliedAtcKey, ctxKey])

  // ctx 전환 시 팝업 스냅샷 정리 (렌더 중 ref 갱신 금지 → effect로)
  useEffect(() => {
    filtersBeforeOpenRef.current = null
  }, [ctxKey])

  useEffect(() => {
    const onDoc = (e: MouseEvent) => {
      if (filterBtnRef.current && !filterBtnRef.current.contains(e.target as Node)) {
        // 바깥 클릭 = 취소와 동일 — 열기 전 상태로 되돌리고 닫기
        if (filtersBeforeOpenRef.current) {
          setDraftFilters(cloneFilters(filtersBeforeOpenRef.current))
        }
        setFilterOpen(false)
      }
    }
    document.addEventListener('mousedown', onDoc, true)
    return () => document.removeEventListener('mousedown', onDoc, true)
  }, [])

  useEffect(() => {
    if (!filterOpen) return
    const onScroll = (e: Event) => {
      if (filterBtnRef.current?.contains(e.target as Node)) return
      if (filtersBeforeOpenRef.current) setDraftFilters(cloneFilters(filtersBeforeOpenRef.current))
      setFilterOpen(false)
    }
    window.addEventListener('scroll', onScroll, true)
    return () => window.removeEventListener('scroll', onScroll, true)
  }, [filterOpen])

  const parents = {
    atc1: draftAtc.atc1,
    atc2: draftAtc.atc2,
    atc3: draftAtc.atc3,
  }

  const jwAtcOptions = toOpts(
    fullAtcTree.atc4 ?? fallbackAtc4.map(c => ({ key: c, label: c })),
  )

  const openFilters = () => {
    // 재오픈 시 확정(검색/선택적용) 상태로 표시 + 취소용 스냅샷
    const raw = Object.values(committedFilters).some(v => v.length > 0)
      ? committedFilters
      : fullAnalysisLevel(source, dimensions, dimensionHierarchies)
    const base = reconcileAnalysisLevel(raw, source, dimensions, dimensionHierarchies)
    filtersBeforeOpenRef.current = cloneFilters(base)
    setDraftFilters(base)
    setFilterOpen(true)
  }

  const handleDraftFilterChange = (
    nextFilters: Record<string, string[]>,
    _changedDimension: string,
  ) => {
    setDraftFilters(nextFilters)
  }

  const handleFilterApply = () => {
    const nextFilters = cloneFilters(draftFilters)
    setCommittedFilters(nextFilters)
    // Market Standard의 빈 leaf는 전체 ATC4 나열이 아니라 focus_brand_key fallback을 뜻한다.
    const atc4 = draftAtc.atc4
    setAppliedTags(buildAppliedTags(
      assayMode,
      { ...draftAtc, atc4 },
      nextFilters,
      dimensions,
      source,
      atc4,
    ))
    setTagsOpen(true)
    filtersBeforeOpenRef.current = null
    setFilterOpen(false)
  }

  const handleFilterCancel = () => {
    if (filtersBeforeOpenRef.current) {
      setDraftFilters(cloneFilters(filtersBeforeOpenRef.current))
    }
    filtersBeforeOpenRef.current = null
    setFilterOpen(false)
  }

  const handleSearch = () => {
    const atc4 = assayMode === 'jw'
      ? (draftAtc.atc4.length > 0 ? draftAtc.atc4 : jwAtcOptions.map(o => o.value))
      : draftAtc.atc4
    const atcScopeChanged = !isSameAtc4Scope(applied.atc4, atc4)
    const searchFilters = atcScopeChanged ? emptyAnalysisLevel(source) : draftFilters
    // 전체 선택 차원은 요청에서 생략(백엔드 디폴트=전체). 부분 선택만 전송.
    const level: Record<string, string[]> = {}
    for (const [k, v] of Object.entries(searchFilters)) {
      if (v.length === 0) continue
      const all = dimensionValues(dimensions, k).map(x => x.key)
      if (all.length > 0 && v.length >= all.length) continue
      level[k] = v
    }
    // 검색 = 현재 draft 확정
    setDraftFilters(cloneFilters(searchFilters))
    setCommittedFilters(cloneFilters(searchFilters))
    onApply({ assayMode, atc4, analysisLevel: level }, draftAtc)
    setAppliedTags(buildAppliedTags(
      assayMode,
      atcLevelsFromCanonicalAtc4(atc4),
      searchFilters,
      dimensions,
      source,
      atc4,
    ))
    setTagsOpen(true)
    setFilterOpen(false)
    filtersBeforeOpenRef.current = null
  }

  const handleReset = () => {
    const full = fullAnalysisLevel(source, dimensions, dimensionHierarchies)
    atcSelectionHistoryRef.current = {}
    setDraftAtc(defaultAtcRef.current) 
    setDraftFilters(full)
    setCommittedFilters(full)
    setFilterOpen(false)
    setAppliedTags([])
    setTagsOpen(true)
    filtersBeforeOpenRef.current = null
    onReset()
  }

  const setLevel = (level: keyof AtcLevelSelection, vals: string[]) => {
    const next = transitionAtcHierarchySelection(
      { selection: draftAtc, history: atcSelectionHistoryRef.current },
      fullAtcTree,
      level,
      vals,
    )
    atcSelectionHistoryRef.current = next.history
    setDraftAtc(next.selection)
  }

  const showFold = appliedTags.length > 0

  // 옵션 로딩 완료 전 — 실제 레이아웃과 동일한 자리에 shimmer 스켈레톤
  if (loading) {
    return (
      <div
        className="atc-filter-block"
        data-market-standard-atc-options="full-with-brand-defaults"
        data-market-standard-atc-leaf-fallback="focus-brand"
      >
        <div className="atc-wrap">
          <div className="s-tit">ATC CODE</div>
          {assayMode === 'jw' ? (
            <span className="chart-skel-shimmer atc-skel-select" />
          ) : (
            <>
              <span className="chart-skel-shimmer atc-skel-select" />
              <div className="in-sepa-arrow" />
              <span className="chart-skel-shimmer atc-skel-select" />
              <div className="in-sepa-arrow" />
              <span className="chart-skel-shimmer atc-skel-select" />
              <div className="in-sepa-arrow" />
              <span className="chart-skel-shimmer atc-skel-select" />
              <div className="in-sepa-line" />
              <span className="chart-skel-shimmer atc-skel-filter" />
            </>
          )}
          <span className="chart-skel-shimmer atc-skel-search" />
          <span className="chart-skel-shimmer atc-skel-reset" />
        </div>
      </div>
    )
  }

  if (filterOptionsError) {
    return (
      <div className="atc-filter-block atc-filter-error" role="alert">
        <span>필터 옵션을 불러오지 못했습니다. 새로고침 해주세요.</span>
        <button
          type="button"
          onClick={() => {
            setFilterOptionsError(false)
            setLoading(true)
            setFilterOptionsRetry(current => current + 1)
          }}
        >
          다시 시도
        </button>
      </div>
    )
  }

  return (
    <div
      className="atc-filter-block"
      data-market-standard-atc-options="full-with-brand-defaults"
      data-market-standard-atc-leaf-fallback="focus-brand"
    >
      <div className="atc-wrap">
        <div className="s-tit">ATC CODE</div>

        {assayMode === 'jw' ? (
          <AtcSelect
            options={jwAtcOptions}
            values={draftAtc.atc4}
            onChange={vals => setLevel('atc4', vals)}
          />
        ) : (
          <>
            <AtcSelect
              options={toOpts(cascadeAtcOptions(fullAtcTree, 'atc1', parents))}
              values={draftAtc.atc1}
              onChange={vals => setLevel('atc1', vals)}
              showSelectAll={false}
            />
            <div className="in-sepa-arrow" />
            <AtcSelect
              options={toOpts(cascadeAtcOptions(fullAtcTree, 'atc2', parents))}
              values={draftAtc.atc2}
              onChange={vals => setLevel('atc2', vals)}
              showSelectAll={false}
            />
            <div className="in-sepa-arrow" />
            <AtcSelect
              options={toOpts(cascadeAtcOptions(fullAtcTree, 'atc3', parents))}
              values={draftAtc.atc3}
              onChange={vals => setLevel('atc3', vals)}
              showSelectAll={false}
            />
            <div className="in-sepa-arrow" />
            <AtcSelect
              options={toOpts(cascadeAtcOptions(fullAtcTree, 'atc4', parents))}
              values={draftAtc.atc4}
              onChange={vals => setLevel('atc4', vals)}
              showSelectAll={false}
            />
            <div className="in-sepa-line" />
            <div className="btn-filter-wrap" ref={filterBtnRef}>
              <div
                className={`btn-filter-n${filterOpen ? ' active' : ''}`}
                onClick={() => {
                  if (filterOpen) handleFilterCancel()
                  else openFilters()
                }}
                onKeyDown={e => {
                  if (e.key !== 'Enter') return
                  if (filterOpen) handleFilterCancel()
                  else openFilters()
                }}
                role="button"
                tabIndex={0}
              >
                <div className="icon-filter" />
                <div className="text-filter">Filters</div>
              </div>
              <MarketFilterPanel
                open={filterOpen}
                source={source}
                dimensions={dimensions}
                dimensionHierarchies={dimensionHierarchies}
                draft={draftFilters}
                onDraftChange={handleDraftFilterChange}
                onApply={handleFilterApply}
                onCancel={handleFilterCancel}
              />
            </div>
          </>
        )}

        <button type="button" className="btn-atc-search" onClick={handleSearch}>검색</button>
        <button type="button" className="btn-atc-reset" onClick={handleReset}>새로고침</button>

        {showFold && (
          <button
            type="button"
            className={`btn-atc-fold${tagsOpen ? '' : ' is-folded'}`}
            aria-label={tagsOpen ? '필터 접기' : '필터 펼치기'}
            aria-expanded={tagsOpen}
            onClick={() => setTagsOpen(v => !v)}
          />
        )}
      </div>

      {/* PDF 10p 1-5 — 적용된 ATC·Filters pill (#F0F4F9) */}
      {showFold && (
        <div className={`atc-applied-tags-slide${tagsOpen ? ' is-open' : ''}`}>
          <div className="atc-applied-tags-inner">
            <div className="atc-applied-tags">
              {appliedTags.map(t => (
                <span key={t.id} className="atc-applied-tag">{t.text}</span>
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
