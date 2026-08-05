from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from jw_chat_agent_poc.tool_use.v3_execution_contracts import (
    MarketMetricFact,
    ToolFailureRecord,
    V3EvidenceBundle,
    WebSourceFact,
)
from jw_chat_agent_poc.tool_use.v3_fusion import validate_fusion_answer
from jw_chat_agent_poc.tool_use.v3_fusion_contracts import (
    GeneratedFusionAnswer,
    GeneratedFusionClaim,
)
from jw_chat_agent_poc.tool_use.v3_fusion_evidence import fusion_fact_payload
from jw_chat_agent_poc.tool_use.v3_web_augmentation import (
    V3WebAugmenter,
    WebSearchResult,
    web_augmentation_eligibility,
)


NOW = datetime(2026, 8, 5, 3, 0, tzinfo=UTC)


def _bundle(
    *,
    facts: tuple[object, ...] = (),
    failures: tuple[ToolFailureRecord, ...] = (),
) -> V3EvidenceBundle:
    return V3EvidenceBundle(
        status="partial" if facts and failures else "complete" if facts else "failed",
        facts=facts,
        failures=failures,
        deferred=(),
        executions=(),
        original_call_count=len(facts) + len(failures),
        executed_call_count=len(facts) + len(failures),
        deduplicated_call_count=0,
    )


def _web_fact(
    *,
    excerpt: str = "보험급여 인정기준은 2024년 3월 개정됐다.",
    conflicts_with: tuple[str, ...] = (),
) -> WebSourceFact:
    return WebSourceFact(
        evidence_id="v3-shadow:web_search:0123456789abcdef",
        tool_name="web_search",
        arguments={"query": "아일리아 보험급여 인정기준"},
        raw_result={"title": "급여기준", "url": "https://www.hira.or.kr/rule/1"},
        missing_required_fields=(),
        url="https://www.hira.or.kr/rule/1",
        title="급여기준",
        excerpt=excerpt,
        fetched_at_utc="2026-08-05T03:00:00Z",
        domain="www.hira.or.kr",
        search_query="아일리아 보험급여 인정기준",
        result_rank=1,
        source_grade="SUPPLEMENTARY",
        search_stage="official",
        conflicts_with_evidence_ids=conflicts_with,
    )


def _market_fact() -> MarketMetricFact:
    return MarketMetricFact(
        evidence_id="v3-shadow:market.get_brand_metric:fedcba9876543210",
        tool_name="market.get_brand_metric",
        arguments={"brand": "아일리아", "metric": "sales"},
        raw_result={"value": 80.39, "period": "2026-05"},
        missing_required_fields=(),
        entity="아일리아",
        metric="sales",
        period="2026-05",
        unit="억원",
        view="strategic",
        market="retina",
    )


def _failure(tool: str, error_type: str, message: str) -> ToolFailureRecord:
    return ToolFailureRecord(tool, {"brand": "아일리아"}, "execution", error_type, message)


def test_web_source_fact_keeps_web_numbers_out_of_allowed_numeric_literals() -> None:
    payload = fusion_fact_payload(_web_fact(excerpt="2024년 3월 기준 12건"))

    assert payload["fact_type"] == "web_source"
    assert payload["allowed_numeric_literals"] == []
    assert payload["web_quoted_numeric_literals"] == ["12", "2024", "3"]
    assert payload["canonical"]["url"] == "https://www.hira.or.kr/rule/1"


def test_web_fact_payload_bounds_only_the_fusion_excerpt() -> None:
    excerpt = "A" * 1200 + " 외부값 9999"
    fact = replace(
        _web_fact(excerpt=excerpt),
        raw_result={"title": "급여기준", "snippet": excerpt},
    )

    payload = fusion_fact_payload(fact)

    assert payload["canonical"]["excerpt"] == excerpt[:1200]
    assert "snippet" not in payload["raw_result"]
    assert fact.excerpt == excerpt
    assert fact.raw_result["snippet"] == excerpt
    assert "9999" not in payload["web_quoted_numeric_literals"]


def test_web_claim_requires_visible_source_url() -> None:
    fact = _web_fact()
    generated = GeneratedFusionAnswer(
        claims=(
            GeneratedFusionClaim(
                text="외부 자료에 따르면 2024년 3월 개정됐다.",
                evidence_ids=(fact.evidence_id,),
            ),
        )
    )

    result = validate_fusion_answer(generated, _bundle(facts=(fact,)))

    assert result.answer.claims == ()
    assert result.audit.rejected_claims[0].reason == "web_source_attribution_missing"


def test_attributed_web_number_is_quoted_without_internal_promotion() -> None:
    fact = _web_fact()
    generated = GeneratedFusionAnswer(
        claims=(
            GeneratedFusionClaim(
                text=(
                    "외부 자료에 따르면 2024년 3월 개정됐다 "
                    "(출처: https://www.hira.or.kr/rule/1)."
                ),
                evidence_ids=(fact.evidence_id,),
            ),
        )
    )

    result = validate_fusion_answer(generated, _bundle(facts=(fact,)))

    assert len(result.answer.claims) == 1
    assert result.audit.ungrounded_numeric_literals == ()


