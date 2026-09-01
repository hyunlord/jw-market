import { useState, useRef, useEffect } from 'react'
import { useNavigate } from 'react-router-dom'
import Sidebar from '../components/main/Sidebar'
import TopNavigation from '../components/main/TopNavigation'
import Modals from '../components/main/Modals'
import { MCP_SERVERS, MCP_SERVER_TOTAL } from '../data/mcpServers'
import { useChatSessions } from '../utils/useChatSessions'
import { fetchRndInternalDocs, fetchRndThesisDocs, type RndDocument } from '../utils/rndDocuments'

// 100개당 1페이지 (기획).
const PER_PAGE = 100

// 페이지 번호 윈도우 (현재 기준 최대 5개) — 문서 40여 페이지에서 번호 폭증 방지
function pageWindow(current: number, total: number, size = 5): number[] {
  let start = Math.max(1, current - Math.floor(size / 2))
  const end = Math.min(total, start + size - 1)
  start = Math.max(1, end - size + 1)
  return Array.from({ length: end - start + 1 }, (_, i) => start + i)
}

function Pagination({ page, totalPages, onPage }: { page: number; totalPages: number; onPage: (n: number) => void }) {
  if (totalPages <= 1) return null
  const first = page === 1
  const last = page === totalPages
  return (
    <div className="mcp-pagination">
      <button type="button" className={`btn-page-nav-first${first ? ' disabled' : ''}`} disabled={first} onClick={() => onPage(1)} />
      <button type="button" className={`btn-page-nav-prev${first ? ' disabled' : ''}`} disabled={first} onClick={() => onPage(page - 1)} />
      <div className="page-numbers">
        {pageWindow(page, totalPages).map(n => (
          <button key={n} type="button" className={`btn-page${n === page ? ' active' : ''}`} onClick={() => onPage(n)}>{n}</button>
        ))}
      </div>
      <button type="button" className={`btn-page-nav-next${last ? ' disabled' : ''}`} disabled={last} onClick={() => onPage(page + 1)} />
      <button type="button" className={`btn-page-nav-last${last ? ' disabled' : ''}`} disabled={last} onClick={() => onPage(totalPages)} />
    </div>
  )
}

