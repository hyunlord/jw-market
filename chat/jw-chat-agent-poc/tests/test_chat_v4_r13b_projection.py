from __future__ import annotations

from copy import deepcopy
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from jw_chat_agent_poc.service.trace_transport import trace_for_transport
from jw_chat_agent_poc.service import app as service_app
from jw_chat_agent_poc.service.app import FinalAnswer
from jw_chat_agent_poc.service.v4.lossless_contracts import (
    CoverageLedger,
    EvidenceRecord,
    EvidenceSet,
    RenderNode,
)
from jw_chat_agent_poc.service.v4.patent import build_patent_lane_payload
from jw_chat_agent_poc.service.v4.scope_provenance import (
    ProjectionInputError,
    build_scope_provenance_projection,
    validate_scope_provenance_projection,
)


def _patent_call(*rows: dict[str, object]) -> dict[str, object]:
    return {
        "tool": "mfds_patent_list",
        "source": "patent",
        "render_data": {"items": list(rows), "request_limit": 500},
    }


def _patent_row(
    patent_no: str,
    *,
    item_seq: str = "202105578",
    status: str = "소멸(무효)",
) -> dict[str, object]:
    return {
        "ITEM_SEQ": item_seq,
        "ITEM_NAME": "리바로젯정2/10밀리그램",
        "INGR_ENG_NAME": "Pitavastatin Calcium Hydrate/Ezetimibe",
        "SHAPE": "필름코팅정",
        "CONT_QY": "피타바스타틴칼슘수화물-2.205mg|에제티미브-10mg",
        "PMS_END_DATE": "2021-07-28~2027-07-27",
        "PAGE_GB_NM": "제품특허",
        "DOMESTIC_PATENT_NO": patent_no,
        "DOMESTIC_PATENT_STATUS": status,
        "DOMESTIC_END_DATE": "2021-05-06",
    }


def _evidence(
    source: str,
    record_id: str,
    payload: dict[str, object],
    *,
    pagination_complete: bool = True,
) -> EvidenceSet:
    return EvidenceSet(
        source=source,
        retrieved_at="2026-08-15T00:00:00Z",
        coverage=CoverageLedger(
            records_received=1,
            records_unique=1,
            pagination_complete=pagination_complete,
        ),
        records=(
            EvidenceRecord(
                evidence_id=record_id,
                source=source,
                result_kind="structured_record",
                payload=payload,
            ),
        ),
    )


def test_f1_missing_item_seq_fails_patent_projection_loudly() -> None:
    record = _evidence(
        "patent",
        "patent:KR:10-1244508",
        {
            "lane": "kr_primary",
            "patent_no": "10-1244508",
            "source_record": {
                key: value
                for key, value in _patent_row("10-1244508").items()
                if key != "ITEM_SEQ"
            },
        },
    )

    with pytest.raises(ProjectionInputError, match="ITEM_SEQ"):
        build_scope_provenance_projection((record,), ())


def test_patent_projection_promotes_period_and_preserves_all_official_edges() -> None:
    lanes = build_patent_lane_payload(
        kr_calls=(
            _patent_call(
                _patent_row("10-1244508", item_seq="202105578"),
                _patent_row("10-1244508", item_seq="202105579"),
            ),
        ),
        us_calls=(),
        news_calls=(),
    )
    kr = lanes["kr_primary"]

    assert len(kr["records"]) == 1
    assert kr["records"][0]["product_item_seq"] == "202105578"
    assert kr["records"][0]["pms_period_start"] == "2021-07-28"
    assert kr["records"][0]["pms_period_end"] == "2027-07-27"
    assert kr["records"][0]["listed_end_date"] == "2021-05-06"
    assert kr["records"][0]["event_type"] == "PATENT_INVALIDATED"
    assert kr["records"][0]["authority"] == "KR_LISTED_PATENT"
    assert kr["product_patent_edges"] == [
        {"product_item_seq": "202105578", "patent_no": "10-1244508"},
        {"product_item_seq": "202105579", "patent_no": "10-1244508"},
    ]
    assert len(kr["pms_periods"]) == 2


