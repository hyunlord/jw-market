from __future__ import annotations

from pathlib import Path

import pytest

from jw_chat_agent_poc.orchestrator.response_format_contract import (
    ClaimBindingLookup,
    ResponseFormatMode,
    apply_response_format_contract,
    evaluate_response_format_contract,
)


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "response_format_contract"


def _fixture(name: str) -> str:
    return (FIXTURE_ROOT / f"{name}.md").read_text(encoding="utf-8").strip()


class _BindingLookup(ClaimBindingLookup):
    def __init__(self, bound: bool) -> None:
        self._bound = bound

    def has_binding(self, _claim_text: str) -> bool:
        return self._bound


@pytest.mark.parametrize(
    ("code", "question", "fixture"),
    (
        ("C1_TABLE_ONLY", "리바로 매출 추이를 분석해줘", "c1_violation"),
        ("C2_EMPTY_ISSUE_SECTION", "리바로 핵심 이슈를 분석해줘", "c2_violation"),
        ("C3_UNBOUND_CAUSAL", "리바로 매출 증가 원인을 분석해줘", "c3_violation"),
        ("C4_CROSS_SOURCE_AGGREGATION", "두 출처를 비교해줘", "c4_violation"),
        ("C5_PROVENANCE_INCOMPLETE", "리바로 매출을 알려줘", "c5_violation"),
    ),
)
def test_red_violation_fixture_is_detected(code: str, question: str, fixture: str) -> None:
    report = evaluate_response_format_contract(question, _fixture(fixture))

    assert code in report.violation_codes


@pytest.mark.parametrize(
    ("question", "fixture"),
    (
        ("리바로 매출 추이를 분석해줘", "c1_pass"),
        ("미보유 처방 데이터의 분석 한계를 알려줘", "c2_pass"),
        ("리바로 매출과 뉴스를 대조해줘", "c3_pass"),
        ("UBIST와 IQVIA를 비교해줘", "c4_pass"),
        ("리바로 매출을 알려줘", "c5_pass"),
    ),
)
def test_green_fixture_passes(question: str, fixture: str) -> None:
    report = evaluate_response_format_contract(question, _fixture(fixture))

    assert report.violation_codes == ()


def test_c1_explicit_table_only_request_is_exempt() -> None:
    report = evaluate_response_format_contract("월별 매출을 표만 보여줘", _fixture("c1_violation"))

    assert "C1_TABLE_ONLY" not in report.violation_codes


def test_c1_table_intro_without_change_narrative_is_still_a_violation() -> None:
    answer = "아래 표를 참고하세요.\n\n" + _fixture("c1_violation")

    report = evaluate_response_format_contract("리바로 매출 추이를 분석해줘", answer)

    assert "C1_TABLE_ONLY" in report.violation_codes


