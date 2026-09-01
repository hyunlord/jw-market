import { useState } from 'react'
import type { DimensionHierarchy } from '../../types/market'
import {
  childrenForParent,
  groupSelectedChildrenByParent,
  navigateHierarchyParent,
} from '../../utils/moleculeStrengthHierarchy'

interface MoleculeStrengthDrilldownProps {
  hierarchy: DimensionHierarchy
  selectedChildKeys: string[]
  search: string
  onSelectionChange: (keys: string[]) => void
}

export default function MoleculeStrengthDrilldown({
  hierarchy,
  selectedChildKeys,
  search,
  onSelectionChange,
}: MoleculeStrengthDrilldownProps) {
  const [activeParentKey, setActiveParentKey] = useState(hierarchy.parents[0]?.key ?? '')
  const normalizedSearch = search.trim().toLocaleLowerCase()
  const selected = new Set(selectedChildKeys)
  const matchingParents = hierarchy.parents.filter(parent => {
    if (!normalizedSearch) return true
    return parent.value.toLocaleLowerCase().includes(normalizedSearch)
      || childrenForParent(hierarchy, parent.key)
        .some(child => child.value.toLocaleLowerCase().includes(normalizedSearch))
  })
  const effectiveParentKey = hierarchy.parents.some(parent => parent.key === activeParentKey)
    ? activeParentKey
    : (hierarchy.parents[0]?.key ?? '')
  const activeChildren = childrenForParent(hierarchy, effectiveParentKey)
  const selectedGroups = groupSelectedChildrenByParent(hierarchy, selectedChildKeys)

  const activateParent = (parentKey: string) => {
    const next = navigateHierarchyParent(
      { activeParentKey: effectiveParentKey, selectedChildKeys },
      parentKey,
    )
    setActiveParentKey(next.activeParentKey)
  }

  const toggleChild = (childKey: string, checked: boolean) => {
    const next = new Set(selected)
    if (checked) next.add(childKey)
    else next.delete(childKey)
    onSelectionChange([...next])
  }

  const selectCurrentParent = () => {
    onSelectionChange([
      ...new Set([
        ...selectedChildKeys,
        ...childrenForParent(hierarchy, effectiveParentKey).map(child => child.key),
      ]),
    ])
  }

  return (
    <div className="molecule-strength-drilldown">
      {selectedGroups.length > 0 && (
        <div className="molecule-strength-selected" aria-label="선택된 성분용량">
          {selectedGroups.map(group => (
            <div key={group.parent.key} className="molecule-strength-selected__group">
              <span className="molecule-strength-selected__parent">{group.parent.value}</span>
              <div className="molecule-strength-selected__chips">
                {group.children.map(child => (
                  <span key={`${group.parent.key}:${child.key}`} className="molecule-strength-chip">
                    <span>{child.value}</span>
                    <button
                      type="button"
                      aria-label={`${child.value} 선택 해제`}
                      onClick={() => toggleChild(child.key, false)}
                    >
                      ×
                    </button>
                  </span>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}

      <div className="molecule-strength-browser">
        <div className="molecule-strength-parents" role="listbox" aria-label="성분">
          {matchingParents.map(parent => (
            <button
              key={parent.key}
              type="button"
              role="option"
              aria-selected={effectiveParentKey === parent.key}
              className={effectiveParentKey === parent.key ? 'active' : ''}
              onClick={() => activateParent(parent.key)}
            >
              <span>{parent.value}</span>
              <span>{childrenForParent(hierarchy, parent.key).length}</span>
            </button>
          ))}
        </div>

        <div className="molecule-strength-children">
          <div className="molecule-strength-children__toolbar">
            <span>성분용량</span>
            <button
              type="button"
              onClick={selectCurrentParent}
              disabled={childrenForParent(hierarchy, effectiveParentKey).length === 0}
            >
              현재 성분의 성분용량 전체 선택
            </button>
          </div>
          {activeChildren.length === 0 ? (
            <p className="molecule-strength-empty">등록된 성분용량이 없습니다.</p>
          ) : activeChildren.map(child => (
            <div key={child.key} className="check-item">
              <label className="custom-checkbox">
                <input
                  type="checkbox"
                  checked={selected.has(child.key)}
                  onChange={event => toggleChild(child.key, event.target.checked)}
                />
                <span className="checkmark" />
                <span className="check-item-label" title={child.value}>{child.value}</span>
              </label>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}
