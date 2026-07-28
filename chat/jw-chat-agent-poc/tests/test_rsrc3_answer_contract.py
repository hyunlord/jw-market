from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from jw_chat_agent_poc.agent_loop.loop import ToolUseAgent
from jw_chat_agent_poc.resolver import BrandResolver
from jw_chat_agent_poc.service import app as service_app
from jw_chat_agent_poc.service import genos_client as genos_module
from jw_chat_agent_poc.service.answer_safety import FAIL_CLOSED_TEXT
from jw_chat_agent_poc.service.genos_client import (
    GenosClient,
    append_source_basis_notice,
)
from jw_chat_agent_poc.service.runtime_provenance import trace_envelope
from jw_chat_agent_poc.tools.metrics import MetricsTool
from jw_chat_agent_poc.tools.query_layer import (
    MartRecord,
    StaticStrategicMartReader,
    StrategicQueryLayer,
)


def test_public_trace_projects_explicit_answer_delivery_nulls() -> None:
    trace = trace_envelope(
        question="설명해줘",
        result={"tool_calls": [], "markdown_response": {"fact_md": "", "data_md": ""}},
        answer="확인된 근거입니다.",
        charts=(),
        timing={"stages": []},
        conversation_id="rsrc3-null",
    )

    assert trace["qa_trace"]["answer_delivery"] == {
        "answer_branch": None,
        "source_notice_attached": None,
    }


def test_requested_source_variants_reach_same_public_deterministic_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    layer = StrategicQueryLayer(
        reader=StaticStrategicMartReader(
            (
                _record("리바로", "ubist", "2026-05", 80.39),
            )
        )
    )
    agent = ToolUseAgent(
        metrics=MetricsTool(mode="fixture", query_layer=layer),
        resolver=BrandResolver(mode="fixture"),
        query_layer=layer,
    )
    client = GenosClient(token="fixture-token")
    monkeypatch.setattr(service_app, "GenosClient", lambda: client)
    monkeypatch.setattr(
        GenosClient,
        "_chat_text",
        lambda *_args, **_kwargs: pytest.fail("deterministic branch must bypass the LLM"),
    )

    answers = {
        question: service_app.compute_final_answer(
            question,
            agent.answer(question),
            f"rsrc3-{index}",
        )
        for index, question in enumerate(
            (
                "리바로 IQVIA 매출 알려줘",
                "리바로 UBIST 매출 알려줘",
                "리바로 매출 알려줘",
            )
        )
    }

    branches = {
        answer.trace["qa_trace"]["answer_delivery"]["answer_branch"]
        for answer in answers.values()
    }
    assert branches == {"genos_markdown_deterministic_market"}
    assert len(answers) == 3


def test_requested_source_notice_survives_the_deterministic_answer_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = _source_variant_answers(monkeypatch)
    iqvia = answers["리바로 IQVIA 매출 알려줘"]
    ubist = answers["리바로 UBIST 매출 알려줘"]
    unspecified = answers["리바로 매출 알려줘"]

    assert "요청은 제조사 출하(IQVIA NSA) 기준이며" in iqvia.text
    assert "이 응답은 원외 처방(UBIST) 기준입니다" in iqvia.text
    notice_line = next(
        line.lstrip("- ")
        for line in iqvia.text.splitlines()
        if line.lstrip("- ").startswith("요청은 ")
    )
    assert all(
        token not in notice_line
        for token in ("없습니다", "없음", "없다", "미보유", "존재하지")
    )
    assert "측정 대상이 다른" not in ubist.text
    assert "측정 대상이 다른" not in unspecified.text
    assert _sha(iqvia.text) != _sha(ubist.text)
    assert _sha(iqvia.text) != _sha(unspecified.text)
    assert _sha(ubist.text) == _sha(unspecified.text)
    assert iqvia.trace["qa_trace"]["answer_delivery"]["source_notice_attached"] is True
    assert ubist.trace["qa_trace"]["answer_delivery"]["source_notice_attached"] is False
    assert unspecified.trace["qa_trace"]["answer_delivery"]["source_notice_attached"] is False


