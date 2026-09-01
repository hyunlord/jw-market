import { useState, useRef, useEffect, useLayoutEffect } from 'react'
import { Link, useLocation } from 'react-router-dom'

interface ChatItem {
  uid: string
  title: string
  date: string
  pinned?: boolean
}

interface ChatLayerPos {
  chatId: string
  x: number
  y: number
  pinned: boolean
}

interface Props {
  pinnedList: ChatItem[]
  normalList: ChatItem[]
  activeChatId: string | null
  onToggleSidebar: () => void
  onNewChat: () => void
  onSelectChat: (uid: string) => void
  onDeleteModal: (uid: string) => void
  onChangeNameModal: (uid: string) => void
  onPinChat: (uid: string) => void
  onUnpinChat: (uid: string) => void
  onBulkDeleteRequest?: (uids: string[]) => void
  resetSelectionSignal?: number
  hideChatHistory?: boolean
  hideNewChat?: boolean   // 시장분석(대시보드/원인분석/심층분석)은 새 채팅 버튼 숨김 — 2차 때 진행
  showMcpInfo?: boolean   // R&D 전용 — 'MCP 서버 정보' 메뉴 노출 (시장분석엔 미노출)
  hasMore?: boolean       
  loadingMore?: boolean  
  onLoadMore?: () => void 
}

