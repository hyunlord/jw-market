import { useState, useRef, useEffect, useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import { apiFetch } from '../../utils/apiFetch'

interface Props {
  onAlertMessage?: (msg: string) => void    
}

interface BrandItem {
  brand: string
  market_id: string
  sources?: string[]   // UBIST/IQVIA — navState로 전달해 IQVIA-only 브랜드의 빈 차트 방지
}

export default function BrandSearchArea({onAlertMessage = () => {}} : Props) {
  const navigate = useNavigate()

  // --- [상태 관리] ---
  const [searchInput, setSearchInput] = useState('')
  const [suggestionsOpen, setSuggestionsOpen] = useState(false)
  const [selectedIndex, setSelectedIndex] = useState<number | null>(null)
  const [brands, setBrands] = useState<BrandItem[]>([])

  // --- [DOM 접근을 위한 Ref] ---
  const searchWrapRef = useRef<HTMLDivElement>(null)
  const suggestionsRef = useRef<HTMLUListElement | null>(null)
    

  /**
   * [Effect] 컴포넌트 마운트 시 최초 1회 전체 브랜드 데이터를 조회하여 메모리에 저장
   */
  useEffect(() => {
    apiFetch('/api/v1/market/brands', {
      method: 'POST',
      body: JSON.stringify({ query: '', marketId: '' }),
    })
      .then(r => r.json() as Promise<{ status: string; result: BrandItem[] }>)
      .then(d => { if (d.status === 'SUCCESS') setBrands(d.result) })
      .catch(() => {})
  }, [])

  /**
   * [Memo] 사용자의 입력(searchInput)에 따라 실시간으로 일치하는 브랜드를 필터링 (성능 최적화)
   */  
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

  //   useEffect 내 동기 setState는 react-hooks/set-state-in-effect 위반 → 렌더 중 이전 값 비교로 조정
  const [lastMatching, setLastMatching] = useState(matchingBrands)
  if (matchingBrands !== lastMatching) {
    setLastMatching(matchingBrands)
    setSuggestionsOpen(matchingBrands.length > 0)
    setSelectedIndex(null)
  }

  /**
   * [Effect] 외부 영역 클릭 시 자동완성 창 닫기 (Dropdown Close)
   */  
  useEffect(() => {
    if (!suggestionsOpen) return
    const handler = (e: MouseEvent) => {
      if (searchWrapRef.current?.contains(e.target as Node)) return
      setSuggestionsOpen(false)
    }
    document.addEventListener('click', handler, true)
    return () => document.removeEventListener('click', handler, true)
  }, [suggestionsOpen])

  /**
   * [Effect] 키보드로 항목 선택 시, 선택된 <li> 항목이 스크롤 뷰 밖으로 가려지지 않도록 자동 스크롤
   */
  useEffect(() => {
    if (selectedIndex === null) return
    const ul = suggestionsRef.current
    if (!ul) return
    const el = ul.children[selectedIndex] as HTMLElement | undefined
    if (el) {
      // 선택된 엘리먼트가 리스트 내에 잘 보이도록 위치 조정
      el.scrollIntoView({ block: 'nearest', inline: 'nearest' })
    }
  }, [selectedIndex, suggestionsOpen, matchingBrands])

  /**
  * [핸들러] 사용자가 검색어를 직접 입력하고 엔터를 누르거나 검색 버튼을 클릭했을 때 실행
  */
  const handleBrandSearch = async (e: React.SyntheticEvent) => {
    e.preventDefault()
    const trimmed = searchInput.trim()
    if (!trimmed) return  // 빈 문자열이면 실행 안 함
    try {
      // 서버에 검색어 검증 요청
      const res = await apiFetch('/api/v1/market/brands', {
        method: 'POST',
        body: JSON.stringify({ query: trimmed, marketId: '' }),
      })
      const data = await res.json() as { status: string; result: Array<{ brand: string; sources?: string[]; general_sources?: string[]; strategic_sources?: string[]; is_jw?: boolean }> }
      if (data.status === 'SUCCESS') {
        // 서버에서 받아온 리스트 중, 입력한 텍스트와 정확히 100% 일치하는 브랜드가 있는지 확인
        const matched = data.result.find(item => item.brand === trimmed)
        if (matched) {
          //   (경쟁 브랜드는 marketBrandsResult(자사 25개)에 없어 페이지가 검색결과 값을 받아야 함)
          navigate('/market/analyze', { state: {
            productName: matched.brand, sources: matched.sources,
            generalSources: matched.general_sources, strategicSources: matched.strategic_sources,
            assay: matched.is_jw === false ? 'market' : 'jw',
          } })
          setSearchInput('')
        } else {
          // 일치하는 브랜드가 없으면 경고 메시지 출력
          onAlertMessage('입력하신 브랜드명과 일치하는\n결과가 없습니다.\n브랜드명을 다시 한 번 확인해 주세요.')
        }
      }
    } catch (err) {
      console.error('검색 실패:', err)
    }
  }  

  /**
   * [핸들러] 인풋창에서 키보드(상/하/엔터/ESC) 입력 시 자동완성 리스트 제어
   */  
  const handleSearchKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.nativeEvent.isComposing) return
    if (e.key === 'ArrowDown') {
      e.preventDefault() // 인풋 커서가 맨 뒤로 이동하는 기본 동작 방지
      if (!suggestionsOpen && matchingBrands.length > 0) {
        // 창이 닫혀있는데 추천 항목이 있다면 창을 열고 첫 번째 항목 선택
        setSuggestionsOpen(true)
        setSelectedIndex(0)
      } else if (matchingBrands.length > 0) {
        // 이미 열려있다면 다음 항목으로 인덱스 이동 (최대 index를 넘지 않도록 제한)
        setSelectedIndex(prev => prev === null ? 0 : Math.min(matchingBrands.length - 1, prev + 1))
      }
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      if (matchingBrands.length > 0) {
        // 이전 항목으로 인덱스 이동 (0 미만으로 내려가지 않도록 제한)
        setSelectedIndex(prev => prev === null ? matchingBrands.length - 1 : Math.max(0, prev - 1))
      }
    } else if (e.key === 'Enter') {
      // 키보드로 특정 항목을 선택한 상태에서 엔터를 누른 경우
      if (suggestionsOpen && selectedIndex !== null && matchingBrands[selectedIndex]) {
        e.preventDefault() // 폼이 제출(submit)되어 페이지가 새로고침되거나 중복 API 요청이 가는 것을 방지
        const item = matchingBrands[selectedIndex]
        // 선택된 아이템의 데이터로 페이지 이동
        navigate('/market/analyze', { state: { productName: item.brand, sources: item.sources } })
        setSuggestionsOpen(false)
        setSelectedIndex(null)
      }
      // 아무것도 선택하지 않고 엔터를 친 경우는 <form onSubmit={handleBrandSearch}> 가 처리함
    } else if (e.key === 'Escape') {
      // ESC를 누르면 추천 창을 닫고 선택 초기화
      setSuggestionsOpen(false)
      setSelectedIndex(null)
    }
  }
  

  return (
    <div className="utility-menu">
      <ul>
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
                onFocus={() => { if (matchingBrands.length > 0) setSuggestionsOpen(true) }} // 인풋 포커스 시 매칭 항목 있으면 추천창 다시 열기
                onKeyDown={handleSearchKeyDown}
              />
              {matchingBrands.length > 0 && suggestionsOpen && (
                <ul className="search-suggestions" ref={suggestionsRef}>
                  {matchingBrands.map((item, idx) => (
                    <li
                      key={item.brand}
                      className={selectedIndex === idx ? 'selected' : ''}  // 현재 선택된 인덱스에 CSS 클래스 부여 (하이라이트)
                      tabIndex={0}
                      onFocus={() => setSelectedIndex(idx)}         // 키보드 포커스 시 인덱스 동기화
                      onMouseEnter={() => setSelectedIndex(idx)}    // 마우스가 올라가면 해당 인덱스로 선택 변경
                      onMouseLeave={() => setSelectedIndex(null)}   // 마우스가 떠나면 하이라이트 해제
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
      </ul>
    </div>
  )    
}