def test_projection_keeps_manifest_edges_hidden_by_patent_entity_dedup() -> None:
    payload = {
        **_patent_row("10-1244508"),
        "lane": "kr_primary",
        "patent_no": "10-1244508",
        "source_record": _patent_row("10-1244508"),
    }
    evidence = EvidenceSet(
        source="patent",
        query_manifest=(
            {
                "product_patent_edges": [
                    {"product_item_seq": "202105578", "patent_no": "10-1244508"},
                    {"product_item_seq": "202105579", "patent_no": "10-1244508"},
                ],
                "pms_periods": [],
            },
        ),
        retrieved_at="2026-08-15T00:00:00Z",
        coverage=CoverageLedger(records_received=2, records_unique=1),
        records=(
            EvidenceRecord(
                evidence_id="patent:KR:10-1244508",
                source="patent",
                result_kind="structured_patent_record",
                payload=payload,
            ),
        ),
    )

    projection = build_scope_provenance_projection((evidence,), ())

    assert projection["patent_entity_count"] == 1
    assert len(projection["product_patent_edges"]) == 2


def test_projection_covers_eight_lanes_and_marks_relation_incompatibility() -> None:
    fixtures = (
        _evidence("mart", "mart:1", {"brand": "리바로젯", "period": "2026-06"}),
        _evidence("nedrug", "nedrug:1", {"item_name": "리바로젯정"}),
        _evidence("hira", "hira:1", {"disease_code": "D69", "notice_date": "2024"}),
        _evidence("openfda", "openfda:1", {"product": "LivaloZet"}),
        _evidence(
            "clinicaltrials",
            "ct:NCT00000001",
            {"nct_id": "NCT00000001", "start_date": "2025-06-25"},
        ),
        _evidence("web", "web:1", {"title": "리바로젯 보도"}),
        _evidence(
            "patent",
            "patent:KR:10-1244508",
                {
                    "lane": "kr_primary",
                    "patent_no": "10-1244508",
                    "as_of_date": "2026-08-15",
                    "source_record": _patent_row("10-1244508"),
                },
        ),
        _evidence("document", "document:1", {"file_name": "시장보고서.pdf"}),
    )
    original = deepcopy(fixtures)
    relation = RenderNode(
        block_id="narrative:cross-record-relations",
        record_ids=("patent:KR:10-1244508", "openfda:1"),
        text="표면에는 영향을 주지 않는 기존 관계",
    )

    projection = build_scope_provenance_projection(fixtures, (relation,))

    assert tuple(projection["lanes"]) == (
        "mart",
        "nedrug",
        "hira",
        "openfda",
        "clinicaltrials",
        "web",
        "patent",
        "document",
    )
    assert projection["lanes"]["mart"]["records"][0]["deterministic_origin"] == "CODE"
    assert projection["lanes"]["mart"]["records"][0]["source"] == "mart"
    assert projection["lanes"]["mart"]["records"][0]["market_definition_version"] == "UNKNOWN"
    assert projection["lanes"]["hira"]["records"][0]["notice_date_semantics"] == "UNKNOWN"
    assert projection["lanes"]["patent"]["records"][0]["source_lane"] == "kr_primary"
    assert projection["lanes"]["patent"]["records"][0]["pms_status_as_of"] == "IN_PROGRESS"
    assert projection["relations"][0]["compatibility"] == "INCOMPATIBLE"
    assert "jurisdiction" in projection["relations"][0]["reasons"]
    assert fixtures == original


