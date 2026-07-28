"""IDCK2 — a mismatched reimbursement notice is withheld, not annotated.

Reimbursement criteria get quoted into downstream reports. A warning placed in
front of the notice body does not survive that copy, so the body of another
product's notice must never be built in the first place. Overblocking is the
only failure mode that matters here, so every match, unverifiable and unindexed
shape is asserted to be byte-identical to its pre-change behaviour.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import json

import pytest
from pydantic import BaseModel

from jw_chat_agent_poc.resolver import BrandResolver
from jw_chat_agent_poc.service.genos_client import append_source_basis_notice
from jw_chat_agent_poc.tool_use.executor import AgentExecutor
from jw_chat_agent_poc.tool_use.integration import _agent_result_payload
from jw_chat_agent_poc.tool_use.provider import ToolChoice
from jw_chat_agent_poc.tool_use.reimbursement_evidence import (
    is_reimbursement_identity_notice,
    reimbursement_envelope,
)
from jw_chat_agent_poc.tool_use.specs import ToolSpec
from jw_chat_agent_poc.tools.external.hira_reimbursement import (
    CacheLookupStatus,
    CacheStatus,
    ReimbursementCriterion,
    ReimbursementLookupResult,
)


# Shaped after the production notice 리바로 is currently linked to: three
# ezetimibe combination products, followed by the billing rules that make a
# wrong answer expensive.
COMBINATION_NOTICE = (
    "Ezetimibe + Atorvastatin 복합경구제(품명: 아토젯정 등), "
    "Ezetimibe + pitavastatin calcium 복합경구제(품명: 리바로젯정 등), "
    "Ezetimibe + Rosuvastatin calcium 복합경구제(품명: 로수젯정 등)\n"
    "허가사항 범위 내에서 아래와 같은 기준으로 요양급여를 인정하며, "
    "동 인정기준 이외에는 약값 전액을 환자가 부담한다."
)
# Every token here belongs to the other product's record. None may appear in a
# blocked answer, including the sibling brand name itself.
BODY_TOKENS = (
    "Ezetimibe",
    "리바로젯",
    "아토젯",
    "로수젯",
    "요양급여를 인정",
    "전액을 환자가 부담",
)
BLOCK_NOTICE = (
    "요청하신 리바로 단일제 급여기준은 제공할 수 없습니다. "
    "연결된 고시가 성분 구성이 다른 복합제 기준으로 확인되어, "
    "요청하신 제품의 기준으로 사용할 수 없습니다. "
    "확인이 필요하시면 심사평가원 고시에서 해당 제품명으로 직접 확인해 주세요."
)


def _lookup(raw_text: str, *, brand: str) -> ReimbursementLookupResult:
    return ReimbursementLookupResult(
        ok=True,
        cache_status=CacheStatus.FRESH,
        retrieval="cache",
        data=ReimbursementCriterion(
            brand_name=brand,
            title="고지혈증 치료제 급여기준",
            raw_text=raw_text,
            source_date="2021-10-01",
            collected_at=datetime(2026, 7, 28, tzinfo=UTC),
            notice_number="제2021-245호",
            source_url="https://www.hira.or.kr/rc/example.do",
        ),
        cache_lookup_status=CacheLookupStatus.HIT,
    )


def _envelope(subject: str, raw_text: str):
    return reimbursement_envelope(
        _lookup(raw_text, brand=subject),
        subject=subject,
        resolver=BrandResolver(mode="fixture"),
    )


class _NoInput(BaseModel):
    pass


@dataclass(slots=True)
class _OneChoice:
    calls: int = 0

    def choose(self, *, user_text: str, messages: list[dict], tools: list[dict]) -> ToolChoice:
        del user_text, messages, tools
        self.calls += 1
        return ToolChoice("hira_reimbursement_criteria", {}, "lookup", call_id="idck2-call")


def _payload(subject: str, raw_text: str) -> dict:
    envelope = _envelope(subject, raw_text)
    spec = ToolSpec(
        name="hira_reimbursement_criteria",
        description="verified reimbursement fixture",
        input_model=_NoInput,
        execute=lambda _payload: envelope,
        timeout_s=1.0,
        tags=("external", "hira"),
    )
    result = AgentExecutor(provider=_OneChoice()).run(
        user_text=f"{subject} 급여기준",
        tools=(spec,),
    )
    return _agent_result_payload(f"{subject} 급여기준", result)


# ─── a · mismatch is blocked ────────────────────────────────────────────────


def test_mismatched_notice_body_is_never_built() -> None:
    envelope = _envelope("리바로", COMBINATION_NOTICE)

    assert envelope.ok is False
    assert envelope.error_code == "IDENTITY_MISMATCH"
    assert envelope.evidence == ()
    assert envelope.raw["body_suppressed"] is True
    serialized = json.dumps(envelope.raw, ensure_ascii=False, default=str)
    for token in BODY_TOKENS:
        assert token not in serialized


def test_blocked_answer_carries_the_reason_and_none_of_the_other_product() -> None:
    payload = _payload("리바로", COMBINATION_NOTICE)
    answer = payload["answer"]
    markdown = payload["markdown_response"]

    assert answer == BLOCK_NOTICE
    assert markdown["notice_md"] == BLOCK_NOTICE
    assert markdown["evidence"] == []
    assert markdown["fact_md"] == ""
    for token in BODY_TOKENS:
        assert token not in answer
        assert token not in json.dumps(markdown, ensure_ascii=False, default=str)


def test_blocked_reason_names_the_cause_not_a_lookup_failure() -> None:
    answer = _payload("리바로", COMBINATION_NOTICE)["answer"]

    # The user has to know why, or they cannot decide what to do next.
    assert "연결된 고시가" in answer
    assert "성분 구성이 다른" in answer
    for evasion in ("조회 실패", "데이터 없음", "찾지 못했습니다", "확인할 수 없습니다만"):
        assert evasion not in answer


def test_blocked_notice_body_never_enters_the_planner_context() -> None:
    """The envelope is fed back to the model as a tool result.

    Suppressing the body at render time would still leave it in this payload,
    which is why the fact is declined at construction instead.
    """

    envelope = _envelope("리바로", COMBINATION_NOTICE)
    spec = ToolSpec(
        name="hira_reimbursement_criteria",
        description="verified reimbursement fixture",
        input_model=_NoInput,
        execute=lambda _payload: envelope,
        timeout_s=1.0,
        tags=("external", "hira"),
    )
    result = AgentExecutor(provider=_OneChoice()).run(
        user_text="리바로 급여기준",
        tools=(spec,),
    )
    render_data = result.tool_calls[0]["render_data"]

    assert render_data["identity_status"] == "mismatch"
    assert render_data["body_suppressed"] is True
    assert render_data["evidence"] == []
    serialized = json.dumps(render_data, ensure_ascii=False, default=str)
    for token in BODY_TOKENS:
        assert token not in serialized


# ─── h · the reason must survive the notice surface ─────────────────────────


def test_block_notice_is_recognised_and_digit_free() -> None:
    assert is_reimbursement_identity_notice(BLOCK_NOTICE)
    assert not any(character.isdigit() for character in BLOCK_NOTICE)


def test_block_notice_reaches_the_final_answer_boundary() -> None:
    answer, attached = append_source_basis_notice(
        f"{BLOCK_NOTICE}\n\n## 출처\n\n- HIRA",
        {"notice_md": BLOCK_NOTICE},
    )

    assert attached is True
    assert answer.count(BLOCK_NOTICE) == 1
    assert answer.index(BLOCK_NOTICE) < answer.index("## 출처")


# ─── b~g · safety valves: nothing else may change ───────────────────────────


@pytest.mark.parametrize(
    ("subject", "raw_text", "expected_status"),
    (
        ("리바로젯", COMBINATION_NOTICE, "match"),
        ("헴리브라", "Emicizumab 주사제 (품명: 헴리브라피하주사 30mg 등)", "match"),
        ("악템라", "Tocilizumab 주사제 (품명: 악템라주 등)", "match"),
        ("페린젝트", "철분 주사제 (품명: 페린젝트주 등)", "match"),
        ("악템라", "관련 약제 투여 후 이상반응 관리 기준을 안내한다.", "unverifiable"),
    ),
)
def test_non_mismatch_records_keep_their_body(
    subject: str,
    raw_text: str,
    expected_status: str,
) -> None:
    envelope = _envelope(subject, raw_text)

    assert envelope.ok is True
    assert envelope.error_code is None
    assert envelope.raw["identity_status"] == expected_status
    assert envelope.raw["identity_notice_required"] is False
    assert envelope.raw["body_suppressed"] is False
    assert envelope.evidence[0].source_locator == raw_text


@pytest.mark.parametrize(
    ("subject", "raw_text"),
    (
        ("리바로젯", COMBINATION_NOTICE),
        ("헴리브라", "Emicizumab 주사제 (품명: 헴리브라피하주사 30mg 등)"),
        ("악템라", "관련 약제 투여 후 이상반응 관리 기준을 안내한다."),
    ),
)
def test_non_mismatch_answers_stay_verified_and_unannotated(
    subject: str,
    raw_text: str,
) -> None:
    payload = _payload(subject, raw_text)

    assert payload["markdown_response"]["verification"]["status"] == "pass"
    assert payload["markdown_response"]["notice_md"] == ""
    assert raw_text.splitlines()[0] in payload["answer"]


def test_requested_combination_product_still_receives_the_combination_notice() -> None:
    """The sibling product asking for its own notice must still get it.

    Blocking 리바로 must not spill onto 리바로젯: the same record is the correct
    answer for one subject and the wrong answer for the other.
    """

    payload = _payload("리바로젯", COMBINATION_NOTICE)

    assert payload["markdown_response"]["notice_md"] == ""
    for token in ("Ezetimibe", "리바로젯", "요양급여를 인정"):
        assert token in payload["answer"]


def test_unindexed_brand_failure_is_untouched_by_identity_blocking() -> None:
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
    assert "body_suppressed" not in envelope.raw


def test_missing_resolver_does_not_block() -> None:
    """Without a resolver there is no judgment, so there is nothing to block."""

    envelope = reimbursement_envelope(
        _lookup(COMBINATION_NOTICE, brand="리바로"),
        subject="리바로",
    )

    assert envelope.ok is True
    assert envelope.raw["identity_status"] == "unverifiable"
    assert envelope.raw["body_suppressed"] is False
    assert envelope.evidence[0].source_locator == COMBINATION_NOTICE
