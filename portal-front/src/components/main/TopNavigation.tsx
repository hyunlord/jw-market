import { useState, useRef, useEffect, useLayoutEffect, useMemo } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { useAuth } from '../../context/AuthContext'
import { apiFetch } from '../../utils/apiFetch'
import { hasClientPagePermission } from '../../utils/pagePermission'
import { ensureMarketStatusResult } from '../../utils/dynamicMarket'
import { useCreditUsed, formatCredit } from '../../utils/creditUsage'
import { getRuntimeConfig } from '../../config/runtimeConfig'
import AttachmentListPopup from './AttachmentListPopup'

export const MANUAL_URL_MARKET = 'https://gemini.google.com/gem/1CBWhKZuUGEEqw3WNiEQjoqTtVHG_IkkB?usp=sharing'
const MANUAL_URL_RND = 'https://gemini.google.com/gem/1IpV3UmnVICsp3BPJFJbOlFh4-7qlFPmq?usp=sharing'

interface Props {
  isDetail?: boolean
  chatTitle?: string
  profileMenuOpen?: boolean
  utilMenuOpen?: boolean
  onMenuToggle?: (menu: 'profile' | 'util') => void
  onDeleteModal?: () => void
  onChangeNameModal?: () => void
  onPinChat?: () => void
  onUnpinChat?: () => void
  isActivePinned?: boolean
  onCloseMenus?: () => void
  onAlertMessage?: (msg: string) => void
  onDownloadReport?: () => void
  onMcpPanel?: () => void
  showAttachments?: boolean
  // 첨부파일 목록 드롭다운(btn-attach-list 아래 탑다운) 렌더용 — Market 전용
  attachAppSessionId?: string | null
  attachDocIdByName?: Map<string, number>
  section?: 'rnd' | 'market'
  navLeftLabel?: string
  showReportButton?: boolean
}

interface BrandItem {
  brand: string
  market_id: string
  sources?: string[]   // UBIST/IQVIA — navState로 전달해 IQVIA-only 브랜드의 빈 차트 방지
  general_sources?: string[]   
  strategic_sources?: string[]  
  is_jw?: boolean        
}

interface MarketStatusItem {
  brand_cards?: BrandCardData[]
}

interface BrandCardData {
  brand: string
  front: Front
  market_id: string
  sources: string[]
}

interface SalesMetrics {
  value_recent: number
  ms_recent_pct: number
  gr_mom_pct: number
  gr_qoq_pct: number
  gr_yoy_pct: number
  gr_yoy_mat_pct: number
  gr_yoy_ym_pct: number
}

interface Front extends SalesMetrics {
  ms_change_yoy_pct: number
  sources_data: { [key: string]: SalesMetrics }
  default_source: string
}

function getSession<T>(key: string): T | null {
  try {
    const raw = sessionStorage.getItem(key)
    return raw ? (JSON.parse(raw) as T) : null
  } catch {
    return null
  }
}

// 매출순으로 브랜드 정렬
function getSortBrands(): BrandItem[] {
  const data = getSession<MarketStatusItem>('marketStatusResult')
  if (!data?.brand_cards) return []

  // 기본 소스 데이터 또는 front 자체를 반환하는 헬퍼 함수
  const getMetrics = (card: BrandCardData) => {
    const { front } = card
    return front?.sources_data?.[front.default_source] ?? front;
  }

  return [...data.brand_cards]
    .sort((a, b) => (getMetrics(b)?.value_recent || 0) - (getMetrics(a)?.value_recent || 0))
    .map(({ brand, market_id, sources }) => ({ brand, market_id, sources }))
}

