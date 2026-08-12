from __future__ import annotations

from jw_chat_agent_poc.service.v4.contracts import SourceResult
from jw_chat_agent_poc.service.v4.gates import apply_v4_gates
from jw_chat_agent_poc.service.v4.lossless_contracts import (
    DeterministicRender,
    RenderNode,
    SourceReference,
)
from jw_chat_agent_poc.service.v4.lossless_spine import compose_lossless_answer
from jw_chat_agent_poc.service.v4.patent import build_patent_lane_payload


def test_lossless_source_surface_hides_internal_and_api_urls_but_keeps_record_links() -> None:
    # Given: an injected R12.1 answer with internal transports, API endpoints,
    # and one user-facing ClinicalTrials record URL.
    rendered = DeterministicRender(
        profile="patent_portfolio",
        text="## 특허 전건\n확인된 특허입니다.",
        source_refs=(
            SourceReference(url="http://mcp-nedrug-standby-svc:8080/json"),
            SourceReference(
                url="http://mcp-nedrug.llmops.svc.cluster.local:8080/json"
            ),
            SourceReference(url="http://10.24.3.19:8080/json"),
            SourceReference(
                url="https://genos.example.com/api/gateway/rep/serving/214"
            ),
            SourceReference(url="https://clinicaltrials.gov/api/v2/studies"),
            SourceReference(
                url="https://clinicaltrials.gov/study/NCT05151731",
                title="NCT05151731",
            ),
        ),
    )

    # When: the deterministic facts and their source block are composed.
    composed = compose_lossless_answer(
        rendered,
        (
            "## 핵심 답\n자동 해설입니다.\n\n"
            "## 출처\n"
            "- 내부 전송 — http://mcp-nedrug-standby-svc:8080/json\n"
            "- 내부 별칭 — mcp-nedrug-standby-svc:8080/json\n"
            "- 내부 DNS — mcp-nedrug.llmops.svc.cluster.local:8080/json\n"
            "- 내부 IP — 10.24.3.19:8080/json\n"
            "- API 전송 — https://clinicaltrials.gov/api/v2/studies"
        ),
        synthesis_trace={"status": "synthesized"},
        mode="inject",
    )

    # Then: transport/API links are absent while the public record link survives.
    assert "mcp-nedrug-standby-svc" not in composed.text
    assert ".svc.cluster.local" not in composed.text
    assert "10.24.3.19" not in composed.text
    assert "/api/gateway/rep/serving/214" not in composed.text
    assert "clinicaltrials.gov/api/v2" not in composed.text
    assert "https://clinicaltrials.gov/study/NCT05151731" in composed.text


def test_lossless_surface_reorders_commentary_and_facts_with_one_source_block() -> None:
    rendered = DeterministicRender(
        profile="patent_portfolio",
        nodes=(
            RenderNode(
                block_id="patent:coverage",
                text="## 조사 범위와 완전성\n원천 검색 4건 · 수신 4건 · 중복 제거 후 3건 · 상세 표시 3건",
            ),
            RenderNode(
                block_id="patent:kr-primary",
                record_ids=("kr-1",),
                text="## 국내 NeDrug 특허목록 정본\n| 특허 |\n| --- |\n| KR-1 |",
            ),
            RenderNode(
                block_id="patent:us-secondary",
                record_ids=("us-1",),
                text="## 미국 Orange Book 보조표\n| 특허 |\n| --- |\n| US-1 |",
            ),
            RenderNode(
                block_id="patent:news",
                record_ids=("news-1",),
                text="## 뉴스 맥락\n| 보도 |\n| --- |\n| 관련 기사 |",
            ),
            RenderNode(
                block_id="patent:limits",
                text="## 해석 상한\n출시 가능성을 단정하지 않습니다.",
            ),
        ),
        source_refs=(
            SourceReference(
                url="https://clinicaltrials.gov/study/NCT05151731",
                title="NCT05151731",
            ),
        ),
    )
    commentary = """# 근거와 맥락
공식 목록과 뉴스 맥락을 구분했습니다.

### 핵심 답
국내 목록의 상태를 먼저 확인해야 합니다.

#### 종합 인사이트
경쟁 진입 시점은 추가 확인이 필요합니다.

## 미확인 요소
소송 상태는 확인하지 못했습니다.

### 출처
- 식품의약품안전처 의약품 특허목록 — 조회 "리바로젯 특허현황"
- [NCT05151731](https://clinicaltrials.gov/study/NCT05151731)"""

    composed = compose_lossless_answer(
        rendered,
        commentary,
        synthesis_trace={"status": "synthesized"},
        mode="inject",
    )

    expected_order = (
        "## 핵심 답",
        "## 조사 범위와 완전성",
        "## 국내 NeDrug 특허목록 정본",
        "## 미국 Orange Book 보조표",
        "## 뉴스 맥락",
        "## 근거와 맥락",
        "## 종합 인사이트",
        "## 해석 상한",
        "## 미확인 요소",
        "## 출처",
    )
    positions = tuple(composed.text.index(heading) for heading in expected_order)
    assert positions == tuple(sorted(positions))
    assert composed.text.count("## 출처") == 1
    assert composed.text.count("https://clinicaltrials.gov/study/NCT05151731") == 1
    assert "자동 해설" not in composed.text
    assert not any(
        line.startswith("# ") or line.startswith("###")
        for line in composed.text.splitlines()
    )


