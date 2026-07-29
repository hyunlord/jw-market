from __future__ import annotations

from datetime import UTC, datetime

from jw_chat_agent_poc.tool_use.reimbursement_evidence import reimbursement_envelope
from jw_chat_agent_poc.tools.external.hira_reimbursement import (
    CacheLookupStatus,
    CacheStatus,
    ReimbursementCacheResult,
    ReimbursementCriterion,
    ReimbursementLookupService,
)


class _IndexMissStore:
    def get_reimbursement_criteria(self, _brand_name: str) -> ReimbursementCacheResult:
        return ReimbursementCacheResult(
            CacheStatus.NOT_FOUND,
            None,
            None,
            lookup_status=CacheLookupStatus.BRAND_UNMATCHED,
            schema_name="reimbursement_stage",
        )

    def put_reimbursement_criteria(self, _criterion: ReimbursementCriterion) -> bool:
        return False


class _Realtime:
    def __init__(self, result: ReimbursementCriterion | None) -> None:
        self.result = result
        self.calls: list[str] = []

    def fetch(self, brand_name: str) -> ReimbursementCriterion | None:
        self.calls.append(brand_name)
        return self.result


def _criterion() -> ReimbursementCriterion:
    return ReimbursementCriterion(
        brand_name="아일리아",
        title="아일리아 급여기준",
        raw_text="아일리아의 급여기준 원문",
        source_date="2026-07-30",
        collected_at=datetime(2026, 7, 30, tzinfo=UTC),
        notice_number="notice-1",
        source_url="https://www.hira.or.kr/rc/example.do",
    )


def test_fb03_brand_index_miss_attempts_bounded_realtime_lookup() -> None:
    realtime = _Realtime(_criterion())

    result = ReimbursementLookupService(
        store=_IndexMissStore(),
        realtime=realtime,
    ).lookup("아일리아")

    assert result.ok is True
    assert result.retrieval == "realtime"
    assert result.cache_lookup_status is CacheLookupStatus.BRAND_UNMATCHED
    assert realtime.calls == ["아일리아"]


def test_fb03_policy_can_skip_realtime_after_known_upstream_failure() -> None:
    realtime = _Realtime(_criterion())

    result = ReimbursementLookupService(
        store=_IndexMissStore(),
        realtime=realtime,
        realtime_allowed=lambda: False,
    ).lookup("아일리아")

    assert result.ok is False
    assert result.error_code == "INDEX_MISS"
    assert realtime.calls == []


def test_fb03_realtime_miss_reports_index_and_official_lookup_separately() -> None:
    realtime = _Realtime(None)

    result = ReimbursementLookupService(
        store=_IndexMissStore(),
        realtime=realtime,
    ).lookup("아일리아")
    envelope = reimbursement_envelope(result, subject="아일리아")

    assert result.error_code == "REALTIME_NO_EVIDENCE"
    assert realtime.calls == ["아일리아"]
    assert "내부 급여기준 색인" in envelope.preview
    assert "실시간 공식 조회" in envelope.preview


def test_fb03_policy_skip_does_not_claim_official_source_absence() -> None:
    result = ReimbursementLookupService(
        store=_IndexMissStore(),
        realtime=_Realtime(_criterion()),
        realtime_allowed=lambda: False,
    ).lookup("아일리아")
    envelope = reimbursement_envelope(result, subject="아일리아")

    assert result.error_code == "INDEX_MISS"
    assert "내부 급여기준 색인" in envelope.preview
    assert "공식 원천에 없다는 뜻은 아닙니다" in envelope.preview