def test_general_view_early_return_mirrors_source_notice_attachment() -> None:
    notice = (
        "요청은 제조사 출하(IQVIA NSA) 기준이며, 이 응답은 원외 처방(UBIST) 기준입니다. "
        "측정 대상이 다른 두 기준은 유통 재고, 병원 직거래, 반품, 원내 처방의 영향으로 값이 서로 다를 수 있습니다."
    )
    result = {
        "general_view_ready": True,
        "answer": "일반뷰 근거를 반환했습니다.",
        "tool_calls": [],
        "sources": ["UBIST"],
        "markdown_response": {
            "fact_md": "",
            "data_md": "",
            "notice_md": f"## 안내\n\n- {notice}",
        },
    }

    final = service_app.compute_final_answer(
        "리바로 IQVIA 일반뷰로는?",
        result,
        "rsrc3-general-view",
    )

    assert notice in final.text
    assert final.trace["qa_trace"]["answer_delivery"] == {
        "answer_branch": "general_view_ready",
        "source_notice_attached": True,
    }


@pytest.mark.parametrize("section_heading", ("## 출처", "## 처리 시간"))
def test_source_notice_is_inserted_before_source_sections_without_digits(
    section_heading: str,
) -> None:
    notice = (
        "요청은 제조사 출하(IQVIA NSA) 기준이며, 이 응답은 원외 처방(UBIST) 기준입니다. "
        "측정 대상이 다른 두 기준은 유통 재고, 병원 직거래, 반품, 원내 처방의 영향으로 값이 서로 다를 수 있습니다."
    )
    answer, attached = append_source_basis_notice(
        f"확인된 답변입니다.\n\n{section_heading}\n\n| source |\n| --- |",
        {"notice_md": f"- {notice}"},
    )

    assert attached is True
    assert answer.index(notice) < answer.index(section_heading)
    assert not any(character.isdigit() for character in notice)


