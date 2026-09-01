import { useState } from 'react'
import type { DimensionHierarchy, FilterDimension } from '../../types/market'
import {
  displayDimensions,
  dimensionValues,
} from '../../utils/dynamicMarket'
import {
  findMoleculeStrengthHierarchy,
  hierarchyDimensionDefinitions,
  MOLECULE_STRENGTH_HIERARCHY_KEY,
} from '../../utils/moleculeStrengthHierarchy'
import MoleculeStrengthDrilldown from './MoleculeStrengthDrilldown'

interface MarketFilterPanelProps {
  open: boolean
  source: 'UBIST' | 'IQVIA'
  dimensions: FilterDimension[]
  dimensionHierarchies: DimensionHierarchy[]
  draft: Record<string, string[]>
  onDraftChange: (next: Record<string, string[]>, changedDimension: string) => void
  onApply: () => void
  onCancel: () => void
}

export default function MarketFilterPanel({
  open,
  source,
  dimensions,
  dimensionHierarchies,
  draft,
  onDraftChange,
  onApply,
  onCancel,
}: MarketFilterPanelProps) {
  const hierarchy = findMoleculeStrengthHierarchy(dimensionHierarchies)
  const dimDefs = hierarchyDimensionDefinitions(
    displayDimensions(source, dimensions),
    dimensionHierarchies,
  )
  const [activeDim, setActiveDim] = useState<string>(dimDefs[0]?.key ?? 'seller')
  const activeDimEff = dimDefs.some(d => d.key === activeDim) ? activeDim : (dimDefs[0]?.key ?? '')
  const [search, setSearch] = useState('')
  const [cancelConfirm, setCancelConfirm] = useState(false)
  const hierarchyActive = activeDimEff === MOLECULE_STRENGTH_HIERARCHY_KEY && hierarchy
  const selectedDimension = hierarchyActive ? hierarchy.child_dimension : activeDimEff

  // React Compiler가 자동 메모이즈 → 수동 useMemo 제거.
  //   (deps 불일치로 "Existing memoization could not be preserved" 바일아웃을 유발해 컴포넌트 최적화가 통째 스킵됐음)
  const q = search.trim().toLowerCase()
  const rawValues = hierarchyActive
    ? hierarchy.children
    : dimensionValues(dimensions, activeDimEff)
  const values = q ? rawValues.filter(v => v.value.toLowerCase().includes(q)) : rawValues
  const allVals = values.map(v => v.key)
  const selected = draft[selectedDimension] ?? []
  const selectedInList = selected.filter(v => allVals.includes(v))
  const total = allVals.length
  const allChecked = total > 0 && selectedInList.length === total

  const setDimSelected = (dim: string, keys: string[]) => {
    onDraftChange({ ...draft, [dim]: keys }, dim)
  }

  const toggleAll = (checked: boolean) => {
    const hiddenSelected = selected.filter(v => !allVals.includes(v))
    setDimSelected(activeDimEff, checked ? [...hiddenSelected, ...allVals] : hiddenSelected)
  }

  const toggleOne = (value: string, checked: boolean) => {
    const set = new Set(selected)
    if (checked) set.add(value)
    else set.delete(value)
    setDimSelected(activeDimEff, [...set])
  }

  const countForDim = (key: string) => {
    if (key === MOLECULE_STRENGTH_HIERARCHY_KEY && hierarchy) {
      const childKeys = new Set(hierarchy.children.map(child => child.key))
      const selectedChildren = (draft[hierarchy.child_dimension] ?? [])
        .filter(value => childKeys.has(value))
      return { selected: selectedChildren.length, total: hierarchy.children.length }
    }
    const vals = dimensionValues(dimensions, key)
    const valSet = new Set(vals.map(v => v.key))
    const sel = (draft[key] ?? []).filter(v => valSet.has(v))
    return { selected: sel.length, total: vals.length }
  }

  if (!open) return null

  return (
    <div className="filter-container open">
      <div className="sidebar">
        {dimDefs.map(d => {
          const c = countForDim(d.key)
          return (
            <div
              key={d.key}
              className={`menu-item${activeDimEff === d.key ? ' active' : ''}`}
              onClick={() => { setActiveDim(d.key); setSearch('') }}
              onKeyDown={e => e.key === 'Enter' && setActiveDim(d.key)}
              role="button"
              tabIndex={0}
            >
              <span className="label">{d.label}</span>
              <div className="counts">
                <span className="selected">{c.selected}</span> / {c.total}
                <span className="arrow" />
              </div>
            </div>
          )
        })}
      </div>
      <div className="main-content">
        <div className="search-box">
          <input
            type="text"
            placeholder="검색어 입력"
            value={search}
            onChange={e => setSearch(e.target.value)}
          />
          <div className="search-icon" />
        </div>
        <div className={`checkbox-list${hierarchyActive ? ' hierarchy' : ''}`}>
          {hierarchyActive ? (
            <MoleculeStrengthDrilldown
              hierarchy={hierarchy}
              selectedChildKeys={selected}
              search={search}
              onSelectionChange={keys => setDimSelected(hierarchy.child_dimension, keys)}
            />
          ) : (
            <>
              <div className="check-all">
                <label className="custom-checkbox">
                  <input type="checkbox" checked={allChecked} onChange={e => toggleAll(e.target.checked)} />
                  <span className="checkmark" />
                  <span className="check-item-label">전체</span>
                </label>
              </div>
              {values.map(v => {
                const checked = selected.includes(v.key)
                return (
                  <div key={v.key} className="check-item">
                    <label className="custom-checkbox">
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={e => toggleOne(v.key, e.target.checked)}
                      />
                      <span className="checkmark" />
                      <span className="check-item-label" title={v.value}>{v.value}</span>
                    </label>
                  </div>
                )
              })}
            </>
          )}
        </div>
        {!hierarchyActive && <div className="checkbox-fade" />}
        <div className="action-buttons">
          <button
            type="button"
            className="btn btn-reset"
            onClick={() => setCancelConfirm(true)}
          >
            취소
          </button>
          <button
            type="button"
            className="btn btn-apply"
            onClick={() => { setCancelConfirm(false); onApply() }}
          >
            선택 적용
          </button>
        </div>
      </div>

      {cancelConfirm && (
        <div className="filter-cancel-confirm">
          <div className="filter-cancel-confirm__box">
            <p className="filter-cancel-confirm__msg">
              취소하시겠습니까?<br />변경사항은 적용되지 않습니다.
            </p>
            <div className="filter-cancel-confirm__btns">
              <button
                type="button"
                className="btn btn-reset"
                onClick={() => setCancelConfirm(false)}
              >
                취소
              </button>
              <button
                type="button"
                className="btn btn-apply"
                onClick={() => { setCancelConfirm(false); onCancel() }}
              >
                확인
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
