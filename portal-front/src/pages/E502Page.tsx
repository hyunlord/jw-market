import { useNavigate } from 'react-router-dom'

export default function E502Page() {
  const navigate = useNavigate()

  return (
    <div className="wrap">
      <div className="error-container">
        <div className="error-icon">
          <svg xmlns="http://www.w3.org/2000/svg" width="120" height="120" viewBox="0 0 120 120" fill="none">
            <path d="M29.999 11.6667H64.4766C66.6866 11.6668 68.8064 12.5445 70.3691 14.1072L83.1309 26.8689L95.8916 39.6306C97.4544 41.1934 98.333 43.3131 98.333 45.5232V99.9998C98.333 104.602 94.6014 108.334 89.999 108.334H29.999C25.3968 108.334 21.666 104.602 21.666 99.9998V19.9998C21.6662 15.3976 25.3969 11.6669 29.999 11.6667Z" stroke="#DFE2E7" strokeWidth="10"/>
            <path d="M66.666 36.6668V14.0238C66.666 12.539 68.4613 11.7954 69.5112 12.8453L97.1542 40.4883C98.2041 41.5382 97.4605 43.3335 95.9757 43.3335H73.3327C69.6508 43.3335 66.666 40.3487 66.666 36.6668Z" fill="#DFE2E7" stroke="#DFE2E7" strokeWidth="6.66667" strokeLinejoin="round"/>
            <path d="M60 52V72" stroke="#DFE2E7" strokeWidth="10" strokeLinecap="round"/>
            <path d="M60 88H60.05" stroke="#DFE2E7" strokeWidth="10" strokeLinecap="round" strokeLinejoin="round"/>
          </svg>
        </div>
        <h1 className="error-title">페이지를 찾을 수 없습니다</h1>
        <p className="error-description">
          방문하시려는 페이지의 주소가 잘못 입력되었거나 페이지가 삭제 또는 변경되어<br />
          요청하신 페이지를 찾을 수 없습니다. 입력하신 주소를 다시 확인해 주세요.
        </p>
        <div className="error-actions">
          <button type="button" className="btn-prev" onClick={() => navigate(-1)}>이전 페이지로</button>
        </div>
        <div className="error-footer">
          <p>지속적인 문제가 발생할 경우, 담당자에게 문의해 주세요.</p>
          <p className="contact-info">
            AX실 심명선매니저(<span className="tel">02-840-6611</span> / <span className="tel">010-3041-5473</span>)
          </p>
        </div>
      </div>
    </div>
  )
}