export default function TopNavigation({
  isDetail = false, chatTitle = '',
  profileMenuOpen, utilMenuOpen,
  onMenuToggle,
  onDeleteModal, onChangeNameModal, onPinChat, onUnpinChat,
  isActivePinned = false,
  onCloseMenus,
  onAlertMessage = () => {},
  onDownloadReport,
  onMcpPanel,
  showAttachments,
  attachAppSessionId,
  attachDocIdByName,
  section,
  navLeftLabel,
  showReportButton = true,
}: Props) {
  const { user, logout } = useAuth()
  const { pathname } = useLocation()
  const navigate = useNavigate()
  const isDashboard = pathname === '/market'
  const isMarketSection = section ? section === 'market' : isDashboard

  const [navLeftOpen, setNavLeftOpen] = useState(false)
  const [depth2Open, setDepth2Open] = useState(false)
  const [hoveredProduct, setHoveredProduct] = useState<string | null>(null)
  const depth2Timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const [brands, setBrands] = useState<BrandItem[]>(getSortBrands)

  useEffect(() => {
    if (!isMarketSection || getSortBrands().length > 0) return
    let cancelled = false
    ensureMarketStatusResult().then(() => { if (!cancelled) setBrands(getSortBrands()) })
    return () => { cancelled = true }
  }, [isMarketSection])

  // 서버 page API는 "페이지 진입 게이트"라 메뉴 노출엔 불필요 (PrivateRoute가 진입 시 1콜로 게이트)
  const canRnd = hasClientPagePermission('/rnd')
  const canMarket = hasClientPagePermission('/market')

  // 대시보드 검색 상태
  const [searchInput, setSearchInput] = useState('')
  const [suggestionsOpen, setSuggestionsOpen] = useState(false)
  const searchWrapRef = useRef<HTMLDivElement>(null)
  const [selectedIndex, setSelectedIndex] = useState<number | null>(null)
  const suggestionsRef = useRef<HTMLUListElement | null>(null)

  // 브랜드검색 실시간 필터링
  const matchingBrands = useMemo(() => {
    const query = searchInput.trim().toLowerCase();
    
    // 검색어가 비어있으면 빈 배열 반환
    if (!query) return []; 

    return brands.filter(item => {
      const normalizedBrand = item.brand.toLowerCase();
      // 입력한 검색어 전체가 브랜드명에 포함되어 있는지 확인
      return normalizedBrand.includes(query); 
    });
  }, [searchInput, brands])

  const [lastMatching, setLastMatching] = useState(matchingBrands)
  if (matchingBrands !== lastMatching) {
    setLastMatching(matchingBrands)
    setSuggestionsOpen(matchingBrands.length > 0)
    setSelectedIndex(null)
  }

  useEffect(() => {
    if (!suggestionsOpen) return
    const handler = (e: MouseEvent) => {
      if (searchWrapRef.current?.contains(e.target as Node)) return
      setSuggestionsOpen(false)
    }
    document.addEventListener('click', handler, true)
    return () => document.removeEventListener('click', handler, true)
  }, [suggestionsOpen])

  // 선택 인덱스가 변경될 때 리스트가 가려지지 않도록 스크롤
  useEffect(() => {
    if (selectedIndex === null) return
    const ul = suggestionsRef.current
    if (!ul) return
    const el = ul.children[selectedIndex] as HTMLElement | undefined
    if (el) {
      el.scrollIntoView({ block: 'nearest', inline: 'nearest' })
    }
  }, [selectedIndex, suggestionsOpen, matchingBrands])

  const handleSearchKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.nativeEvent.isComposing) return
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      if (!suggestionsOpen && matchingBrands.length > 0) {
        setSuggestionsOpen(true)
        setSelectedIndex(0)
      } else if (matchingBrands.length > 0) {
        setSelectedIndex(prev => prev === null ? 0 : Math.min(matchingBrands.length - 1, prev + 1))
      }
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      if (matchingBrands.length > 0) {
        setSelectedIndex(prev => prev === null ? matchingBrands.length - 1 : Math.max(0, prev - 1))
      }
    } else if (e.key === 'Enter') {
      if (suggestionsOpen && selectedIndex !== null && matchingBrands[selectedIndex]) {
        e.preventDefault()
        const item = matchingBrands[selectedIndex]
        navigate('/market/analyze', { state: { productName: item.brand, sources: item.sources } })
        setSuggestionsOpen(false)
        setSelectedIndex(null)
      }
    } else if (e.key === 'Escape') {
      setSuggestionsOpen(false)
      setSelectedIndex(null)
    }
  }

  // 프로필 내부 상태 (대시보드용 — onMenuToggle 없을 때)
  const [profileMenuInternal, setProfileMenuInternal] = useState(false)
  const isControlled = onMenuToggle !== undefined
  const profileEffective = isControlled ? (profileMenuOpen ?? false) : profileMenuInternal
  const utilEffective = utilMenuOpen ?? false
  const creditUsed = useCreditUsed(profileEffective)   // 프로필 팝업 열릴 때마다 "총 사용 토큰" 최신화

  // 각 팝업 영역 ref
  const navLeftRef = useRef<HTMLDivElement>(null)
  const profileBtnRef = useRef<HTMLDivElement>(null)
  const profileLayerRef = useRef<HTMLDivElement>(null)
  const utilBtnRef = useRef<HTMLButtonElement>(null)
  const utilLayerRef = useRef<HTMLDivElement>(null)

  // 첨부파일 목록 드롭다운 (btn-attach-list 아래 탑다운) — 열림 상태는 내부 관리.
  const [attachOpen, setAttachOpen] = useState(false)
  const attachBtnRef = useRef<HTMLButtonElement>(null)
  // 외부 클릭 시 닫기 (레이어 .attach-list-pop 또는 트리거 버튼 내부 클릭은 유지)
  useEffect(() => {
    if (!attachOpen) return
    const handler = (e: MouseEvent) => {
      const t = e.target as Element
      if (attachBtnRef.current?.contains(t)) return
      if (t.closest('.attach-list-pop')) return
      setAttachOpen(false)
    }
    document.addEventListener('click', handler, true)
    return () => document.removeEventListener('click', handler, true)
  }, [attachOpen])

  const closeMenusRef = useRef<() => void>(() => {})
  useLayoutEffect(() => {
    closeMenusRef.current = () => {
      if (isControlled) onCloseMenus?.()
      else setProfileMenuInternal(false)
    }
  })

  const handleProfileClick = () => {
    if (isControlled) onMenuToggle('profile')
    else setProfileMenuInternal(p => !p)
  }

  // nav-left 외부 클릭 시 닫기 (capture — stopPropagation 무관하게 동작)
  useEffect(() => {
    if (!navLeftOpen) return
    const handler = (e: MouseEvent) => {
      if (navLeftRef.current?.contains(e.target as Node)) return
      setNavLeftOpen(false)
    }
    document.addEventListener('click', handler, true)
    return () => document.removeEventListener('click', handler, true)
  }, [navLeftOpen])

  // profile / util-menu 외부 클릭 시 닫기 (capture)
  useEffect(() => {
    if (!profileEffective && !utilEffective) return
    const handler = (e: MouseEvent) => {
      const target = e.target as Element
      if (profileLayerRef.current?.contains(target)) return
      if (utilLayerRef.current?.contains(target)) return
      if (profileBtnRef.current?.contains(target)) return
      if (utilBtnRef.current?.contains(target)) return
      closeMenusRef.current()
    }
    document.addEventListener('click', handler, true)
    return () => document.removeEventListener('click', handler, true)
  }, [profileEffective, utilEffective])

  const openDepth2 = () => {
    if (depth2Timer.current) clearTimeout(depth2Timer.current)
    setDepth2Open(true)
  }
  const closeDepth2 = () => {
    depth2Timer.current = setTimeout(() => setDepth2Open(false), 100)
  }

  const handleBrandSearch = async (e: React.SyntheticEvent) => {
    e.preventDefault()
    const trimmed = searchInput.trim()
    if (!trimmed) return
    try {
      const res = await apiFetch('/api/v1/market/brands', {
        method: 'POST',
        body: JSON.stringify({ query: trimmed, marketId: '' }),
      })
      const data = await res.json() as { status: string; result: BrandItem[] }
      if (data.status === 'SUCCESS') {
        const matched = data.result.find(item => item.brand === trimmed)
        if (matched) {
          navigate('/market/analyze', { state: {
            productName: matched.brand, sources: matched.sources,
            generalSources: matched.general_sources, strategicSources: matched.strategic_sources,
            assay: matched.is_jw === false ? 'market' : 'jw',
          } })
        } else {
          onAlertMessage('입력하신 브랜드명과 일치하는\n결과가 없습니다.\n브랜드명을 다시 한 번 확인해 주세요.')
        }
      }
    } catch (err) {
      console.error('[DashboardPage] 검색 실패:', err)
    }
  }

  return (
    <div className="top-navigation">
      {/* nav-left */}
      <div
        ref={navLeftRef}
        className={`nav-left${navLeftOpen ? ' open' : ''}`}
        onClick={() => setNavLeftOpen(p => !p)}
      >
        <div className="text-wrap">{navLeftLabel ?? (isDashboard ? '시장분석' : '신약 R&D')}</div>
        <div className="icon-wrap" />
        <div
          className={`nav-left-layer ${navLeftOpen ? 'open' : 'close'}`}
          onClick={e => e.stopPropagation()}
        >
          <div className="depth1-wrap">
            <ul>
              {canMarket && (
                <li>
                  {isMarketSection ? (
                    <a
                      href="#"
                      className="btn-market on"
                      onMouseEnter={openDepth2}
                      onMouseLeave={closeDepth2}
                      onClick={e => e.preventDefault()}
                    >
                      시장분석
                    </a>
                  ) : (
                    <Link
                      to="/market"
                      className="btn-market"
                      onMouseEnter={openDepth2}
                      onMouseLeave={closeDepth2}
                      onClick={() => setNavLeftOpen(false)}
                    >
                      시장분석
                    </Link>
                  )}
                </li>
              )}
              {canRnd && (
                <li>
                  {isMarketSection ? (
                    <Link
                      to="/rnd"
                      className="btn-rnd"
                      onClick={() => setNavLeftOpen(false)}
                    >
                      신약 R&amp;D
                    </Link>
                  ) : (
                    <a
                      href="#"
                      className="btn-rnd on"
                      onClick={e => e.preventDefault()}
                      onMouseEnter={() => { if (depth2Timer.current) clearTimeout(depth2Timer.current); setDepth2Open(false) }}
                    >
                      신약 R&amp;D
                    </a>
                  )}
                </li>
              )}
              <li>
                <a
                  href={getRuntimeConfig().genosNavigationUrl}
                  className="btn-genos"
                  target="_blank"
                  rel="noopener noreferrer"
                  onClick={() => setNavLeftOpen(false)}
                >
                  GenOS
                </a>
              </li>
            </ul>
          </div>
          <div
            className={`depth2-wrap ${(isMarketSection || depth2Open) ? 'open' : 'close'}`}
            onMouseEnter={openDepth2}
            onMouseLeave={closeDepth2}
          >
            <div className="inner-wrap">
              <div className="s-title-wrap"><a href="#" onClick={e => { e.preventDefault(); navigate('/market'); setNavLeftOpen(false) }}>자사 제품 현황</a></div>
              <div className="product-type-list">
                <p className="sub-title">제품군</p>
                <ul>
                  {brands.map(b => (
                    <li
                      key={b.brand}
                      onMouseEnter={() => setHoveredProduct(b.brand)}
                      onMouseLeave={() => setHoveredProduct(null)}
                    >
                      <a
                        href="#"
                        onClick={e => e.preventDefault()}
                        className={hoveredProduct === b.brand ? 'over' : ''}
                      >
                        {b.brand}
                      </a>
                      <div className={`depth3 ${hoveredProduct === b.brand ? 'open' : ''}`}>
                        <a
                          href="#"
                          onClick={e => {
                            e.preventDefault()
                            navigate('/market/analyze', { state: { productName: b.brand, sources: b.sources } })
                            setNavLeftOpen(false)
                          }}
                        >원인분석</a>
                        <a
                          href="#"
                          onClick={e => {
                            e.preventDefault()
                            navigate('/market/deep-analyze', { state: { productName: b.brand, sources: b.sources } })
                            setNavLeftOpen(false)
                          }}
                        >심층분석</a>
                      </div>
                    </li>
                  ))}
                </ul>
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* title-center (채팅 시에만 표시) */}
      {isDetail && (
        <div className="title-center">{chatTitle}</div>
      )}

      {/* user-utility */}
      <div className="user-utility">
        <div className="utility-menu">
          <ul>
            {isDashboard && (
              <li className="menu-item">
                <div className="input-dashboard-wrap" ref={searchWrapRef}>
                  <form onSubmit={handleBrandSearch}>
                    <a href="#" className="btn-search" onClick={handleBrandSearch}>검색</a>
                    <input
                      type="text"
                      placeholder="브랜드를 입력해 주세요."
                      className="input-dashboard-search"
                      value={searchInput}
                      onChange={e => setSearchInput(e.target.value)}
                      onFocus={() => { if (matchingBrands.length > 0) setSuggestionsOpen(true) }}
                      onKeyDown={handleSearchKeyDown}
                    />
                    {matchingBrands.length > 0 && suggestionsOpen && (
                      <ul className="search-suggestions" ref={suggestionsRef}>
                        {matchingBrands.map((item, idx) => (
                          <li
                            key={item.brand}
                            className={selectedIndex === idx ? 'selected' : ''}
                            tabIndex={0}
                            onFocus={() => setSelectedIndex(idx)}
                            onMouseEnter={() => setSelectedIndex(idx)}
                            onMouseLeave={() => setSelectedIndex(null)}
                            onKeyDown={e => {
                              if (e.key === 'Enter') {
                                e.preventDefault()
                                navigate('/market/analyze', { state: { productName: item.brand, sources: item.sources } })
                                setSuggestionsOpen(false)
                                setSelectedIndex(null)
                              }
                            }}
                            onClick={e => {
                              e.preventDefault()
                              navigate('/market/analyze', { state: { productName: item.brand, sources: item.sources } })
                              setSuggestionsOpen(false)
                              setSelectedIndex(null)
                            }}
                          >
                            {item.brand}
                          </li>
                        ))}
                      </ul>
                    )}                    
                  </form>
                </div>
              </li>
            )}
            {!isDashboard && isDetail && (
              <>
              <li className="menu-item">
                {/* 순서: 보고서 요약 → 첨부파일 목록 (기획 요청) */}
                {showReportButton && (
                  <button
                    className="btn-down-report"
                    title="보고서 요약"
                    onClick={() => onDownloadReport?.()}
                  />
                )}
                {section === 'market' && showAttachments && (
                  <>
                    <button
                      ref={attachBtnRef}
                      className={`btn-attach-list${attachOpen ? ' active' : ''}`}
                      title="첨부파일 목록"
                      onClick={() => { onCloseMenus?.(); setAttachOpen(o => !o) }}
                    />
                    {attachOpen && (
                      <AttachmentListPopup
                        asDropdown
                        onClose={() => setAttachOpen(false)}
                        appSessionId={attachAppSessionId ?? null}
                        docIdByName={attachDocIdByName ?? new Map()}
                        onAlert={onAlertMessage}
                      />
                    )}
                  </>
                )}
                {section !== 'market' && (
                  <button
                    className="btn-panel"
                    title="MCP 실행 정보 전체 보기"
                    onClick={() => onMcpPanel?.()}
                  />
                )}
              </li>
              <li className="menu-item">
                <button
                  ref={utilBtnRef}
                  className="btn-util-menu"
                  title="더보기"
                  onClick={() => onMenuToggle?.('util')}
                />
                <div ref={utilLayerRef} className={`util-menu-layer ${utilEffective ? 'open' : 'close'}`}>
                  <ul>
                    {isActivePinned ? (
                      <li><a href="#" onClick={e => { e.preventDefault(); onUnpinChat?.() }}><div className="icon-wrap icon03" /><div className="text-wrap">고정 해제</div></a></li>
                    ) : (
                      <li><a href="#" onClick={e => { e.preventDefault(); onPinChat?.() }}><div className="icon-wrap icon02" /><div className="text-wrap">채팅 고정</div></a></li>
                    )}
                    <li><a href="#" onClick={e => { e.preventDefault(); onChangeNameModal?.() }}><div className="icon-wrap icon04" /><div className="text-wrap">이름 변경</div></a></li>
                    <li><a href="#" onClick={e => { e.preventDefault(); onDeleteModal?.() }}><div className="icon-wrap icon05" /><div className="text-wrap">삭제</div></a></li>
                  </ul>
                </div>
              </li>
              </>
            )}
            <li className="menu-item">
              <button
                className="btn-manual"
                title="사용자 매뉴얼"
                onClick={() => window.open(isMarketSection ? MANUAL_URL_MARKET : MANUAL_URL_RND, '_blank', 'noopener,noreferrer')}
              />
            </li>
          </ul>
        </div>

        <div className="member-profile-wrap">
          <div
            ref={profileBtnRef}
            className={`btn-member-profile${profileEffective ? ' active' : ''}`}
            onClick={handleProfileClick}
          >
            {user?.picture && <img src={user.picture} alt="프로필사진" referrerPolicy="no-referrer" />}
          </div>
          <div ref={profileLayerRef} className={`member-profile-layer ${profileEffective ? 'open' : 'close'}`}>
            <div className="top-info">
              <div className="mem-pic"><img src={user?.picture || '/images/img_profile.png'} alt="프로필사진" referrerPolicy="no-referrer" /></div>
              <div className="mem-text">
                <dl>
                  <dt><span>{user?.userName ?? ''}</span></dt>
                  <dd>{user?.userId ?? ''}</dd>
                </dl>
              </div>
            </div>
            <div className="mid-info">
              <ul>
                <li>
                  <p className="text-top">총 사용/잔여 토큰</p>
                  <p className="text-btm">{formatCredit(creditUsed.used)} / {formatCredit(creditUsed.remaining)}</p>
                </li>
                <li>
                  <p className="text-top">최근 접속일</p>
                  <p className="text-btm">{user?.lastLoginTime?.slice(0, 10) ?? '-'}</p>
                </li>
              </ul>
            </div>
            <div className="sepa-line" />
            <div className="mem-s-menu">
              <ul>
                <li><a href="#" onClick={e => { e.preventDefault(); logout() }}><div className="icon-wrap icon-logout" /><div className="text-wrap">로그아웃</div></a></li>
              </ul>
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