def test_off_is_dormant_even_for_violation_fixture(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JW_CHAT_RESPONSE_FORMAT_CONTRACT", raising=False)
    answer = _fixture("c4_violation")

    result = apply_response_format_contract("두 출처를 비교해줘", answer)

    assert result.answer == answer
    assert result.report.mode is ResponseFormatMode.OFF
    assert result.report.violation_codes == ()
    assert result.report.blocked is False


def test_shadow_reports_without_changing_answer() -> None:
    answer = _fixture("c1_violation")

    result = apply_response_format_contract(
        "리바로 매출 추이를 분석해줘",
        answer,
        mode=ResponseFormatMode.SHADOW,
    )

    assert result.answer == answer
    assert result.report.violation_codes == ("C1_TABLE_ONLY",)
    assert result.report.blocked is False


def test_c1_enforce_blocks_table_only_failure_injection() -> None:
    result = apply_response_format_contract(
        "리바로 매출 추이를 분석해줘",
        _fixture("c1_violation"),
        mode=ResponseFormatMode.ENFORCE,
    )

    assert result.report.blocked is True
    assert "C1_TABLE_ONLY" in result.report.blocked_codes
    assert "월별 매출" not in result.answer


def test_c2_enforce_omits_empty_issue_section_but_keeps_real_content() -> None:
    result = apply_response_format_contract(
        "리바로 핵심 이슈를 분석해줘",
        _fixture("c2_violation"),
        mode=ResponseFormatMode.ENFORCE,
    )

    assert result.report.blocked is False
    assert "핵심 이슈" not in result.answer
    assert "확인된 지표" in result.answer
    assert "omit_empty_issue_section" in result.report.actions


def test_c2_enforce_preserves_five_step_data_absence_contract() -> None:
    answer = _fixture("c2_pass")

    result = apply_response_format_contract(
        "미보유 처방 데이터의 분석 한계를 알려줘",
        answer,
        mode=ResponseFormatMode.ENFORCE,
    )

    assert result.answer == answer
    assert result.report.violation_codes == ()


def test_c3_without_binding_provider_remains_detection_only_in_enforce() -> None:
    answer = _fixture("c3_violation")

    result = apply_response_format_contract(
        "리바로 매출 증가 원인을 분석해줘",
        answer,
        mode=ResponseFormatMode.ENFORCE,
    )

    assert result.answer == answer
    assert result.report.blocked is False
    assert result.report.binding_state == "unavailable_shadow"
    assert "C3_UNBOUND_CAUSAL" in result.report.violation_codes


def test_c3_binding_interface_blocks_only_confirmed_unbound_claim() -> None:
    answer = _fixture("c3_violation")

    unbound = apply_response_format_contract(
        "리바로 매출 증가 원인을 분석해줘",
        answer,
        mode=ResponseFormatMode.ENFORCE,
        claim_bindings=_BindingLookup(False),
    )
    bound = apply_response_format_contract(
        "리바로 매출 증가 원인을 분석해줘",
        answer,
        mode=ResponseFormatMode.ENFORCE,
        claim_bindings=_BindingLookup(True),
    )

    assert unbound.report.blocked is True
    assert "C3_UNBOUND_CAUSAL" in unbound.report.blocked_codes
    assert bound.report.violation_codes == ()
    assert bound.answer == answer


def test_c4_enforce_blocks_cross_source_aggregation_failure_injection() -> None:
    result = apply_response_format_contract(
        "두 출처를 비교해줘",
        _fixture("c4_violation"),
        mode=ResponseFormatMode.ENFORCE,
    )

    assert result.report.blocked is True
    assert "C4_CROSS_SOURCE_AGGREGATION" in result.report.blocked_codes
    assert "220억원" not in result.answer


def test_c5_enforce_blocks_incomplete_provenance_failure_injection() -> None:
    result = apply_response_format_contract(
        "리바로 매출을 알려줘",
        _fixture("c5_violation"),
        mode=ResponseFormatMode.ENFORCE,
    )

    assert result.report.blocked is True
    assert "C5_PROVENANCE_INCOMPLETE" in result.report.blocked_codes


def test_c5_classifies_upstream_missing_fields() -> None:
    report = evaluate_response_format_contract(
        "리바로 매출을 알려줘",
        _fixture("c5_violation"),
        tool_calls=({"tool": "get_brand_metric", "source": "UBIST", "render_data": {}},),
        sources=("UBIST",),
    )

    violation = next(item for item in report.violations if item.code == "C5_PROVENANCE_INCOMPLETE")
    assert violation.origin == "upstream"


def test_c5_classifies_assembler_projection_when_structured_call_has_all_fields() -> None:
    call = {
        "tool": "get_brand_metric",
        "source": "UBIST",
        "render_data": {
            "metric": "sales",
            "period": "2026-04",
            "total_brands_in_market": 470,
            "query_spec": {
                "source": "UBIST",
                "view": "general",
                "market": "C10A1",
                "market_name": "ATC4 C10A1",
            },
        },
    }

    report = evaluate_response_format_contract(
        "리바로 매출을 알려줘",
        _fixture("c5_violation"),
        tool_calls=(call,),
        sources=("UBIST",),
    )

    violation = next(item for item in report.violations if item.code == "C5_PROVENANCE_INCOMPLETE")
    assert violation.origin == "assembler"


def test_public_final_answer_boundary_applies_shadow_contract_to_every_internal_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jw_chat_agent_poc.service import app as service_app

    answer = _fixture("c1_violation")
    internal = service_app.FinalAnswer(
        text=answer,
        charts=[],
        timing={},
        trace={"internal": True},
        sources=(),
        conversation_id="response-format-shadow",
    )
    monkeypatch.setenv("JW_CHAT_RESPONSE_FORMAT_CONTRACT", "SHADOW")
    monkeypatch.setattr(service_app, "_compute_final_answer", lambda *_args: internal)

    final = service_app.compute_final_answer("리바로 매출 추이를 분석해줘", {})

    assert final.text == answer
    assert final.trace["response_format_contract"]["mode"] == "SHADOW"
    assert final.trace["response_format_contract"]["violations"][0]["code"] == "C1_TABLE_ONLY"


def test_public_final_answer_boundary_enforces_empty_section_omission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jw_chat_agent_poc.service import app as service_app

    internal = service_app.FinalAnswer(
        text=_fixture("c2_violation"),
        charts=[],
        timing={},
        trace={},
        sources=(),
        conversation_id="response-format-enforce",
    )
    monkeypatch.setenv("JW_CHAT_RESPONSE_FORMAT_CONTRACT", "ENFORCE")
    monkeypatch.setattr(service_app, "_compute_final_answer", lambda *_args: internal)

    final = service_app.compute_final_answer("리바로 핵심 이슈를 분석해줘", {})

    assert "핵심 이슈" not in final.text
    assert "확인된 지표" in final.text
    assert final.trace["response_format_contract"]["actions"] == ("omit_empty_issue_section",)


def test_runtime_version_includes_response_format_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    from jw_chat_agent_poc.service.runtime_provenance import version_payload

    payload = version_payload()

    assert payload["policy_versions"]["response_format_contract_version"].startswith("sha256:")