def test_web_value_cannot_be_mislabeled_as_internal_data() -> None:
    fact = _web_fact(excerpt="매출은 80.39억원이다.")
    generated = GeneratedFusionAnswer(
        claims=(
            GeneratedFusionClaim(
                text="내부 데이터 기준 매출은 80.39억원이다 (https://www.hira.or.kr/rule/1).",
                evidence_ids=(fact.evidence_id,),
            ),
        )
    )

    result = validate_fusion_answer(generated, _bundle(facts=(fact,)))

    assert result.answer.claims == ()
    assert result.audit.rejected_claims[0].reason == "web_source_mislabeled_internal"


def test_mixed_claim_cannot_label_web_only_value_as_internal_data() -> None:
    market = _market_fact()
    web = _web_fact(excerpt="외부 자료의 매출은 81.00억원이다.")
    generated = GeneratedFusionAnswer(
        claims=(
            GeneratedFusionClaim(
                text=(
                    "내부 데이터 기준 매출은 80.39억원과 81.00억원이다 "
                    "(https://www.hira.or.kr/rule/1)."
                ),
                evidence_ids=(market.evidence_id, web.evidence_id),
            ),
        )
    )

    result = validate_fusion_answer(generated, _bundle(facts=(market, web)))

    assert result.answer.claims == ()
    assert result.audit.rejected_claims[0].reason == "web_source_mislabeled_internal"


def test_conflicting_web_fact_requires_both_sources_and_limitation() -> None:
    market = _market_fact()
    web = _web_fact(
        excerpt="외부 자료의 매출은 81.00억원이다.",
        conflicts_with=(market.evidence_id,),
    )
    one_sided = GeneratedFusionAnswer(
        claims=(
            GeneratedFusionClaim(
                text="외부 자료의 매출은 81.00억원이다 (https://www.hira.or.kr/rule/1).",
                evidence_ids=(web.evidence_id,),
            ),
        )
    )
    both = GeneratedFusionAnswer(
        claims=(
            GeneratedFusionClaim(
                text=(
                    "내부 데이터는 80.39억원이고 외부 자료는 81.00억원이다 "
                    "(https://www.hira.or.kr/rule/1)."
                ),
                evidence_ids=(market.evidence_id, web.evidence_id),
            ),
        ),
        limitations=("내부 데이터와 외부 자료의 값에 차이가 있습니다.",),
    )

    rejected = validate_fusion_answer(one_sided, _bundle(facts=(market, web)))
    accepted = validate_fusion_answer(both, _bundle(facts=(market, web)))

    assert rejected.audit.rejected_claims[0].reason == "web_conflict_missing_internal_evidence"
    assert len(accepted.answer.claims) == 1
    assert accepted.answer.limitations == ("내부 데이터와 외부 자료의 값에 차이가 있습니다.",)


def test_unrelated_difference_limitation_does_not_disclose_web_conflict() -> None:
    market = _market_fact()
    web = _web_fact(
        excerpt="외부 자료의 매출은 81.00억원이다.",
        conflicts_with=(market.evidence_id,),
    )
    generated = GeneratedFusionAnswer(
        claims=(
            GeneratedFusionClaim(
                text=(
                    "내부 데이터는 80.39억원이고 외부 자료는 81.00억원이다 "
                    "(https://www.hira.or.kr/rule/1)."
                ),
                evidence_ids=(market.evidence_id, web.evidence_id),
            ),
        ),
        limitations=("표현 방식에 차이가 있습니다.",),
    )

    result = validate_fusion_answer(generated, _bundle(facts=(market, web)))

    assert result.answer.claims == ()
    assert result.audit.rejected_claims[0].reason == "web_conflict_not_disclosed"


def test_q77_uses_official_search_then_general_expansion() -> None:
    calls: list[str] = []

    def search(query: str, *, topic: str) -> WebSearchResult:
        calls.append(query)
        if "site:hira.or.kr" in query:
            return WebSearchResult(provider="fixture", query=query, items=(), latency_ms=3.0)
        return WebSearchResult(
            provider="fixture",
            query=query,
            items=(
                {
                    "title": "아일리아 급여기준",
                    "url": "https://example.org/eylea",
                    "snippet": "아일리아 급여기준 안내",
                },
            ),
            latency_ms=4.0,
        )

    bundle = _bundle(
        failures=(
            _failure(
                "hira_reimbursement_criteria",
                "REALTIME_NO_EVIDENCE",
                "실시간 공식 조회에서도 급여기준을 확인하지 못했습니다.",
            ),
        )
    )
    result = V3WebAugmenter(search=search, now=lambda: NOW).augment(
        "아일리아 급여기준 알려줘",
        bundle,
    )

    assert len(calls) == 2
    assert "site:hira.or.kr" in calls[0]
    assert "site:" not in calls[1]
    assert result.expanded_to_general is True
    assert len(result.bundle.facts) == 1
    assert result.bundle.facts[0].search_stage == "general"
    assert result.search_log[0].items == ()