export default function Sidebar({
  pinnedList, normalList, activeChatId,
  onToggleSidebar, onNewChat, onSelectChat,
  onDeleteModal, onChangeNameModal, onPinChat, onUnpinChat,
  onBulkDeleteRequest,
  resetSelectionSignal = 0,
  hideChatHistory = false,
  hideNewChat = false,
  showMcpInfo = false,
  hasMore = false,
  loadingMore = false,
  onLoadMore,
}: Props) {
  const { pathname } = useLocation()
  const [chatLayer, setChatLayer] = useState<ChatLayerPos | null>(null)
  const layerRef = useRef<HTMLDivElement>(null)
  const scrollRef = useRef<HTMLDivElement>(null)   
  const lockedScrollTopRef = useRef(0)  
  const [selectMode, setSelectMode] = useState(false)
  const [newChatHover, setNewChatHover] = useState(false)
  const [mcpHover, setMcpHover] = useState(false)
  const [selectedUids, setSelectedUids] = useState<Set<string>>(new Set())

  const [lastResetSignal, setLastResetSignal] = useState(resetSelectionSignal)
  if (resetSelectionSignal !== lastResetSignal) {
    setLastResetSignal(resetSelectionSignal)
    setSelectMode(false)
    setSelectedUids(new Set())
  }

  const handleLayerBtn = (e: React.MouseEvent, item: ChatItem) => {
    e.stopPropagation()
    if (chatLayer?.chatId === item.uid) {
      setChatLayer(null)
      return
    }
    const rect = (e.currentTarget as HTMLElement).getBoundingClientRect()
    setChatLayer({ chatId: item.uid, x: rect.right + 20, y: rect.top - 5, pinned: item.pinned ?? false })
  }

  const closeLayer = () => setChatLayer(null)

  // 컨텍스트 메뉴 열린 동안 사이드바 스크롤 잠금 (스크롤바는 유지, 작동만 차단 — fixed 메뉴가 항목과 어긋나지 않게).
  useEffect(() => {
    const el = scrollRef.current
    if (!chatLayer || !el) return
    lockedScrollTopRef.current = el.scrollTop
    const prevent = (e: Event) => e.preventDefault()
    el.addEventListener('wheel', prevent, { passive: false })
    el.addEventListener('touchmove', prevent, { passive: false })
    return () => {
      el.removeEventListener('wheel', prevent)
      el.removeEventListener('touchmove', prevent)
    }
  }, [chatLayer])

  // 컨텍스트 메뉴가 뷰포트 하단을 넘으면(맨 아래 히스토리) 위로 끌어올려 잘림 방지.
  useLayoutEffect(() => {
    if (!chatLayer) return
    const el = layerRef.current
    if (!el) return
    const h = el.offsetHeight
    const maxTop = window.innerHeight - h - 8
    if (chatLayer.y > maxTop) {
      setChatLayer(prev => prev ? { ...prev, y: Math.max(8, maxTop) } : prev)
    }
  }, [chatLayer])

  // chat-layer 외부 클릭 시 닫기 (capture 단계 — stopPropagation 무관하게 동작)
  useEffect(() => {
    if (!chatLayer) return
    const handler = (e: MouseEvent) => {
      const target = e.target as Element
      if (layerRef.current?.contains(target)) return
      if (target.closest('.btn-chat-layer')) return // 트리거 버튼은 handleLayerBtn에 위임
      setChatLayer(null)
    }
    document.addEventListener('click', handler, true)
    return () => document.removeEventListener('click', handler, true)
  }, [chatLayer])

  const toggleSelect = (uid: string) => {
    setSelectedUids(prev => {
      const next = new Set(prev)
      if (next.has(uid)) next.delete(uid)
      else next.add(uid)
      return next
    })
  }

  const handleEnterSelectMode = () => {
    setChatLayer(null)
    setSelectMode(true)
  }

  const handleCancelSelect = () => {
    setSelectMode(false)
    setSelectedUids(new Set())
  }

  const handleBulkDeleteClick = () => {
    if (selectedUids.size === 0) return
    const uids = Array.from(selectedUids)
    onBulkDeleteRequest?.(uids)
  }

  // 채팅 항목 진입 슬라이드 애니메이션은 CSS(common.css의 .chat-list .chat-item mount 애니메이션)로 처리 → JS 불필요.

  // 무한 스크롤 — 일반 목록 스크롤이 바닥 80px 이내로 오면 다음 페이지 요청
  const handleListScroll = (e: React.UIEvent<HTMLDivElement>) => {
    // 컨텍스트 메뉴 열림: 스크롤바 드래그 등으로 움직인 스크롤을 잠근 위치로 되돌림
    if (chatLayer) { e.currentTarget.scrollTop = lockedScrollTopRef.current; return }
    if (!hasMore || loadingMore) return
    const el = e.currentTarget
    if (el.scrollHeight - el.scrollTop - el.clientHeight < 80) onLoadMore?.()
  }

  const renderChatItem = (item: ChatItem) => (
    <li
      key={item.uid}
      className={['chat-item',
        item.pinned ? 'notice' : '',
        (item.uid === activeChatId || chatLayer?.chatId === item.uid) ? 'active' : '',
        item.uid === activeChatId ? 'origin' : '',
        chatLayer?.chatId === item.uid ? 'active-by-layer' : '',
      ].filter(Boolean).join(' ')}
    >
      <div className="text-wrap">
        {item.pinned && <div className="icon-notice" />}
        <div
          className="inner-bx"
          style={selectMode
            ? { cursor: 'pointer', display: 'flex', alignItems: 'flex-start', gap: 8, paddingRight: 0 }
            : { cursor: 'pointer' }
          }
          onClick={() => selectMode ? toggleSelect(item.uid) : onSelectChat(item.uid)}
        >
          {selectMode && (
            <div
              className={`sidebar-check${selectedUids.has(item.uid) ? ' checked' : ''}`}
              onClick={e => { e.stopPropagation(); toggleSelect(item.uid) }}
            />
          )}
          <div style={selectMode ? { flex: 1, minWidth: 0 } : {}}>
            <p className="tx-title">
              <a href="#" onClick={e => e.preventDefault()}>{item.title}</a>
            </p>
            <p className="tx-date">{item.date}</p>
          </div>
        </div>
        {!selectMode && (
          <div className="btn-chat-layer" onClick={e => handleLayerBtn(e, item)} />
        )}
      </div>
    </li>
  )

  return (
    <div id="siadbar-wrap" className="siadbar-wrap">
      <div className="sidebar-header">
        <button className="btn-toggle-sidebar" onClick={onToggleSidebar}>
          <div className="tooltip-toggle"><p>메뉴 접기</p></div>
        </button>
      </div>

      {/* MCP 서버 정보 — R&D 전용. 현재 페이지가 /rnd/mcp면 active. (시장분석엔 showMcpInfo 미전달 → 미노출) */}
      {showMcpInfo && (
        <div className={`mcp-info-section${pathname === '/rnd/mcp' ? ' active' : ''}`}>
          <Link
            to="/rnd/mcp"
            className="btn-mcp-info"
            onMouseEnter={() => setMcpHover(true)}
            onMouseLeave={() => setMcpHover(false)}
          >
            <div className="icon-wrap" />
            <div className="text-wrap">MCP 서버 정보</div>
          </Link>
          <div className={`tooltip-mcp-info${mcpHover ? ' open' : ''}`}><p>MCP 서버 정보</p></div>
        </div>
      )}

      {!hideNewChat && (
        <div className={`new-chat-section${(pathname === '/rnd' || pathname === '/market/chat') && !activeChatId ? ' active' : ''}`}>
          <a
            href="#"
            className="btn-new-chat"
            onClick={e => { e.preventDefault(); onNewChat() }}
            onMouseEnter={() => setNewChatHover(true)}
            onMouseLeave={() => setNewChatHover(false)}
          >
            <div className="icon-wrap" />
            <div className="text-wrap">새 채팅</div>
          </a>
          <div className={`tooltip-new-chat${newChatHover ? ' open' : ''}`}><p>새 채팅 (Alt+N)</p></div>
        </div>
      )}

      <div className="chat-history-list" style={hideChatHistory ? { display: 'none' } : undefined}>
        <div className="list-title-wrap">
          <div className="list-title">채팅</div>
          {/* 히스토리(고정+일반) 비어있으면 선택삭제 영역 자체 숨김 */}
          {(pinnedList.length > 0 || normalList.length > 0) && (
            selectMode ? (
              <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                <a
                  href="#"
                  style={{ fontWeight: 500, fontSize: 14, color: '#82828D' }}
                  onClick={e => { e.preventDefault(); handleCancelSelect() }}
                >
                  취소
                </a>
                <span style={{ color: '#D0D0D6', fontSize: 12 }}>|</span>
                <a
                  href="#"
                  style={{
                    fontWeight: 500,
                    fontSize: 14,
                    color: selectedUids.size > 0 ? '#00a9e5' : '#B2B2B8',
                    pointerEvents: selectedUids.size === 0 ? 'none' : 'auto',
                  }}
                  onClick={e => { e.preventDefault(); handleBulkDeleteClick() }}
                >
                  선택삭제
                </a>
              </div>
            ) : (
              <a
                href="#"
                style={{ fontWeight: 400, fontSize: 14, color: '#82828D' }}
                onClick={e => { e.preventDefault(); handleEnterSelectMode() }}
              >
                선택삭제
              </a>
            )
          )}
        </div>
        {/* chat-layer는 CSS 선택자(.siadbar-wrap .chat-history-list .chat-list .chat-layer)를 만족하도록 .chat-list 안에 위치 */}
        {/* scroll-container: "채팅/선택삭제" 헤더만 위에 고정. 고정 채팅은 별도 섹션 없이 목록 맨 위에 함께 스크롤 */}
        <div
          ref={scrollRef}
          className="chat-list scroll-container"
          onScroll={handleListScroll}
        >
          <ul>{[...pinnedList, ...normalList].map(renderChatItem)}</ul>
          {/* 다음 페이지 로딩 스피너 (무한 스크롤) */}
          {loadingMore && (
            <div className="sidebar-more-spinner">
              <div className="fixed-8bar-spinner">
                {Array.from({ length: 8 }, (_, i) => <div key={i} className={`bar bar${i + 1}`} />)}
              </div>
            </div>
          )}
          <div
            ref={layerRef}
            className={`chat-layer${chatLayer ? ' open show-anim' : ''}`}
            style={chatLayer ? { position: 'fixed', left: chatLayer.x, top: chatLayer.y } : {}}
            onClick={e => e.stopPropagation()}
          >
            <ul>
              {chatLayer?.pinned ? (
                <li>
                  <a href="#" onClick={e => { e.preventDefault(); if (chatLayer) { onUnpinChat(chatLayer.chatId); closeLayer() } }}>
                    <div className="icon-wrap icon03" /><div className="text-wrap">고정 해제</div>
                  </a>
                </li>
              ) : (
                <li>
                  <a href="#" onClick={e => { e.preventDefault(); if (chatLayer) { onPinChat(chatLayer.chatId); closeLayer() } }}>
                    <div className="icon-wrap icon02" /><div className="text-wrap">채팅 고정</div>
                  </a>
                </li>
              )}
              <li>
                <a href="#" onClick={e => { e.preventDefault(); if (chatLayer) { onChangeNameModal(chatLayer.chatId); closeLayer() } }}>
                  <div className="icon-wrap icon04" /><div className="text-wrap">이름 변경</div>
                </a>
              </li>
              <li>
                <a href="#" onClick={e => { e.preventDefault(); if (chatLayer) { onDeleteModal(chatLayer.chatId); closeLayer() } }}>
                  <div className="icon-wrap icon05" /><div className="text-wrap">삭제</div>
                </a>
              </li>
            </ul>
          </div>
        </div>
      </div>
    </div>
  )
}
