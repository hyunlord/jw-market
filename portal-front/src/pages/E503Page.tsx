import { useNavigate } from 'react-router-dom'

export default function E503Page() {
  const navigate = useNavigate()

  return (
    <div className="wrap">
      <div className="error-container">
        <div className="error-icon">
          <svg xmlns="http://www.w3.org/2000/svg" width="120" height="120" viewBox="0 0 120 120" fill="none">
            <circle cx="60" cy="59.9999" r="50" stroke="#DFE2E7" strokeWidth="10"/>
            <path d="M60 37V67" stroke="#DFE2E7" strokeWidth="10" strokeLinecap="round"/>
            <path d="M60 83H60.05" stroke="#DFE2E7" strokeWidth="10" strokeLinecap="round" strokeLinejoin="round"/>
          </svg>
        </div>
        <h1 className="error-title">서비스에 접속할 수 없습니다</h1>
        <p className="error-description">
          기술적인 문제로 일시적으로 접속되지 않습니다.<br />
          잠시 후 다시 시도해 주세요.
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