def test_web_augmentation_does_not_answer_typed_exclusions() -> None:
    blocked = (
        ("D693 환자수", _failure("hira_disease_patient_stats", "ERROR", "조회 실패")),
        ("메트포르민 계열 매출", _failure("market.get_brand_metric", "UnknownBrandError", "unknown_brand: 메트포르민")),
        ("카나브패밀리 실적", _failure("market.get_brand_metric", "AmbiguousFamilyError", "ambiguous_family")),
        ("존재하지않는브랜드XYZ987654 매출", _failure("market.get_brand_metric", "UnknownBrandError", "unknown_brand")),
        ("헴리브라 UBIST", _failure("market.get_brand_metric", "UnsupportedSourceError", "unsupported_source")),
    )

    for question, failure in blocked:
        decision = web_augmentation_eligibility(question, _bundle(failures=(failure,)))
        assert decision.eligible is False
        assert decision.reason.startswith("blocked_")


def test_web_results_preserve_required_raw_fields() -> None:
    result = WebSearchResult(
        provider="fixture",
        query="리바로 뉴스",
        items=(
            {
                "title": "리바로 소식",
                "url": "https://news.example.com/a",
                "snippet": "원문 발췌",
                "published_date": "March 15, 2024",
            },
        ),
        latency_ms=8.5,
    )
    augmented = V3WebAugmenter(search=lambda *_args, **_kwargs: result, now=lambda: NOW).augment(
        "리바로 관련 최근 뉴스 알려줘",
        _bundle(failures=(_failure("web_search", "NO_DATA", "검색 결과 없음"),)),
    )

    fact = augmented.bundle.facts[0]
    assert fact.title == "리바로 소식"
    assert fact.excerpt == "원문 발췌"
    assert fact.fetched_at_utc == "2026-08-05T03:00:00Z"
    assert fact.domain == "news.example.com"
    assert fact.result_rank == 1
    assert fact.raw_result["published_date"] == "March 15, 2024"


def test_web_augmentation_preserves_all_results_but_projects_top_three_facts() -> None:
    items = tuple(
        {
            "title": f"검색 결과 {rank}",
            "url": f"https://news.example.com/{rank}",
            "snippet": f"원문 발췌 {rank}",
        }
        for rank in range(1, 6)
    )
    result = WebSearchResult(
        provider="fixture",
        query="리바로 뉴스",
        items=items,
        latency_ms=8.5,
    )

    augmented = V3WebAugmenter(
        search=lambda *_args, **_kwargs: result,
        now=lambda: NOW,
    ).augment("리바로 관련 최근 뉴스 알려줘", _bundle())

    assert len(augmented.search_log[0].items) == 5
    assert [fact.result_rank for fact in augmented.bundle.facts] == [1, 2, 3]


def test_web_result_marks_a_conflicting_internal_metric() -> None:
    market = _market_fact()
    result = WebSearchResult(
        provider="fixture",
        query="아일리아 매출 웹 검색",
        items=(
            {
                "title": "아일리아 매출 자료",
                "url": "https://news.example.com/eylea-sales",
                "snippet": "아일리아 매출은 81.00억원이다.",
            },
        ),
        latency_ms=5.0,
    )
    augmented = V3WebAugmenter(
        search=lambda *_args, **_kwargs: result,
        now=lambda: NOW,
    ).augment(
        "아일리아 매출 웹 검색",
        _bundle(
            facts=(market,),
            failures=(_failure("web_search", "NO_DATA", "검색 결과 없음"),),
        ),
    )

    web = augmented.bundle.facts[-1]
    assert isinstance(web, WebSourceFact)
    assert web.conflicts_with_evidence_ids == (market.evidence_id,)


def test_web_conflict_detection_skips_internal_fact_without_metric() -> None:
    market = replace(_market_fact(), metric=None)
    result = WebSearchResult(
        provider="fixture",
        query="아일리아 관련 최근 뉴스",
        items=(
            {
                "title": "아일리아 소식",
                "url": "https://news.example.com/a",
                "snippet": "외부 자료에 따르면 81.00억원이다.",
            },
        ),
        latency_ms=8.5,
    )

    augmented = V3WebAugmenter(
        search=lambda *_args, **_kwargs: result,
        now=lambda: NOW,
    ).augment("아일리아 관련 최근 뉴스 알려줘", _bundle(facts=(market,)))

    assert augmented.bundle.facts[-1].conflicts_with_evidence_ids == ()