def test_fulfilled_iqvia_request_keeps_answer_without_notice(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    layer = StrategicQueryLayer(
        reader=StaticStrategicMartReader(
            (
                _record(
                    "아일리아",
                    "iqvia_nsa",
                    "2026-Q1",
                    218.7,
                    market_id="S01P0",
                ),
            )
        )
    )
    agent = ToolUseAgent(
        metrics=MetricsTool(mode="fixture", query_layer=layer),
        resolver=_fixture_resolver(tmp_path, "아일리아", "S01P0"),
        query_layer=layer,
    )
    client = GenosClient(token="fixture-token")
    monkeypatch.setattr(service_app, "GenosClient", lambda: client)
    monkeypatch.setattr(
        GenosClient,
        "_chat_text",
        lambda *_args, **_kwargs: pytest.fail("deterministic branch must bypass the LLM"),
    )

    agent_result = agent.answer("아일리아 IQVIA 매출 알려줘")
    metrics = agent_result["agent_loop_metrics"]
    assert metrics["requested_source"] == "iqvia_nsa"
    assert metrics["served_source"] == "iqvia_nsa"

    final = service_app.compute_final_answer(
        "아일리아 IQVIA 매출 알려줘",
        agent_result,
        "rsrc3-iqvia-served",
    )

    assert "측정 대상이 다른" not in final.text
    assert final.trace["qa_trace"]["answer_delivery"]["source_notice_attached"] is False


def test_dual_source_market_serves_requested_iqvia_without_notice(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    layer = StrategicQueryLayer(
        reader=StaticStrategicMartReader(
            (
                _record("가드렛", "ubist", "2026-05", 12.3, market_id="ml_003"),
                _record("가드렛", "iqvia_nsa", "2026-Q1", 14.5, market_id="ml_003"),
            )
        )
    )
    agent = ToolUseAgent(
        metrics=MetricsTool(mode="fixture", query_layer=layer),
        resolver=_fixture_resolver(tmp_path, "가드렛", "ml_003"),
        query_layer=layer,
    )
    client = GenosClient(token="fixture-token")
    monkeypatch.setattr(service_app, "GenosClient", lambda: client)
    monkeypatch.setattr(
        GenosClient,
        "_chat_text",
        lambda *_args, **_kwargs: pytest.fail("deterministic branch must bypass the LLM"),
    )

    agent_result = agent.answer("가드렛 IQVIA 매출 알려줘")
    metrics = agent_result["agent_loop_metrics"]
    assert metrics["requested_source"] == "iqvia_nsa"
    assert metrics["served_source"] == "iqvia_nsa"

    final = service_app.compute_final_answer(
        "가드렛 IQVIA 매출 알려줘",
        agent_result,
        "rsrc3-dual-source-iqvia",
    )

    assert "측정 대상이 다른" not in final.text
    assert final.trace["qa_trace"]["answer_delivery"]["source_notice_attached"] is False


def test_nine_genos_return_sites_record_distinct_public_enums(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[str] = []
    base = {
        "answer": "검증 답변",
        "tool_calls": [],
        "markdown_response": {
            "fact_md": "검증 근거",
            "data_md": "검증 근거",
            "allowed_numbers": [],
        },
    }

    with monkeypatch.context() as patch:
        patch.setattr(genos_module, "deterministic_single_period_sales_answer", lambda *_args: "단일 기간")
        observed.append(_run_branch(GenosClient(token="token"), base))

    tool_result = {
        **base,
        "router_diagnostics": {"mode": "tool_use_agent"},
    }
    with monkeypatch.context() as patch:
        patch.setattr(genos_module, "deterministic_single_period_sales_answer", lambda *_args: "")
        patch.setattr(genos_module, "_verified_tool_use_agent_answer", lambda *_args: FAIL_CLOSED_TEXT)
        observed.append(_run_branch(GenosClient(token=None), tool_result))

    with monkeypatch.context() as patch:
        patch.setattr(genos_module, "deterministic_single_period_sales_answer", lambda *_args: "")
        patch.setattr(genos_module, "_verified_tool_use_agent_answer", lambda *_args: "검증 답변")
        patch.setattr(genos_module, "_has_combined_clinical_registry_evidence", lambda *_args: True)
        patch.setattr(genos_module, "finalized_fallback_fact_answer", lambda *_args: "임상 답변")
        observed.append(_run_branch(GenosClient(token="token"), tool_result))

    with monkeypatch.context() as patch:
        patch.setattr(genos_module, "deterministic_single_period_sales_answer", lambda *_args: "")
        patch.setattr(genos_module, "_verified_tool_use_agent_answer", lambda *_args: "검증 답변")
        patch.setattr(genos_module, "_has_combined_clinical_registry_evidence", lambda *_args: False)
        patch.setattr(genos_module, "_deterministic_tool_use_external_answer", lambda *_args: "외부 답변")
        observed.append(_run_branch(GenosClient(token="token"), tool_result))

    with monkeypatch.context() as patch:
        patch.setattr(genos_module, "deterministic_single_period_sales_answer", lambda *_args: "")
        patch.setattr(genos_module, "_verified_tool_use_agent_answer", lambda *_args: "검증 답변")
        observed.append(_run_branch(GenosClient(token=None), tool_result))

    with monkeypatch.context() as patch:
        patch.setattr(genos_module, "deterministic_single_period_sales_answer", lambda *_args: "")
        patch.setattr(genos_module, "_requires_deterministic_external_relay", lambda *_args: True)
        patch.setattr(genos_module, "_deterministic_external_relay_answer", lambda *_args: "릴레이 답변")
        observed.append(_run_branch(GenosClient(token="token"), base))

    with monkeypatch.context() as patch:
        patch.setattr(genos_module, "deterministic_single_period_sales_answer", lambda *_args: "")
        patch.setattr(genos_module, "_requires_deterministic_external_relay", lambda *_args: False)
        patch.setattr(genos_module, "_deterministic_concentration_answer", lambda *_args: "집중도 답변")
        observed.append(_run_branch(GenosClient(token="token"), base))

    with monkeypatch.context() as patch:
        patch.setattr(genos_module, "deterministic_single_period_sales_answer", lambda *_args: "")
        patch.setattr(genos_module, "_requires_deterministic_external_relay", lambda *_args: False)
        patch.setattr(genos_module, "_deterministic_concentration_answer", lambda *_args: "")
        patch.setattr(genos_module, "deterministic_top_n_share_answer", lambda *_args: "상위 답변")
        observed.append(_run_branch(GenosClient(token="token"), base))

    with monkeypatch.context() as patch:
        patch.setattr(genos_module, "deterministic_single_period_sales_answer", lambda *_args: "")
        observed.append(_run_branch(GenosClient(token=None), base))

    assert observed == [
        "genos_single_period_sales",
        "genos_tool_fail_closed",
        "genos_tool_clinical_registry",
        "genos_tool_external",
        "genos_tool_verified_fallback",
        "genos_external_relay",
        "genos_concentration",
        "genos_top_n",
        "genos_cache",
    ]


def _run_branch(client: GenosClient, result: dict[str, object]) -> str:
    assert "".join(client.stream_answer("리바로 매출 알려줘", result))
    return client.answer_branch_events[-1]


def _source_variant_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, service_app.FinalAnswer]:
    layer = StrategicQueryLayer(
        reader=StaticStrategicMartReader(
            (
                _record("리바로", "ubist", "2026-05", 80.39),
            )
        )
    )
    agent = ToolUseAgent(
        metrics=MetricsTool(mode="fixture", query_layer=layer),
        resolver=BrandResolver(mode="fixture"),
        query_layer=layer,
    )
    client = GenosClient(token="fixture-token")
    monkeypatch.setattr(service_app, "GenosClient", lambda: client)
    monkeypatch.setattr(
        GenosClient,
        "_chat_text",
        lambda *_args, **_kwargs: pytest.fail("deterministic branch must bypass the LLM"),
    )
    return {
        question: service_app.compute_final_answer(
            question,
            agent.answer(question),
            f"rsrc3-source-{index}",
        )
        for index, question in enumerate(
            (
                "리바로 IQVIA 매출 알려줘",
                "리바로 UBIST 매출 알려줘",
                "리바로 매출 알려줘",
            )
        )
    }


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _record(
    brand: str,
    source: str,
    period: str,
    value_eok: float,
    *,
    market_id: str = "ml_006",
) -> MartRecord:
    return MartRecord(
        ml_id=market_id,
        brand_name=brand,
        source=source,
        measure="sales",
        metric_history={
            "2026-03": {
                "raw_value": (value_eok - 2) * 100_000_000,
                "ms": 9.5,
                "source_status": "OK",
            },
            "2026-04": {
                "raw_value": (value_eok - 1) * 100_000_000,
                "ms": 9.8,
                "source_status": "OK",
            },
            period: {
                "raw_value": value_eok * 100_000_000,
                "ms": 10.0,
                "source_status": "OK",
            }
        },
        channel_data={},
        specialty_data={},
        dimension_data={},
        by_dimension={"company": "테스트제약", "molecule": f"{brand}성분"},
    )


def _fixture_resolver(
    tmp_path: Path,
    brand: str,
    market_id: str,
) -> BrandResolver:
    fixture_path = tmp_path / "brand_catalog.json"
    fixture_path.write_text(
        json.dumps(
            [
                {
                    "canonical_brand": brand,
                    "aliases": [],
                    "audit_code": f"fixture_{brand}",
                    "molecule_en": [],
                    "atc": [],
                    "edi_code": None,
                    "item_seq": None,
                    "market_id": market_id,
                    "market_name": market_id,
                    "evidence_source": "test fixture",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return BrandResolver(mode="fixture", fixture_path=fixture_path)