export default function McpServerPage() {
  const navigate = useNavigate()
  const [sidebarOpen, setSidebarOpen] = useState(() => localStorage.getItem('sidebarOpen') === 'true')

  // 알림 모달 상태 (세션 훅 에러 + TopNavigation 검색 실패 라우팅)
  const [alertMessage, setAlertMessage] = useState<string | null>(null)

  // 사이드바 채팅 세션 목록 + 관리 (공용 훅) — MainPage와 동일 소스
  const {
    pinnedList, normalList,
    pinChat, unpinChat, renameChat, deleteChat, bulkDelete,
  } = useChatSessions({ onError: setAlertMessage })

  // 채팅 관리 모달 상태 (이 페이지 자체 관리)
  const [deleteModal, setDeleteModal] = useState(false)
  const [changeNameModal, setChangeNameModal] = useState(false)
  const [bulkDeleteModal, setBulkDeleteModal] = useState(false)
  const [targetUid, setTargetUid] = useState<string | null>(null)
  const [pendingBulkUids, setPendingBulkUids] = useState<string[]>([])
  const [bulkResetSignal, setBulkResetSignal] = useState(0)

  const [selectedSys, setSelectedSys] = useState<string | null>(() => MCP_SERVERS[0]?.sys ?? null)
  const [tab, setTab] = useState<'tool' | 'doc' | 'project'>('tool')
  const [toolPage, setToolPage] = useState(1)

  const [docs, setDocs] = useState<RndDocument[] | null>(null)
  const [docsLoading, setDocsLoading] = useState(false)
  const [docsError, setDocsError] = useState(false)
  const [docSearch, setDocSearch] = useState('')
  const [docPage, setDocPage] = useState(1)

  const [projects, setProjects] = useState<RndDocument[] | null>(null)
  const [projectsLoading, setProjectsLoading] = useState(false)
  const [projectsError, setProjectsError] = useState(false)
  const [projectSearch, setProjectSearch] = useState('')
  const [projectPage, setProjectPage] = useState(1)

  const scrollRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (scrollRef.current) scrollRef.current.scrollTop = 0
  }, [tab, toolPage, docPage, projectPage])

  const selected = MCP_SERVERS.find(s => s.sys === selectedSys) ?? null
  const tools = selected?.tools ?? []
  const toolTotalPages = Math.max(1, Math.ceil(tools.length / PER_PAGE))
  const toolSafePage = Math.min(toolPage, toolTotalPages)
  const pageTools = tools.slice((toolSafePage - 1) * PER_PAGE, toolSafePage * PER_PAGE)

  const handleSelectServer = (sys: string) => {
    setSelectedSys(sys)
    setTab('tool')
    setToolPage(1)
  }

  const loadDocs = async () => {
    setDocsLoading(true)
    setDocsError(false)
    try {
      setDocs(await fetchRndInternalDocs())   // §7-3 /list/in (사내문서, vdb_id=124 서버 고정)
    } catch {
      setDocsError(true)
    } finally {
      setDocsLoading(false)
    }
  }

  const openDocTab = () => {
    setTab('doc')
    if (docs === null && !docsLoading) loadDocs()   // 최초 1회만 로드
  }

  const loadProjects = async () => {
    setProjectsLoading(true)
    setProjectsError(false)
    try {
      setProjects(await fetchRndThesisDocs()) 
    } catch {
      setProjectsError(true)
    } finally {
      setProjectsLoading(false)
    }
  }

  const openProjectTab = () => {
    setTab('project')
    if (projects === null && !projectsLoading) loadProjects()
  }

  const docFiltered = (docs ?? []).filter(d => d.fileName.toLowerCase().includes(docSearch.trim().toLowerCase()))
  const docTotalPages = Math.max(1, Math.ceil(docFiltered.length / PER_PAGE))
  const docSafePage = Math.min(docPage, docTotalPages)
  const docPageItems = docFiltered.slice((docSafePage - 1) * PER_PAGE, docSafePage * PER_PAGE)

  const onDocSearchChange = (v: string) => { setDocSearch(v); setDocPage(1) }

  const projectFiltered = (projects ?? []).filter(d => d.fileName.toLowerCase().includes(projectSearch.trim().toLowerCase()))
  const projectTotalPages = Math.max(1, Math.ceil(projectFiltered.length / PER_PAGE))
  const projectSafePage = Math.min(projectPage, projectTotalPages)
  const projectPageItems = projectFiltered.slice((projectSafePage - 1) * PER_PAGE, projectSafePage * PER_PAGE)

  const onProjectSearchChange = (v: string) => { setProjectSearch(v); setProjectPage(1) }

  const targetTitle = [...pinnedList, ...normalList].find(c => c.uid === targetUid)?.title ?? ''

  // 사내 문서 정보 / 논문 정보 공용 렌더 
  const renderDocTable = (cfg: {
    loading: boolean
    error: boolean
    loaded: boolean
    filtered: RndDocument[]
    pageItems: RndDocument[]
    search: string
    onSearchChange: (v: string) => void
    page: number
    totalPages: number
    onPage: (n: number) => void
  }) => (
    <>
      <div className="mcp-list-header">
        <div className="mcp-header-left">
          <div className="mcp-total-count">총 <strong>{cfg.loaded ? cfg.filtered.length : 0}</strong>개</div>
        </div>
        <div className="mcp-header-right">
          <div className={`mcp-search-area${cfg.search ? ' is-typing is-success' : ''}`}>
            <div className="mcp-input-wrap">
              <input
                type="text"
                className="mcp-search-input"
                placeholder="검색어를 입력해 주세요."
                value={cfg.search}
                onChange={e => cfg.onSearchChange(e.target.value)}
              />
              <button type="button" className="mcp-clear-btn" aria-label="검색어 지우기" onClick={() => cfg.onSearchChange('')} />
            </div>
            <button type="button" className="mcp-search-btn" aria-label="검색" />
          </div>
        </div>
      </div>

      {cfg.loading ? (
        <div className="mcp-nodata-state-wrap">
          <div className="fixed-8bar-spinner">
            {Array.from({ length: 8 }, (_, i) => <div key={i} className={`bar bar${i + 1}`} />)}
          </div>
        </div>
      ) : cfg.error ? (
        <div className="mcp-nodata-state-wrap">
          <div className="mcp-nodata-state">
            <div className="mcp-nodata-icon" />
            <p className="mcp-nodata-text">문서를 불러오지 못했습니다.<br />잠시 후 다시 시도해 주세요.</p>
          </div>
        </div>
      ) : cfg.filtered.length === 0 ? (
        <div className="mcp-nodata-state-wrap">
          <div className="mcp-nodata-state">
            <div className="mcp-nodata-icon" />
            <p className="mcp-nodata-text">{cfg.search ? '검색 결과가 없습니다.' : '표시할 데이터가 없습니다.'}</p>
          </div>
        </div>
      ) : (
        <>
          <div className="mcp-table-wrap">
            <table className="mcp-table">
              <colgroup>
                <col style={{ width: '230px' }} />
                <col style={{ width: 'auto' }} />
              </colgroup>
              <thead>
                <tr>
                  <th>번호</th>
                  <th>파일명</th>
                </tr>
              </thead>
              <tbody>
                {cfg.pageItems.map(d => (
                  <tr key={d.id}>
                    <th className="col-tool">{d.id}</th>
                    <td className="col-desc">{d.fileName}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <Pagination page={cfg.page} totalPages={cfg.totalPages} onPage={cfg.onPage} />
        </>
      )}
    </>
  )

  return (
    <div className={`wrap ${sidebarOpen ? 'open' : 'close'}`}>
      <Sidebar
        pinnedList={pinnedList}
        normalList={normalList}
        activeChatId={null}
        showMcpInfo
        onToggleSidebar={() => setSidebarOpen(p => { localStorage.setItem('sidebarOpen', String(!p)); return !p })}
        onNewChat={() => navigate('/rnd')}
        onSelectChat={uid => navigate(`/rnd?session=${uid}`)}
        onDeleteModal={uid => { setTargetUid(uid); setDeleteModal(true) }}
        onChangeNameModal={uid => { setTargetUid(uid); setChangeNameModal(true) }}
        onPinChat={pinChat}
        onUnpinChat={unpinChat}
        onBulkDeleteRequest={uids => { setPendingBulkUids(uids); setBulkDeleteModal(true) }}
        resetSelectionSignal={bulkResetSignal}
      />

      <div className="container-wrap detail">
        <TopNavigation onAlertMessage={setAlertMessage} />

        <div className="content-wrap scroll-container mcp-list" ref={scrollRef}>
          <div className="content">
            <div className="content-inner">
              <div className="mcp-list-wrap">
                <div className="mcp-title-wrap">MCP 서버 목록<span className="tx-num">(총 {MCP_SERVER_TOTAL}개)</span></div>

                <div className="mcp-layout">
                  {/* 좌측 — 서버 목록 */}
                  <div className="mcp-sidebar">
                    <ul className="mcp-server-list">
                      {MCP_SERVERS.map(s => (
                        <li key={s.sys} className={`mcp-server-item${selectedSys === s.sys ? ' active' : ''}`}>
                          <a
                            href="#"
                            className="mcp-server-link"
                            onClick={e => { e.preventDefault(); handleSelectServer(s.sys) }}
                          >
                            <div className="mcp-server-header">
                              <h3 className="mcp-server-title">{s.nameKo}</h3>
                              <span className="mcp-server-arrow" />
                            </div>
                            <p className="mcp-server-desc">{s.desc}</p>
                          </a>
                        </li>
                      ))}
                    </ul>
                  </div>

                  {/* 우측 — 상세 */}
                  <div className="mcp-content">
                    {!selected ? (
                      <div className="mcp-empty-state-wrap">
                        <div className="mcp-empty-state">
                          <div className="mcp-empty-icon" />
                          <p className="mcp-empty-text">서버 목록을 클릭하면,<br />해당 서버의 상세 정보를 확인할 수 있습니다.</p>
                        </div>
                      </div>
                    ) : (
                      <div className="mcp-detail-container">
                        <div className="mcp-tabs">
                          <button
                            type="button"
                            className={`mcp-tab-btn${tab === 'tool' ? ' active' : ''}`}
                            onClick={() => setTab('tool')}
                          >Tool 정보</button>
                          <button
                            type="button"
                            className={`mcp-tab-btn${tab === 'doc' ? ' active' : ''}`}
                            onClick={openDocTab}
                          >사내 문서 정보</button>
                          <button
                            type="button"
                            className={`mcp-tab-btn${tab === 'project' ? ' active' : ''}`}
                            onClick={openProjectTab}
                          >논문 정보</button>
                        </div>

                        <div className="mcp-list-tb-wrap">
                          {tab === 'tool' ? (
                            <>
                              <div className="mcp-list-header">
                                <div className="mcp-header-left">
                                  <div className="mcp-total-count">총 <strong>{tools.length}</strong>개</div>
                                </div>
                              </div>
                              <div className="mcp-table-wrap">
                                <table className="mcp-table">
                                  <colgroup>
                                    <col style={{ width: '230px' }} />
                                    <col style={{ width: 'auto' }} />
                                  </colgroup>
                                  <thead>
                                    <tr>
                                      <th>Tool</th>
                                      <th>설명</th>
                                    </tr>
                                  </thead>
                                  <tbody>
                                    {pageTools.map(t => (
                                      <tr key={t.name}>
                                        <th className="col-tool">{t.name}</th>
                                        <td className="col-desc">{t.desc}</td>
                                      </tr>
                                    ))}
                                  </tbody>
                                </table>
                              </div>
                              <Pagination page={toolSafePage} totalPages={toolTotalPages} onPage={setToolPage} />
                            </>
                          ) : tab === 'doc' ? (
                            /* 사내 문서 정보 — vdb=127 R&D 첨부 문서 전체(서버 무관). 전체 로드 → 클라 검색+페이지네이션 */
                            renderDocTable({
                              loading: docsLoading,
                              error: docsError,
                              loaded: docs !== null,
                              filtered: docFiltered,
                              pageItems: docPageItems,
                              search: docSearch,
                              onSearchChange: onDocSearchChange,
                              page: docSafePage,
                              totalPages: docTotalPages,
                              onPage: setDocPage,
                            })
                          ) : (
                            /* 논문 정보 — 사내 문서 정보와 구조 동일, 데이터만 다름(§7-4 논문) */
                            renderDocTable({
                              loading: projectsLoading,
                              error: projectsError,
                              loaded: projects !== null,
                              filtered: projectFiltered,
                              pageItems: projectPageItems,
                              search: projectSearch,
                              onSearchChange: onProjectSearchChange,
                              page: projectSafePage,
                              totalPages: projectTotalPages,
                              onPage: setProjectPage,
                            })
                          )}
                        </div>
                      </div>
                    )}
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>

      <Modals
        deleteModal={deleteModal}
        onCloseDelete={() => { setDeleteModal(false); setTargetUid(null) }}
        onConfirmDelete={async () => { if (targetUid) await deleteChat(targetUid); setDeleteModal(false); setTargetUid(null) }}
        changeNameModal={changeNameModal}
        chatTitle={targetTitle}
        onCloseChangeName={() => { setChangeNameModal(false); setTargetUid(null) }}
        onConfirmChangeName={title => { if (targetUid) renameChat(targetUid, title); setChangeNameModal(false); setTargetUid(null) }}
        bulkDeleteModal={bulkDeleteModal}
        onCloseBulkDelete={() => { setBulkDeleteModal(false); setPendingBulkUids([]) }}
        onConfirmBulkDelete={async () => { await bulkDelete(pendingBulkUids); setBulkDeleteModal(false); setPendingBulkUids([]); setBulkResetSignal(s => s + 1) }}
        alertMessage={alertMessage}
        onCloseAlert={() => setAlertMessage(null)}
      />
    </div>
  )
}
