from __future__ import annotations

from phase_zeta_runner.config import RunnerConfig
from phase_zeta_runner.prompt_builder import build_question_string


def test_prompt_preserves_negative_percent_sign_and_blocks_fabricated_prediction_evidence():
    question = build_question_string({"brand_context": {"brand_name": "테스트"}}, RunnerConfig.default_for_tests())

    assert "음수인 qoq_pct/yoy_pct/growth 계열 비율" in question
    assert "'-' 부호를 보존" in question
    assert "generic '수치 근거'나 가공 숫자를 만들지 말고 evidence를 빈 배열" in question
    assert "forecast_simulation 수치와 95% CI를 유지하되" in question
    assert "horizon의 방향성" in question
    assert "CI 폭 변화와 장단기 불확실성" in question
    assert "현재 시장/처방 지표와 연결한 시사점" in question
    assert "prediction은 미래 전망의 해석" in question
    assert "cause의 과거 원인 분석이나 recommendation의 실행 지시와 중복하지 마세요" in question
    assert "bundle event가 있을 때만 prediction.evidence에 연결" in question
    assert "해석 문장을 최소 3개 포함" in question
    assert "forecast와 market_views 수치 자체의 의미를 해석" in question
    assert "prediction body도 예외 없이 6문장 이상 9문장 이하" in question
    assert "UBIST와 IQVIA가 모두 있으면 최종 JSON 전체에서 두 source를 모두 사용" in question
    assert "(ML·IQVIA·...) compact tag" in question
