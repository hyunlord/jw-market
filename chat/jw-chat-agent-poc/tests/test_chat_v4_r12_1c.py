from __future__ import annotations

import re

from jw_chat_agent_poc.service.v4.contracts import EvidenceEnvelope, SourceResult
from jw_chat_agent_poc.service.v4.gates import apply_v4_gates
from jw_chat_agent_poc.service.v4.lossless_contracts import (
    DeterministicRender,
    RenderNode,
)
from jw_chat_agent_poc.service.v4.lossless_spine import compose_lossless_answer
from jw_chat_agent_poc.service.v4.patent import build_patent_lane_payload


def _patent_lanes_with_news() -> dict[str, dict[str, object]]:
    return build_patent_lane_payload(
        kr_calls=(
            {
                "tool": "mfds_patent",
                "render_data": {
                    "items": [
                        {
                            "ITEM_NAME": "리바로젯정",
                            "INGR_ENG_NAME": "pitavastatin ezetimibe",
                            "PATENTEE": "유한양행",
                            "PAGE_GB_NM": "제품특허",
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
                            "title": "유한양행, 트루셋 특허 분할",
                            "snippet": "유한양행의 별도 품목 특허 소식입니다.",
                            "url": "https://www.hitnews.co.kr/company-only",
                        },
                        {
                            "title": "리바로젯 제네릭 도전",
                            "snippet": "리바로젯 후발 제품 관련 보도입니다.",
                            "url": "https://www.dailypharm.com/brand-match",
                        },
                    ]
                },
            },
        ),
        entity_tokens=("리바로젯", "pitavastatin", "ezetimibe"),
    )


def test_inline_and_source_block_labels_share_public_dictionary() -> None:
    result = SourceResult(
        source="patent",
        query="리바로젯 특허현황",
        status="ok",
        payload={"patent_lanes": _patent_lanes_with_news()},
        evidence=EvidenceEnvelope(
            kind="patent",
            entity_match="EXACT",
            source_scope="GLOBAL",
            time_match="NOT_REQUESTED",
            eligible_claims=("patent",),
        ),
    )
    answer = (
        "## 핵심 답\n"
        "국내 목록을 확인했습니다. [출처: 특허 자료]\n\n"
        "관련 보도를 확인했습니다. [출처: web_search]\n\n"
        "참조 경로는 web_search, tavily, mfds_patent, "
        "mcp_patent_reader입니다."
    )

    gated = apply_v4_gates("리바로젯 특허현황", answer, (result,))

    assert "[출처: 식품의약품안전처 의약품 특허목록]" in gated.text
    assert "[출처: 웹 뉴스]" in gated.text
    assert '- 식품의약품안전처 의약품 특허목록 — 조회 "리바로젯 특허현황"' in gated.text
    assert '- 특허·분쟁 동향 (웹 뉴스) — 조회 "리바로젯 특허현황"' in gated.text
    assert "유한양행, 트루셋 특허 분할" not in gated.text
    assert re.search(r"(?i)\b(?:web_search|tavily|mfds_patent|mcp_[a-z0-9_]+)\b", gated.text) is None

    composed = compose_lossless_answer(
        DeterministicRender(
            profile="patent_portfolio",
            nodes=(
                RenderNode(
                    block_id="patent:fact-table",
                    text="## 정본 표\nmcp_patent_reader 결과를 확인했습니다.",
                ),
            ),
        ),
        gated.text,
        synthesis_trace={"status": "synthesized"},
        mode="inject",
    )
    assert re.search(
        r"(?i)\b(?:web_search|tavily|mfds_patent|mcp_[a-z0-9_]+)\b",
        composed.text,
    ) is None


def test_patent_news_requires_brand_or_ingredient_and_records_each_decision() -> None:
    lanes = _patent_lanes_with_news()
    news = lanes["news"]

    assert news["records_received"] == 2
    assert news["records_unique"] == 1
    assert [record["title"] for record in news["records"]] == [
        "리바로젯 제네릭 도전"
    ]
    assert news["relevance_decisions"] == [
        {
            "record_index": 0,
            "decision": "discard",
            "reason": "company_token_only",
            "matched_brand_or_ingredient_tokens": [],
            "matched_company_tokens": ["유한양행"],
        },
        {
            "record_index": 1,
            "decision": "keep",
            "reason": "brand_or_ingredient_token",
            "matched_brand_or_ingredient_tokens": ["리바로젯"],
            "matched_company_tokens": [],
        },
    ]


def _compose_table(rows: str) -> str:
    rendered = DeterministicRender(
        profile="patent_portfolio",
        nodes=(
            RenderNode(
                block_id="patent:kr-primary",
                record_ids=("kr-1", "kr-2"),
                text=(
                    "## 국내 NeDrug 특허목록 정본\n"
                    "| 제품 | 권리자 | 만료일 |\n"
                    "| --- | --- | --- |\n"
                    f"{rows}"
                ),
            ),
        ),
    )
    return compose_lossless_answer(
        rendered,
        "## 핵심 답\n확인된 목록입니다.",
        synthesis_trace={"status": "synthesized"},
        mode="inject",
    ).text


def test_all_unprovided_column_is_omitted_and_disclosed() -> None:
    text = _compose_table(
        "| 리바로젯 | 원천 미제공 | 2027-01-01 |\n"
        "| 리바로 | 원천 미제공 | 원천 미제공 |"
    )

    assert "| 제품 | 만료일 |" in text
    assert "| 제품 | 권리자 | 만료일 |" not in text
    assert "## 미확인 요소" in text
    assert "전 행 원천 미제공으로 생략한 열: 권리자" in text
    assert text.count("리바로젯") == 1
    assert text.count("리바로") == 2


def test_partially_provided_column_is_retained() -> None:
    text = _compose_table(
        "| 리바로젯 | 유한양행 | 2027-01-01 |\n"
        "| 리바로 | 원천 미제공 | 원천 미제공 |"
    )

    assert "| 제품 | 권리자 | 만료일 |" in text
    assert "생략한 열: 권리자" not in text
