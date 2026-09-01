import { useState, useRef, useEffect } from 'react'
import { createPortal } from 'react-dom'

export interface SelectBoxOption {
  value: string
  label: string
  disabled?: boolean   // 옵션 개별 비활성 (선택 불가 + 흐리게)
  title?: string       // 비활성 사유 등 hover 툴팁
}

interface SelectBoxBaseProps {
  options: SelectBoxOption[]
  /** 추가 래퍼 클래스 (퍼블 위치 클래스 등) */
  wrapperClassName?: string
  /** 높이 타입 — default: 현재(버튼 38~48 / 항목 52), sm: 40px */
  size?: 'default' | 'sm'
  /** 폰트 굵기 — 400 / 500 */
  weight?: 400 | 500
  /** true면 클릭 무시 + 회색 + not-allowed 커서 */
  disabled?: boolean
}

interface SelectBoxSingleProps extends SelectBoxBaseProps {
  multiple?: false
  value: string
  onChange: (value: string) => void
  values?: never
  onChangeValues?: never
}

interface SelectBoxMultiProps extends SelectBoxBaseProps {
  /** 멀티 선택(체크박스) — ATC CODE 등 */
  multiple: true
  values: string[]
  onChangeValues: (values: string[]) => void
  value?: never
  onChange?: never
  /** 상단 "전체" 옵션 (기본 true) */
  showSelectAll?: boolean
}

type SelectBoxProps = SelectBoxSingleProps | SelectBoxMultiProps

interface MenuPos {
  top: number
  left: number
  minWidth: number
}

function multiTriggerLabel(selected: string[], allValues: string[], showSelectAll: boolean): string {
  // options에 없는 stale 값 제외 (cascade 후 "C 외 N개" 오표기 방지)
  const valid = selected.filter(v => allValues.includes(v))
  if (valid.length === 0) return '선택 안됨'
  if (showSelectAll && allValues.length > 0 && valid.length === allValues.length) return '전체'
  if (valid.length === 1) return valid[0]!
  return `${valid[0]} 외 ${valid.length - 1}개`
}

/**
 * 디자인시스템 SelectBox
 * 드롭다운은 body 포탈 + fixed — sticky 탭/ATC 바에 가려지거나 클릭 막히는 문제 방지.
 * multiple=true 시 체크박스 멀티셀렉트 (ui-state-active 미사용).
 */
