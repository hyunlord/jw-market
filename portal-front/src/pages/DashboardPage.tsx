import { useState, useEffect, useRef, useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import Sidebar from '../components/main/Sidebar'
import TopNavigation from '../components/main/TopNavigation'
import SelectBox from '../components/ui/SelectBox'
import { apiFetch } from '../utils/apiFetch'
import { fetchCauseResult } from '../utils/causeStore'
import { fmtBaekman, fmtPeriodKor } from '../utils/chartHelpers'
import { selectBrandCagr } from '../utils/brandCagr'
import Modals from '../components/main/Modals'

interface SelectOption {
  value: string
  label: string
}

interface KpiItem {
  total_sales_recent_krw: number
  avg_ms_per_brand_pct: number
  sales_up_count: number
  sales_down_count: number
  avg_cagr_5y_pct: number
  period_recent?: string   // "2026-05"(UBIST) / "2026-Q1"(IQVIA) — 헤더 기준 라벨용
}

// KPI 요약 카드 — 미사용(향후 복구 대비 주석 유지)
/* function KpiSummary({ kpiData, isLoading }: { kpiData: KpiItem; isLoading: boolean }) {
  return (
    <div className="card-grid card-grid-status">
      <article className="stat-card">
        <p className="stat-card-label">총 매출</p>
        <p className="stat-card-value">{isLoading ? '' : fmtBaekman(kpiData.total_sales_recent_krw)}</p>
      </article>
      <article className="stat-card">
        <p className="stat-card-label">평균 M/S</p>
        <p className="stat-card-value">{isLoading ? '' : fmtPctNoSign(kpiData.avg_ms_per_brand_pct)}</p>
      </article>
      <article className="stat-card stat-card-up">
        <p className="stat-card-label">매출 상승</p>
        <div className="stat-card-flex">
          <p className="stat-card-value">{isLoading ? '' : kpiData.sales_up_count + '개'}</p>
          <i className="icon-up-arrow"></i>
        </div>
      </article>
      <article className="stat-card stat-card-down">
        <p className="stat-card-label">매출 하락</p>
        <div className="stat-card-flex">
          <p className="stat-card-value">{isLoading ? '' : kpiData.sales_down_count + '개'}</p>
          <i className="icon-down-arrow"></i>
        </div>
      </article>
      <article className="stat-card stat-card-up">
        <p className="stat-card-label">CAGR</p>
        <div className="stat-card-flex">
          <p className="stat-card-value">{isLoading ? '' : fmtPct(kpiData.avg_cagr_5y_pct, 1)}</p>
          <i className={`icon-${isLoading ? '' : kpiData.avg_cagr_5y_pct >= 0 ? 'up' : 'down'}-arrow`}></i>
        </div>
      </article>
    </div>
  )
} */

// 원인분석 prefetch — 모듈 캐시(causeStore)에 sales 데이터를 미리 채워둠.
// AnalyzePage가 같은 요청을 dedup으로 재사용 → 중복 fetch 없이 진입, 재방문 시 즉시 표시.
function prefetchCause(brand: string, sources: string[]) {
  // causeStore가 더 이상 rejection을 null로 삼키지 않으므로 prefetch가 unhandled가 되지 않게 흡수한다.
  void fetchCauseResult(brand, sources?.[0] ?? 'UBIST', 'market_landscape', 'sales')
    .catch(() => undefined)
}

function BrandCardList({ sortValue, brandsData }: { sortValue: string; brandsData: BrandCardData[] }) {
  const navigate = useNavigate()
  const [isFlipped, setIsFlipped] = useState<Record<number, boolean>>({})

  const handleFlip = (index: number) => {
    setIsFlipped(prev => ({ ...prev, [index]: !prev[index] }))
  }

  const sortedBrands = useMemo(() => {
    const cloned = brandsData.slice();
    return cloned.sort((a, b) => {
      const dataA = (a.front.sources_data && a.front.default_source) ? a.front.sources_data[a.front.default_source] : a.front;
      const dataB = (b.front.sources_data && b.front.default_source) ? b.front.sources_data[b.front.default_source] : b.front;
      
      switch(sortValue) {
        case 'sales': return (dataB.value_recent || 0) - (dataA.value_recent || 0);
        case 'ms':    return (dataB.ms_recent_pct || 0) - (dataA.ms_recent_pct || 0);
        case 'mom':   return (dataB.gr_mom_pct ?? -999) - (dataA.gr_mom_pct ?? -999);
        case 'qoq':   return (dataB.gr_qoq_pct || 0) - (dataA.gr_qoq_pct || 0);
        case 'yoy':   return (dataB.gr_yoy_pct || 0) - (dataA.gr_yoy_pct || 0);
        case 'yoy_mat': return (dataB.gr_yoy_mat_pct || 0) - (dataA.gr_yoy_mat_pct || 0);
        case 'cagr':  return b.back.cagr_5y_pct - a.back.cagr_5y_pct;
        default:      return 0;
      }
    });
  }, [brandsData, sortValue])

  return (
    <>
      {sortedBrands.map((b, i) => {
        const f = b.front
        const ext = b.back_extended
        const brandCagr = selectBrandCagr(ext?.brand_cagr_5y_pct, ext?.brand_cagr_3y_pct)

        return (
          <article className="product-card" key={b.brand || i} style={{ animationDelay: `${Math.min(i, 11) * 0.045}s` }}>
            <div className="product-card-header">
              <div className="product-card-inner">
                <div className="product-card-title-group">
                  <div className="product-card-name">{b.brand}</div>
                  <span className="product-card-category">{ext?.market_label_kor}</span>
                </div>
                <button className="btn-change" onClick={() => handleFlip(i)}>전환</button>
              </div>
              <div className={`flip-container ${isFlipped[i] ? 'is-flipped' : ''}`}>
                <div className="flip-inner">
                  <ul className="product-card-data-list front">
                    <li>
                      <div className="data-row">
                        <span className="label">매출</span>
                        <div className="line"></div>
                        <strong className="value">{fmtBaekman(f.value_recent)}</strong>
                      </div>
                    </li>
                    <li>
                      <div className="data-row">
                        <span className="label">M/S</span>
                        <div className="line"></div>
                        <strong className="value">{fmtPct(f.ms_recent_pct)}</strong>
                      </div>
                    </li>
                    <li>
                      <div className={`data-row stat-card-${deltaClass(f.gr_qoq_pct)}`}>
                        <span className="label">QoQ</span>
                        <div className="line"></div>
                        <strong className="value">{fmtPct(f.gr_qoq_pct)}</strong>
                      </div>
                    </li>
                    <li>
                      <div className={`data-row stat-card-${deltaClass(f.gr_yoy_pct)}`}>
                        <span className="label">YoY</span>
                        <div className="line"></div>
                        <strong className="value">{fmtPct(f.gr_yoy_pct)}</strong>
                      </div>
                    </li>
                  </ul>
                  <ul className="product-card-data-list back">
                    <li>
                      <div className={`data-row${brandCagr.value != null ? ` stat-card-${deltaClass(brandCagr.value)}` : ''}`} data-brand-cagr-display="exclusive-5y-3y">
                        <span className="label">{brandCagr.label}</span>
                        <div className="line"></div>
                        <strong className="value">{brandCagr.value != null ? fmtPct(brandCagr.value) : '—'}</strong>
                      </div>
                    </li>
                    <li>
                      <div className="data-row">
                        <span className="label">시장규모</span>
                        <div className="line"></div>
                        <strong className="value">{fmtBaekman(ext.market_size_recent)}</strong>
                      </div>
                    </li>
                    <li>
                      <div className={`data-row stat-card-${deltaClass(ext.excess_growth_pct)}`}>
                        <span className="label">초과성장률</span>
                        <div className="line"></div>
                        <strong className="value">{fmtPct(ext.excess_growth_pct, 1)}p</strong>
                      </div>
                    </li>
                    <li>
                      <div className="data-row">
                        <span className="label">순위</span>
                        <div className="line"></div>
                        <strong className="value">{b.rank}위</strong>
                      </div>
                    </li>
                  </ul>
                </div>
              </div>
            </div>
            <div className="product-card-footer">
              <button className="btn-analysis" onClick={() => {
                  prefetchCause(b.brand, b.sources)
                  navigate('/market/analyze', { state: { productName: b.brand, sources: b.sources } })
              }}>원인분석</button>
              <button className="btn-analysis" onClick={() => {
                  navigate('/market/deep-analyze', { state: { productName: b.brand, sources: b.sources } })}
              }>심층분석</button>
            </div>
          </article>
        )
      })}
    </>
  )
}

interface ChatItem {
  uid: string
  title: string
  date: string
  pinned?: boolean
}

const MOCK_PINNED_LIST: ChatItem[] = []
const MOCK_NORMAL_LIST: ChatItem[] = []

const SORT_OPTIONS: SelectOption[] = [
  { value: 'sales',   label: '매출순' },
  { value: 'ms',      label: 'M/S 순' },
  { value: 'mom',     label: 'MoM 순' },
  { value: 'qoq',     label: 'QoQ 순' },
  { value: 'yoy',     label: 'YoY 순' },
  { value: 'yoy_mat', label: 'YoY (MAT) 순' },
  { value: 'cagr',    label: 'CAGR 순' },
]

interface BrandCardData {
  rank: number
  brand: string
  company: string
  is_jw: boolean
  is_target: boolean
  front: Front
  back: Back
  back_extended: BackExtended
  market_id: string
  market_name: string
  market_name_short: string
  mkt_team: string
  atc_codes: string[]
  atc_desc: string
  sources: string[]
  nhi_type: string
}

// /market/status 응답 result 배열 요소 (sessionStorage 캐시 타입)
interface MarketStatusItem {
  brand_cards?: BrandCardData[]
  kpi_summary?: { UBIST?: KpiItem; IQVIA?: KpiItem }
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

interface Back {
  cagr_5y_pct: number
  sales_first_period_krw: number
  ms_first_period_pct: number
  period_first: string
}

interface BackExtended {
  market_size_recent: number
  market_cagr_5y_pct: number
  brand_cagr_5y_pct?: number | null
  brand_cagr_3y_pct?: number | null
  excess_growth_pct: number
  source_label: string
  is_dual_source: boolean
  sources: string[]
  market_definition_label: string
  market_definition_full: string
  atc_count: number
  direct_competition_count: number
  market_label_kor: string
}

function adaptV091BrandCards(cards : BrandCardData[]): BrandCardData[] {
  return (cards || []).map((card, idx) => {
    const copy = { ...card };
    copy.back_extended = copy.back_extended ?? ({} as BackExtended);
    copy.back = copy.back ?? ({} as Back);
    const ext = copy.back_extended;
    const back = copy.back;
    copy.rank = copy.rank || idx + 1;
    copy.market_name_short = copy.market_name_short || copy.market_name;
    ext.market_cagr_5y_pct = ext.market_cagr_5y_pct ?? 0;
    ext.excess_growth_pct = ext.excess_growth_pct ?? ((ext.brand_cagr_5y_pct ?? back.cagr_5y_pct ?? 0) - ext.market_cagr_5y_pct);
    ext.market_definition_label = ext.market_definition_label || `${(copy.atc_codes || []).length || 0} ATC`;
    ext.market_definition_full = ext.market_definition_full || (copy.atc_codes || []).join(', ');
    ext.direct_competition_count = ext.direct_competition_count ?? 0;
    ext.market_label_kor = ext.market_label_kor || copy.market_name;
    ext.is_dual_source = ext.is_dual_source ?? ((copy.sources || []).length > 1);
    return copy;
  });
}

function getSession<T>(key: string): T | null {
  try {
    const raw = sessionStorage.getItem(key)
    return raw ? (JSON.parse(raw) as T) : null
  } catch {
    return null
  }
}

function isMissingNumber(v: number) {
  return v === null || v === undefined || Number.isNaN(Number(v))
}
function fmtPct(v: number, d = 2) {
  if (isMissingNumber(v)) return 'N/A'
  const n = Number(v)
  return (n >= 0 ? '+' : '') + n.toFixed(d) + '%'
}
// KpiSummary 전용 — 주석 유지 (KpiSummary와 함께 복구)
/* function fmtPctNoSign(v: number, d = 1) {
  if (isMissingNumber(v)) return 'N/A'
  return Number(v).toFixed(d) + '%'
} */
function deltaClass(v: number) {
  return isMissingNumber(v) ? '' : v >= 0 ? 'up' : 'down'
}

export default function DashboardPage() {
  const navigate = useNavigate()
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [activeChatId, setActiveChatId] = useState<string | null>('2')
  const [sortValue, setSortValue] = useState('sales')
  const [alertMessage, setAlertMessage] = useState('')
  const scrollRef = useRef<HTMLDivElement>(null)
  const [showScrollTop, setShowScrollTop] = useState(false)
  // 마운트 시 1회만 sessionStorage 파싱 (매 렌더 재파싱 방지 + 참조 안정화 → effect deps 정합성)
  const [cachedStatus] = useState(() => getSession<MarketStatusItem>('marketStatusResult'))
  const [data, setData] = useState<BrandCardData[]>(() => {
    const brandCards = cachedStatus?.brand_cards
    return brandCards ?? []
  })
  // KpiSummary 주석으로 kpiData 값 미read (setter만 사용)
  const [, setKpiData] = useState<KpiItem>(() => {
    return cachedStatus?.kpi_summary?.UBIST ?? ({} as KpiItem)
  })
  // 헤더 "기준" 라벨 — 각 소스의 period_recent (UBIST="2026-05" / IQVIA="2026-Q1")
  const [ubistPeriod, setUbistPeriod] = useState<string>(() => cachedStatus?.kpi_summary?.UBIST?.period_recent ?? '')
  const [iqviaPeriod, setIqviaPeriod] = useState<string>(() => cachedStatus?.kpi_summary?.IQVIA?.period_recent ?? '')
  const [, setIsLoading] = useState(() => !cachedStatus)   // KpiSummary 주석으로 isLoading 값 미read (setter만 사용)

  useEffect(() => {
    const loadData = async () => {
      try {
        if (!cachedStatus) setIsLoading(true)

        const response = await apiFetch('/api/v1/market/status', {
          method: 'POST',
          body: JSON.stringify({ marketId: '' })
        })

        const resData = await response.json()

        if (resData.status === 'SUCCESS') {
          const brandCards = resData.result?.brand_cards
          const kpiSummary = resData.result?.kpi_summary?.UBIST

          if (brandCards && kpiSummary) {
            const adapted = adaptV091BrandCards(brandCards)
            setData(adapted ?? [])
            setKpiData(kpiSummary ?? ({} as KpiItem))
            setUbistPeriod(resData.result?.kpi_summary?.UBIST?.period_recent ?? '')
            setIqviaPeriod(resData.result?.kpi_summary?.IQVIA?.period_recent ?? '')
            sessionStorage.setItem('marketStatusResult', JSON.stringify(resData.result ?? []))
          } else {
            setData([])
            setKpiData({} as KpiItem)
          }
        }

      } catch (error) {
        console.error('API 호출 에러:', error)
      } finally {
        setIsLoading(false)
      }
    }
    void loadData()
  }, [cachedStatus])

    return (
        <div className={`wrap ${sidebarOpen ? 'open' : 'close'}`}>
            <Sidebar
                pinnedList={MOCK_PINNED_LIST}
                normalList={MOCK_NORMAL_LIST}
                activeChatId={activeChatId}
                onToggleSidebar={() => setSidebarOpen(p => !p)}
                onNewChat={() => navigate('/market/chat')}
                onSelectChat={uid => setActiveChatId(uid)}
                onDeleteModal={() => {}}
                onChangeNameModal={() => {}}
                onPinChat={() => {}}
                onUnpinChat={() => {}}
                hideChatHistory
            />

            <div className="container-wrap dashboard">
                <TopNavigation
                    onAlertMessage={(msg: string) => setAlertMessage(msg)}
                />

                {/* ✅ 중복 제거 — scroll-container 단일 사용 */}
                <div
                    className="content-wrap scroll-container"
                    ref={scrollRef}
                    onScroll={e => setShowScrollTop((e.currentTarget as HTMLDivElement).scrollTop > 300)}
                >
                    <div className="content">
                        <div className="content-inner">
                            <div className="dashboard-inner">

                                <section className="status-section">
                                {/* 기준 pill — '자사 제품 현황' 헤더 오른쪽으로 이동. 상단은 미사용(향후 복구 대비 주석 유지) */}
                                  {/* <div className="section-title">시장현황 <span className="sub-title">Monitoring</span>
                                      <div className="info-right-wrap">
                                          <div className="bx-info"><span>UBIST 기준</span>{ubistPeriod ? fmtPeriodKor(ubistPeriod) : '-'}</div>
                                          <div className="bx-line"></div>
                                          <div className="bx-info"><span>IQVIA 기준</span>{iqviaPeriod ? fmtPeriodKor(iqviaPeriod) : '-'}</div>
                                      </div>
                                    </div> */}
                                {/* KPI 요약 카드 — 미사용(향후 복구 대비 주석 유지) */}
                                {/* <KpiSummary kpiData={kpiData} isLoading={isLoading} /> */}
                                </section>

                                <section className="product-section">
                                    <div className="section-header">
                                        <div className="section-header-left">
                                            <div className="section-title">자사 제품 현황</div>
                                            <SelectBox
                                                wrapperClassName="sel-access"
                                                options={SORT_OPTIONS}
                                                value={sortValue}
                                                onChange={setSortValue}
                                                weight={400}
                                            />
                                        </div>
                                        <div className="info-right-wrap">
                                            <div className="bx-info"><span>UBIST 기준</span>{ubistPeriod ? fmtPeriodKor(ubistPeriod) : '-'}</div>
                                            <div className="bx-line"></div>
                                            <div className="bx-info"><span>IQVIA 기준</span>{iqviaPeriod ? fmtPeriodKor(iqviaPeriod) : '-'}</div>
                                        </div>
                                    </div>
                                    <div className="card-grid card-grid-product">
                                        <BrandCardList key={sortValue} sortValue={sortValue} brandsData={data} />
                                    </div>
                                </section>

                            </div>
                        </div>
                    </div>
                </div>  {/* scroll-container */}
            </div>  {/* ✅ container-wrap dashboard */}

            {showScrollTop && (
                <div
                    className="scroll-botton-up-n"
                    onClick={() => scrollRef.current?.scrollTo({ top: 0, behavior: 'smooth' })}
                />
            )}

            <Modals
                alertMessage={alertMessage}
                onCloseAlert={() => setAlertMessage('')}
            />
        </div>
    )
}
