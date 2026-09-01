export default function MaintenancePage() {
  return (
    <div className="wrap">
      <div className="error-container">
        <div className="error-icon">
          <svg xmlns="http://www.w3.org/2000/svg" width="120" height="120" viewBox="0 0 120 120" fill="none">
            <path d="M37 38L31 60L27.5 71H92.5L89 60L83 38H37Z" fill="#E16349" />
            <path d="M10 104H110" stroke="#DFE2E7" strokeWidth="10" strokeLinecap="round" />
            <path d="M100 104L77.6077 21.8951C76.6585 18.4146 73.4972 16 69.8896 16H50.1104C46.5028 16 43.3415 18.4146 42.3923 21.8951L20 104" stroke="#DFE2E7" strokeWidth="10" />
          </svg>
        </div>
        <h1 className="error-title">시스템 점검 안내</h1>
        <p className="error-description">
          현재 시스템 점검으로 인해 아래와 같이<br />
          이용이 일시적으로 제한될 예정이오니 참고하시기 바랍니다.
        </p>
        <div className="error-notice">
          <dl className="notice-item">
            <dt className="badge">일정</dt>
            <dd className="content">YYYY.MM.DD. hh:mm ~ hh:mm</dd>
          </dl>
          <dl className="notice-item">
            <dt className="badge">내용</dt>
            <dd className="content">시스템 보안패치 작업</dd>
          </dl>
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
