// 시장분석 원인분석·심층분석 공용 상단 네비게이션
// 프로필 상태/로그아웃/유저는 컴포넌트 내부에서 처리 — 페이지는 navHidden·onAlertMessage만 전달
import { useState, useRef, useEffect } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useAuth } from '../../context/AuthContext'
import { hasClientPagePermission } from '../../utils/pagePermission'
import { ensureMarketStatusResult } from '../../utils/dynamicMarket'
import { useCreditUsed, formatCredit } from '../../utils/creditUsage'
import { getRuntimeConfig } from '../../config/runtimeConfig'
import BrandSearchArea from './BrandSearchArea'
import { MANUAL_URL_MARKET } from './TopNavigation'

interface MarketTopNavProps {
  navHidden?: boolean   
  onAlertMessage: (msg: string) => void
}

interface BrandItem {
  brand: string
  market_id: string
  sources?: string[]   // UBIST/IQVIA — navState로 전달해 IQVIA-only 브랜드의 빈 차트 방지
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

export default function MarketTopNav({ navHidden = false, onAlertMessage }: MarketTopNavProps) {
  const navigate = useNavigate()
  const { user, logout } = useAuth()
  const [profileMenuOpen, setProfileMenuOpen] = useState(false)
  const creditUsed = useCreditUsed(profileMenuOpen)
  const profileBtnRef = useRef<HTMLDivElement>(null)
  const profileLayerRef = useRef<HTMLDivElement>(null)

  const navLeftRef = useRef<HTMLDivElement>(null)
  const [navLeftOpen, setNavLeftOpen] = useState(false)
  const [depth2Open, setDepth2Open] = useState(true)
  const [hoveredProduct, setHoveredProduct] = useState<string | null>(null)
  const depth2Timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const [brands, setBrands] = useState<BrandItem[]>(getSortBrands)

  useEffect(() => {
    if (getSortBrands().length > 0) return
    let cancelled = false
    ensureMarketStatusResult().then(() => { if (!cancelled) setBrands(getSortBrands()) })
    return () => { cancelled = true }
  }, [])

  const canMarket = hasClientPagePermission('/market')
  const canRnd = hasClientPagePermission('/rnd')

  if (navHidden && navLeftOpen) setNavLeftOpen(false)
  if (navHidden && profileMenuOpen) setProfileMenuOpen(false)

  useEffect(() => {
    if (!navLeftOpen) return
    const handler = (e: MouseEvent) => {
      if (navLeftRef.current?.contains(e.target as Node)) return
      setNavLeftOpen(false)
    }
    document.addEventListener('click', handler, true)
    return () => document.removeEventListener('click', handler, true)
  }, [navLeftOpen])

  // 프로필 메뉴 열렸을 때 바깥 클릭 시 닫기 (capture — stopPropagation 무관하게 동작)
  useEffect(() => {
    if (!profileMenuOpen) return
    const handler = (e: MouseEvent) => {
      const target = e.target as Node
      if (profileBtnRef.current?.contains(target)) return
      if (profileLayerRef.current?.contains(target)) return
      setProfileMenuOpen(false)
    }
    document.addEventListener('click', handler, true)
    return () => document.removeEventListener('click', handler, true)
  }, [profileMenuOpen])

  const openDepth2 = () => {
    if (depth2Timer.current) clearTimeout(depth2Timer.current)
    setDepth2Open(true)
  }
  const closeDepth2 = () => {
    if (depth2Timer.current) clearTimeout(depth2Timer.current)
  }

  return (
    <div className={`top-navigation${navHidden ? ' hidden' : ''}`}>
      <div
        ref={navLeftRef} 
        className={`nav-left${navLeftOpen ? ' open' : ''}`}
        onClick={() => setNavLeftOpen(p => !p)}
      >
        <div className="text-wrap">시장분석</div>
        <div className="icon-wrap" />
        <div 
          className={`nav-left-layer ${navLeftOpen ? 'open' : 'close'}`}
          onClick={e => e.stopPropagation()}
        >
          <div className="depth1-wrap">
            <ul>
              {canMarket && (
                <li>
                  <Link
                    to="/market"
                    className="btn-market on"
                    onMouseEnter={openDepth2}
                    onMouseLeave={closeDepth2}
                    onClick={() => setNavLeftOpen(false)}
                  >
                    시장분석
                  </Link>
                </li>
              )}
              {canRnd && (
                <li>
                  <Link
                    to="/rnd"
                    className="btn-rnd"
                    onClick={() => setNavLeftOpen(false)}
                  >
                    신약 R&amp;D
                  </Link>
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
            className={`depth2-wrap ${depth2Open ? 'open' : 'close'}`}
            onMouseEnter={openDepth2}
            onMouseLeave={closeDepth2}
          >
            <div className="inner-wrap">
              <div className="s-title-wrap"><a href="#" onClick={() => navigate('/market')}>자사 제품 현황</a></div>
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
      <div className="user-utility">
        <BrandSearchArea onAlertMessage={onAlertMessage} />
        <div className="utility-menu">
          <ul>
            <li className="menu-item">
              <button
                className="btn-manual"
                title="사용자 매뉴얼"
                onClick={() => window.open(MANUAL_URL_MARKET, '_blank', 'noopener,noreferrer')}
              />
            </li>
          </ul>
        </div>
        <div className="member-profile-wrap">
          <div
            ref={profileBtnRef}
            className={`btn-member-profile${profileMenuOpen ? ' active' : ''}`}
            onClick={() => setProfileMenuOpen(p => !p)}
          >
            {user?.picture && <img src={user.picture} alt="프로필사진" referrerPolicy="no-referrer" />}
          </div>
          <div ref={profileLayerRef} className={`member-profile-layer ${profileMenuOpen ? 'open' : 'close'}`}>
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