def test_unresolved_relation_operand_is_never_reported_compatible() -> None:
    projection = build_scope_provenance_projection(
        (_evidence("mart", "mart:1", {"brand": "리바로젯"}),),
        (
            RenderNode(
                block_id="narrative:cross-record-relations",
                record_ids=("mart:1", "missing:2"),
                text="기존 관계",
            ),
        ),
    )

    relation = projection["relations"][0]
    assert relation["compatibility"] == "INCOMPATIBLE"
    assert relation["reasons"] == ["unresolved_operand"]
    assert relation["unresolved_record_ids"] == ["missing:2"]


def test_f2_wrong_mart_origin_fails_projection_validation() -> None:
    projection = build_scope_provenance_projection(
        (_evidence("mart", "mart:1", {"brand": "리바로젯"}),),
        (),
    )
    projection["lanes"]["mart"]["records"][0]["deterministic_origin"] = "LLM"

    with pytest.raises(ProjectionInputError, match="deterministic_origin"):
        validate_scope_provenance_projection(projection)


def test_projection_validation_rejects_missing_source_identity() -> None:
    projection = build_scope_provenance_projection(
        (_evidence("mart", "mart:1", {"brand": "리바로젯"}),),
        (),
    )
    del projection["lanes"]["mart"]["records"][0]["source"]

    with pytest.raises(ProjectionInputError, match="source"):
        validate_scope_provenance_projection(projection)


def test_f3_shadow_transport_flag_is_reversible_and_preserves_server_trace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trace = {
        "claim_ir_shadow": {"claim_ir": [{"claim_id": "c1"}]},
        "inspection_detail": {"calls": [{"source": "mart"}]},
    }
    original = deepcopy(trace)

    monkeypatch.delenv("CHAT_CLAIM_IR_SHADOW_TRANSPORT_ENABLED", raising=False)
    excluded = trace_for_transport(trace)
    monkeypatch.setenv("CHAT_CLAIM_IR_SHADOW_TRANSPORT_ENABLED", "true")
    included = trace_for_transport(trace)

    assert "claim_ir_shadow" not in excluded
    assert excluded["inspection_detail"] == trace["inspection_detail"]
    assert included == trace
    assert trace == original


def test_sse_excludes_shadow_without_mutating_server_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trace = {
        "claim_ir_shadow": {"claim_ir": [{"claim_id": "c1"}]},
        "inspection_detail": {"calls": [{"source": "mart"}]},
    }
    answer = FinalAnswer(
        text="본문",
        charts=[],
        timing={},
        trace=trace,
        sources=("mart",),
        conversation_id="r13b-transport",
    )
    monkeypatch.delenv("CHAT_CLAIM_IR_SHADOW_TRANSPORT_ENABLED", raising=False)

    body = "".join(service_app._sse_events_from_final_answer(answer))

    assert '"claim_ir_shadow"' not in body
    assert '"inspection_detail"' in body
    assert "claim_ir_shadow" in answer.trace


def test_chat_answer_route_excludes_shadow_after_persisting_full_trace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trace = {
        "claim_ir_shadow": {"claim_ir": [{"claim_id": "c1"}]},
        "inspection_detail": {"calls": [{"source": "mart"}]},
    }
    answer = FinalAnswer(
        text="본문",
        charts=[],
        timing={},
        trace=trace,
        sources=("mart",),
        conversation_id="r13b-rest",
    )
    history = MagicMock()
    monkeypatch.setattr(service_app, "_v4_planner_enabled", lambda: True)
    monkeypatch.setattr(service_app, "_run_v4_final_answer", lambda *_args, **_kwargs: answer)
    monkeypatch.delenv("CHAT_CLAIM_IR_SHADOW_TRANSPORT_ENABLED", raising=False)

    response = TestClient(service_app.create_app(history_store=history)).post(
        "/chat/answer",
        json={"question": "리바로 매출 알려줘"},
    )

    assert response.status_code == 200
    assert "claim_ir_shadow" not in response.json()["trace"]
    assert response.json()["trace"]["inspection_detail"] == trace["inspection_detail"]
    assert history.record_turn.call_args.kwargs["trace"] is trace