def test_lossless_surface_does_not_duplicate_commentary_when_core_headings_are_empty() -> None:
    rendered = DeterministicRender(
        profile="patent_portfolio",
        nodes=(
            RenderNode(
                block_id="patent:coverage",
                text="## 조사 범위와 완전성\n원천 검색 1건 · 수신 1건 · 중복 제거 후 1건 · 상세 표시 1건",
            ),
            RenderNode(
                block_id="patent:kr-primary",
                record_ids=("kr-1",),
                text="## 국내 NeDrug 특허목록 정본\n| 특허 |\n| --- |\n| KR-1 |",
            ),
        ),
    )
    commentary = """## 핵심 답
## 핵심 답

## 근거와 맥락
공식 목록에서 확인된 상태를 설명합니다.

## 종합 인사이트
경쟁 진입 시점은 추가 확인이 필요합니다.

## 출처
- 식품의약품안전처 의약품 특허목록 — 조회 "리바로젯 특허현황"""  # noqa: E501

    composed = compose_lossless_answer(
        rendered,
        commentary,
        synthesis_trace={"status": "synthesized"},
        mode="inject",
    )

    assert composed.text.count("## 핵심 답") == 1
    assert composed.text.count("## 근거와 맥락") == 0
    assert composed.text.count("## 종합 인사이트") == 1
    assert composed.text.count("## 출처") == 1
    assert composed.text.count("공식 목록에서 확인된 상태를 설명합니다.") == 1
    assert composed.text.index("## 조사 범위와 완전성") < composed.text.index(
        "## 종합 인사이트"
    )


def test_lossless_surface_omits_empty_fact_sections_and_uses_exact_fallback_copy() -> None:
    rendered = DeterministicRender(
        profile="patent_portfolio",
        nodes=(
            RenderNode(
                block_id="patent:coverage",
                text="## 조사 범위와 완전성\n원천 검색 0건 · 수신 0건 · 중복 제거 후 0건 · 상세 표시 0건",
            ),
            RenderNode(
                block_id="patent:news",
                text=(
                    "## 뉴스 맥락\n| 보도 |\n| --- |\n"
                    "| 조회 결과 없음 |"
                ),
            ),
        ),
    )

    composed = compose_lossless_answer(
        rendered,
        "합성 실패 문구",
        synthesis_trace={"status": "fallback", "fallback_reason": "timeout"},
        mode="inject",
    )

    assert composed.text.startswith("## 핵심 답\n자동 해설 생성 미완료")
    assert "## 뉴스 맥락" not in composed.text
    assert "자동 해설 생성이 완료되지 않았습니다" not in composed.text


def test_patent_news_lane_discards_items_without_brand_ingredient_or_company_tokens() -> None:
    payload = build_patent_lane_payload(
        kr_calls=(
            {
                "tool": "mfds_patent",
                "render_data": {
                    "items": [
                        {
                            "ITEM_NAME": "리바로젯정",
                            "INGR_ENG_NAME": "pitavastatin calcium ezetimibe",
                            "PATENTEE": "JW중외제약",
                            "DOMESTIC_PATENT_NO": "10-1234567",
                        }
                    ]
                },
            },
        ),
        us_calls=(),
        news_calls=(
            {
                "tool": "tavily_search",
                "render_data": {
                    "items": [
                        {
                            "title": "리바로젯 특허 분쟁 동향",
                            "snippet": "JW중외제약의 복합제 특허를 다룹니다.",
                            "url": "https://example.org/relevant",
                        },
                        {
                            "title": "3D 프린팅 특허 괴물",
                            "snippet": "산업용 프린터 이야기입니다.",
                            "url": "https://example.org/monster",
                        },
                        {
                            "title": "주간 산업 브리핑",
                            "snippet": "무관한 기업 소식 모음입니다.",
                            "url": "https://example.org/briefing",
                        },
                        {
                            "title": "Calcium material patent briefing",
                            "snippet": "A construction-material patent roundup.",
                            "url": "https://example.org/calcium",
                        },
                    ]
                },
            },
        ),
        entity_tokens=("리바로젯", "pitavastatin", "ezetimibe", "JW중외제약"),
    )

    news = payload["news"]
    assert news["records_received"] == 4
    assert news["records_unique"] == 1
    assert [record["title"] for record in news["records"]] == [
        "리바로젯 특허 분쟁 동향"
    ]


def test_patent_sources_use_formal_canonical_and_web_news_labels() -> None:
    lanes = build_patent_lane_payload(
        kr_calls=(
            {
                "tool": "mfds_patent",
                "render_data": {"items": [{"DOMESTIC_PATENT_NO": "10-1234567"}]},
            },
        ),
        us_calls=(),
        news_calls=(
            {
                "tool": "tavily_search",
                "render_data": {
                    "items": [
                        {
                            "title": "리바로젯 특허 분쟁 동향",
                            "snippet": "리바로젯 관련 보도",
                            "url": "https://example.org/relevant",
                        }
                    ]
                },
            },
        ),
        entity_tokens=("리바로젯",),
    )
    result = SourceResult(
        source="patent",
        query="리바로젯 특허현황",
        status="ok",
        payload={"patent_lanes": lanes},
    )

    text = apply_v4_gates(
        "리바로젯 특허현황",
        "## 핵심 답\n확인된 특허입니다.",
        (result,),
    ).text

    assert '식품의약품안전처 의약품 특허목록 — 조회 "리바로젯 특허현황"' in text
    assert '특허·분쟁 동향 (웹 뉴스) — 조회 "리바로젯 특허현황"' in text
    assert '특허 자료 — "리바로젯 특허현황" 특허 검색' not in text
