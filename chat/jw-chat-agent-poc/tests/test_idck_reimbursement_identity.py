from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest
from pydantic import BaseModel

from jw_chat_agent_poc.resolver import BrandResolver
from jw_chat_agent_poc.service.genos_client import append_source_basis_notice
from jw_chat_agent_poc.tool_use.executor import AgentExecutor
from jw_chat_agent_poc.tool_use.integration import _agent_result_payload
from jw_chat_agent_poc.tool_use.provider import ToolChoice
from jw_chat_agent_poc.tool_use.reimbursement_evidence import reimbursement_envelope
from jw_chat_agent_poc.tool_use.specs import ToolSpec
from jw_chat_agent_poc.tools.external.hira_reimbursement import (
    CacheLookupStatus,
    CacheStatus,
    ReimbursementCriterion,
    ReimbursementLookupResult,
)


LIVALOZET_NOTICE = (
    "Ezetimibe + Atorvastatin 복합경구제(품명: 아토젯정 등), "
    "Ezetimibe + pitavastatin calcium 복합경구제(품명: 리바로젯정 등), "
    "Ezetimibe + Rosuvastatin calcium 복합경구제(품명: 로수젯정 등)"
)
IDENTITY_NOTICE = (
    "요청하신 리바로 단일제 급여기준은 제공할 수 없습니다. "
    "연결된 고시가 성분 구성이 다른 복합제 기준으로 확인되어, "
    "요청하신 제품의 기준으로 사용할 수 없습니다. "
    "확인이 필요하시면 심사평가원 고시에서 해당 제품명으로 직접 확인해 주세요."
)


def _lookup(raw_text: str = LIVALOZET_NOTICE) -> ReimbursementLookupResult:
    return ReimbursementLookupResult(
        ok=True,
        cache_status=CacheStatus.FRESH,
        retrieval="cache",
        data=ReimbursementCriterion(
            brand_name="리바로",
            title="고지혈증 치료제 급여기준",
            raw_text=raw_text,
            source_date="2021-10-01",
            collected_at=datetime(2026, 7, 28, tzinfo=UTC),
            notice_number="제2021-245호",
            source_url="https://www.hira.or.kr/rc/example.do",
        ),
        cache_lookup_status=CacheLookupStatus.HIT,
    )


def test_projector_detects_livalo_notice_identity_mismatch() -> None:
    envelope = reimbursement_envelope(
        _lookup(),
        subject="리바로",
        resolver=BrandResolver(mode="fixture"),
    )

    assert envelope.ok is False
    assert envelope.error_code == "IDENTITY_MISMATCH"
    assert envelope.raw["identity_status"] == "mismatch"
    assert envelope.raw["identity_match"] is False
    assert envelope.raw["identity_notice"] == IDENTITY_NOTICE
    assert envelope.error_message == IDENTITY_NOTICE
    assert envelope.evidence == ()


def test_matching_livalozet_identity_does_not_add_notice() -> None:
    envelope = reimbursement_envelope(
        _lookup(),
        subject="리바로젯",
        resolver=BrandResolver(mode="fixture"),
    )

    assert envelope.raw["identity_status"] == "match"
    assert envelope.raw["identity_match"] is True
    assert envelope.raw["identity_notice"] == ""
    assert envelope.evidence[0].source_locator == LIVALOZET_NOTICE


def test_identity_notice_reaches_final_answer_without_numbers() -> None:
    answer, attached = append_source_basis_notice(
        "조회된 급여기준을 안내합니다.\n\n## 출처\n\n- HIRA",
        {"notice_md": IDENTITY_NOTICE},
    )

    assert attached is True
    assert IDENTITY_NOTICE in answer
    assert answer.index(IDENTITY_NOTICE) < answer.index("## 출처")


def test_unverifiable_notice_identity_preserves_existing_evidence() -> None:
    raw_text = "관련 약제 투여 후 이상반응 관리 기준을 안내한다."
    envelope = reimbursement_envelope(
        _lookup(raw_text),
        subject="악템라",
        resolver=BrandResolver(mode="fixture"),
    )

    assert envelope.raw["identity_status"] == "unverifiable"
    assert envelope.raw["identity_match"] is None
    assert envelope.raw["identity_notice_required"] is False
    assert envelope.evidence[0].source_locator == raw_text


@pytest.mark.parametrize(
    ("subject", "raw_text"),
    (
        ("헴리브라", "Emicizumab 주사제 (품명: 헴리브라피하주사 30mg 등)"),
        ("악템라", "Tocilizumab 주사제 (품명: 악템라주 등)"),
        ("페린젝트", "철분 주사제 (품명: 페린젝트주 등)"),
    ),
)
def test_matching_products_are_not_overblocked(subject: str, raw_text: str) -> None:
    envelope = reimbursement_envelope(
        _lookup(raw_text),
        subject=subject,
        resolver=BrandResolver(mode="fixture"),
    )

    assert envelope.raw["identity_status"] == "match"
    assert envelope.raw["identity_match"] is True
    assert envelope.raw["identity_notice_required"] is False
    assert envelope.evidence[0].source_locator == raw_text


def test_unindexed_reimbursement_failure_remains_unchanged() -> None:
    result = ReimbursementLookupResult(
        ok=False,
        cache_status=CacheStatus.NOT_FOUND,
        retrieval="typed_unavailable",
        data=None,
        error_code="NO_EVIDENCE",
        cache_lookup_status=CacheLookupStatus.BRAND_UNMATCHED,
    )

    envelope = reimbursement_envelope(
        result,
        subject="아일리아",
        resolver=BrandResolver(mode="fixture"),
    )

    assert envelope.ok is False
    assert envelope.error_code == "NO_EVIDENCE"
    assert envelope.error_message == "해당 브랜드는 아직 급여기준 색인 대상이 아닙니다."
    assert "identity_status" not in envelope.raw


class _NoInput(BaseModel):
    pass


@dataclass(slots=True)
class _OneChoice:
    calls: int = 0

    def choose(self, *, user_text: str, messages: list[dict], tools: list[dict]) -> ToolChoice:
        del user_text, messages, tools
        self.calls += 1
        return ToolChoice(
            "hira_reimbursement_criteria",
            {},
            "lookup",
            call_id="idck-call",
        )


def test_identity_observability_crosses_safe_envelope_without_raw_cache_fields() -> None:
    envelope = reimbursement_envelope(
        _lookup(),
        subject="리바로",
        resolver=BrandResolver(mode="fixture"),
    )
    provider = _OneChoice()
    spec = ToolSpec(
        name="hira_reimbursement_criteria",
        description="verified reimbursement fixture",
        input_model=_NoInput,
        execute=lambda _payload: envelope,
        timeout_s=1.0,
        tags=("external", "hira"),
    )

    result = AgentExecutor(provider=provider).run(
        user_text="리바로 급여기준",
        tools=(spec,),
    )
    render_data = result.tool_calls[0]["render_data"]
    payload = _agent_result_payload("리바로 급여기준", result)

    assert render_data["identity_status"] == "mismatch"
    assert render_data["identity_match"] is False
    assert render_data["identity_notice_required"] is True
    assert render_data["identity_notice"] == IDENTITY_NOTICE
    assert "notice_number" not in render_data
    assert payload["markdown_response"]["notice_md"] == IDENTITY_NOTICE
