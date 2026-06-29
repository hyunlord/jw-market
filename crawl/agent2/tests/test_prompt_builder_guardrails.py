from __future__ import annotations

from phase_zeta_runner.config import RunnerConfig
from phase_zeta_runner.prompt_builder import build_question_string


def test_prompt_preserves_negative_percent_sign_and_blocks_fabricated_prediction_evidence():
    question = build_question_string({"brand_context": {"brand_name": "테스트"}}, RunnerConfig.default_for_tests())

    assert "음수인 qoq_pct/yoy_pct/growth 계열 비율" in question
    assert "'-' 부호를 보존" in question
    assert "generic '수치 근거'나 가공 숫자를 만들지 말고 evidence를 빈 배열" in question
    assert "prediction stage에서는 forecast_simulation 수치, CI, 불확실성만 서술" in question
    assert "임상/뉴스/급여/허가/출시/경쟁사/약가/정책/제네릭 진입/외부 변수/시장 경쟁 환경 변화/경쟁 심화/시장 변화 모니터링 등 사건성 해석" in question
    assert "prediction body도 예외 없이 6문장 이상 9문장 이하" in question
    assert "UBIST와 IQVIA가 모두 있으면 최종 JSON 전체에서 두 source를 모두 사용" in question
    assert "(ML·IQVIA·...) compact tag" in question