export default function SelectBox(props: SelectBoxProps) {
  const {
    options,
    wrapperClassName,
    size = 'default',
    weight = 500,
    disabled = false,
  } = props
  const multiple = props.multiple === true
  const [open, setOpen] = useState(false)
  const [menuPos, setMenuPos] = useState<MenuPos | null>(null)
  const triggerRef = useRef<HTMLDivElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)

  const allValues = options.map(o => o.value)
  const selectedValues = multiple ? props.values : []
  const allChecked = multiple && allValues.length > 0 && allValues.every(v => selectedValues.includes(v))
  const showSelectAll = multiple && (props.showSelectAll !== false)

  const selectedLabel = multiple
    ? multiTriggerLabel(selectedValues, allValues, showSelectAll)
    : (options.find(o => o.value === props.value)?.label ?? '')

  const openMenu = () => {
    const el = triggerRef.current
    if (!el) return
    const r = el.getBoundingClientRect()
    setMenuPos({ top: r.bottom + 4, left: r.left, minWidth: r.width })
    setOpen(true)
  }
  const closeMenu = () => { setOpen(false); setMenuPos(null) }

  useEffect(() => {
    if (!open) return
    const onScroll = (e: Event) => {
      if (menuRef.current?.contains(e.target as Node)) return
      closeMenu()
    }
    const onResize = () => closeMenu()
    // capture — 내부 스크롤 컨테이너(scrollRef 등)의 scroll도 잡음
    window.addEventListener('scroll', onScroll, true)
    window.addEventListener('resize', onResize)
    return () => {
      window.removeEventListener('scroll', onScroll, true)
      window.removeEventListener('resize', onResize)
    }
  }, [open])

  useEffect(() => {
    if (!open) return
    const handleClickOutside = (e: MouseEvent) => {
      const t = e.target as Node
      if (triggerRef.current?.contains(t)) return
      if (menuRef.current?.contains(t)) return
      closeMenu()
    }
    document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [open])

  const dsClass = [
    'ds-select',
    `ds-select--w${weight}`,
    size === 'sm' ? 'ds-select--sm' : '',
    multiple ? 'ds-select--multi' : '',
  ].filter(Boolean).join(' ')

  const toggleAll = () => {
    if (!multiple) return
    props.onChangeValues(allChecked ? [] : [...allValues])
  }

  const toggleOne = (value: string) => {
    if (!multiple) return
    const set = new Set(selectedValues)
    if (set.has(value)) set.delete(value)
    else set.add(value)
    props.onChangeValues([...set])
  }

  const menu = open && !disabled && menuPos && createPortal(
    <div
      ref={menuRef}
      className={`ui-selectmenu-menu ui-front ds-select-menu${multiple ? ' ds-select-menu--multi' : ''}`}
      style={{
        position: 'fixed',
        top: menuPos.top,
        // 글로벌 .ui-selectmenu-menu { left:50% !important } 우회
        ['--ds-menu-left' as string]: `${menuPos.left}px`,
        minWidth: menuPos.minWidth,
        width: 'max-content',
        zIndex: 10000,
      }}
    >
      <ul className="ui-menu ui-widget ui-widget-content" style={{ maxHeight: 400, overflowY: 'auto', overflowX: 'hidden' }}>
        {multiple ? (
          <>
            {showSelectAll && (
              <li className="ui-menu-item">
                <div
                  className="ui-menu-item-wrapper"
                  onMouseDown={e => e.preventDefault()}
                  onClick={() => toggleAll()}
                >
                  <span className={`ds-check${allChecked ? ' is-checked' : ''}`} />
                  전체
                </div>
              </li>
            )}
            {options.map(opt => {
              const checked = selectedValues.includes(opt.value)
              return (
                <li key={opt.value} className="ui-menu-item">
                  <div
                    className="ui-menu-item-wrapper"
                    onMouseDown={e => e.preventDefault()}
                    onClick={() => toggleOne(opt.value)}
                  >
                    <span className={`ds-check${checked ? ' is-checked' : ''}`} />
                    {opt.label}
                  </div>
                </li>
              )
            })}
          </>
        ) : (
          options.map(opt => (
            <li key={opt.value} className="ui-menu-item">
              <div
                className={`ui-menu-item-wrapper${opt.value === props.value ? ' ui-state-active' : ''}${opt.disabled ? ' ui-state-disabled' : ''}`}
                title={opt.title}
                style={opt.disabled ? { opacity: 0.4, cursor: 'not-allowed' } : undefined}
                onClick={() => { if (opt.disabled) return; props.onChange(opt.value); closeMenu() }}
              >
                {opt.label}
              </div>
            </li>
          ))
        )}
      </ul>
    </div>,
    document.body,
  )

  return (
    <div
      ref={triggerRef}
      className={`${dsClass}${wrapperClassName ? ` ${wrapperClassName}` : ''}`}
      style={{ position: 'relative', display: 'inline-block' }}
    >
      <span
        className={`ui-selectmenu-button ui-button ui-widget ${open ? 'ui-selectmenu-button-open' : 'ui-selectmenu-button-closed'}`}
        style={{ cursor: disabled ? 'default' : 'pointer' }}
        onClick={() => {
          if (disabled) return
          if (open) closeMenu()
          else openMenu()
        }}
      >
        <span className="ui-selectmenu-text">{selectedLabel}</span>
        {!disabled && <span className="ui-selectmenu-icon ui-icon" />}
      </span>
      {menu}
    </div>
  )
}
