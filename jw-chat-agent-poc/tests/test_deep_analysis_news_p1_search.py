from __future__ import annotations

import json
from pathlib import Path

from jw_chat_agent_poc import ChatAgent
from jw_chat_agent_poc.agentic import validate_news_filters
from jw_chat_agent_poc.resolver import BrandResolver
from jw_chat_agent_poc.router import BQSubQuestion
from jw_chat_agent_poc.tools.deep_analysis import DeepAnalysisNewsTool, StaticDeepAnalysisNewsReader


def _news(payloads_by_brand: dict[str, dict]) -> DeepAnalysisNewsTool:
    return DeepAnalysisNewsTool(reader=StaticDeepAnalysisNewsReader(payloads_by_brand))


def _resolver(tmp_path: Path, brands: tuple[str, ...]) -> BrandResolver:
    fixture = tmp_path / "brand_catalog.json"
    fixture.write_text(
        json.dumps([{"canonical_brand": brand, "aliases": [], "molecule_en": []} for brand in brands], ensure_ascii=False),
        encoding="utf-8",
    )
    return BrandResolver(fixture_path=fixture)


class RouteBrandRouter:
    def route(self, question: str, has_documents: bool = False) -> list[BQSubQuestion]:
        return [
            BQSubQuestion(
                bq="Q1",
                question="관련 뉴스",
                sources=("deep_analysis_events",),
                reason="test route brand metadata",
                brands=("리바로젯", "리바로"),
            )
        ]


def test_news_filter_plan_supports_title_and_content_contains_terms() -> None:
    plan = validate_news_filters(
        (
            ("title_contains", "아토젯+약가"),
            ("content_contains", "에제티미브 또는 피타바스타틴"),
        )
    )

    assert plan.unsupported == ()
    assert plan.title_text is not None
    assert plan.title_text.terms == ("아토젯", "약가")
    assert plan.title_text.operator == "AND"
    assert plan.content_text is not None
    assert plan.content_text.terms == ("에제티미브", "피타바스타틴")
    assert plan.content_text.operator == "OR"
    assert plan.applied_filters() == {
        "title_contains": "아토젯 AND 약가",
        "content_contains": "에제티미브 OR 피타바스타틴",
    }


def test_related_news_filters_title_contains_with_normalized_contains() -> None:
    news = _news(
        {
            "리바로": {
                "data": {
                    "events": [
                        {
                            "title": "리바로   아토젯 약가 이슈",
                            "source": "약업신문",
                            "date": "2026-06-11",
                            "impact_score": 91,
                            "on_list": True,
                            "summary": "제목 검색 대상",
                        },
                        {
                            "title": "리바로 신규 처방 동향",
                            "source": "약업신문",
                            "date": "2026-06-10",
                            "impact_score": 90,
                            "on_list": True,
                            "summary": "검색어 없음",
                        },
                    ]
                }
            }
        }
    )

    result = ChatAgent(news=news).answer("리바로 뉴스 중 제목에 아토젯+약가 있는거")

    call = result["tool_calls"][0]
    assert call["unsupported_filters"] == []
    assert call["applied_filters"]["title_contains"] == "아토젯 AND 약가"
    assert [item["title"] for item in call["render_data"]["items"]] == ["리바로   아토젯 약가 이슈"]


def test_related_news_reports_title_text_zero_without_generic_fallback() -> None:
    news = _news(
        {
            "리바로": {
                "data": {
                    "events": [
                        {
                            "title": "리바로 신규 처방 동향",
                            "source": "약업신문",
                            "date": "2026-06-11",
                            "impact_score": 91,
                            "on_list": True,
                            "summary": "본문에도 검색어 없음",
                        }
                    ]
                }
            }
        }
    )

    result = ChatAgent(news=news).answer("리바로 뉴스 중 제목에 아토젯 있는거")

    call = result["tool_calls"][0]
    assert call["unsupported_filters"] == []
    assert call["applied_filters"]["title_contains"] == "아토젯"
    assert call["render_data"]["items"] == []
    assert call["render_data"]["status"] == "no_data"
    assert "title_contains=아토젯 조건 0건" in call["render_data"]["message"]
    assert "리바로 신규 처방 동향" not in result["answer"]


def test_text_filter_term_that_is_known_brand_is_not_relevance_brand(tmp_path: Path) -> None:
    news = _news(
        {
            "리바로": {
                "data": {
                    "events": [
                        {
                            "title": "리바로 신규 처방 동향",
                            "source": "약업신문",
                            "date": "2026-06-11",
                            "impact_score": 91,
                            "on_list": True,
                            "summary": "본문에도 검색어 없음",
                        }
                    ]
                }
            },
            "아토젯": {"data": {"events": []}},
        }
    )

    result = ChatAgent(news=news, resolver=_resolver(tmp_path, ("리바로", "아토젯"))).answer("리바로 뉴스 제목에 아토젯")

    call = result["tool_calls"][0]
    assert call["applied_filters"] == {"title_contains": "아토젯"}
    assert call["render_data"]["items"] == []


def test_chat_agent_uses_validated_route_brand_metadata(tmp_path: Path) -> None:
    shared = {
        "title": "공동 관련 뉴스",
        "source": "약업신문",
        "date": "2026-06-11",
        "url": "https://news.example/shared",
        "impact_score": 91,
        "on_list": True,
        "summary": "두 브랜드에 같이 수록",
    }
    news = _news(
        {
            "리바로": {"data": {"events": [shared]}},
            "리바로젯": {"data": {"events": [shared]}},
        }
    )

    result = ChatAgent(
        router=RouteBrandRouter(),
        news=news,
        resolver=_resolver(tmp_path, ("리바로", "리바로젯")),
    ).answer("리바로와 리바로젯 둘 다 관련 뉴스")

    call = result["tool_calls"][0]
    assert call["applied_filters"]["relevance_brands"] == "리바로젯 AND 리바로"
    assert [item["title"] for item in call["render_data"]["items"]] == ["공동 관련 뉴스"]


def test_related_news_filters_content_contains_and_or_terms_without_body_leak() -> None:
    sentinel_body = "FULL_BODY_SENTINEL 아토젯 에제티미브 장기 본문"
    news = _news(
        {
            "리바로": {
                "data": {
                    "events": [
                        {
                            "title": "리바로 처방 동향",
                            "source": "약업신문",
                            "date": "2026-06-11",
                            "impact_score": 91,
                            "on_list": True,
                            "summary": "표시용 요약만 노출",
                            "body_full": sentinel_body,
                        },
                        {
                            "title": "리바로 다른 소식",
                            "source": "약업신문",
                            "date": "2026-06-10",
                            "impact_score": 90,
                            "on_list": True,
                            "summary": "아토젯만 있는 요약",
                            "body_full": "본문에는 검색어 없음",
                        },
                    ]
                }
            }
        }
    )

    result = ChatAgent(news=news).answer("리바로 뉴스 본문에 아토젯+에제티미브")

    call = result["tool_calls"][0]
    assert call["applied_filters"]["content_contains"] == "아토젯 AND 에제티미브"
    items = call["render_data"]["items"]
    assert [item["title"] for item in items] == ["리바로 처방 동향"]
    assert "body_full" not in items[0]
    assert sentinel_body not in result["answer"]


def test_brand_resolver_extracts_multiple_related_news_brands_in_order() -> None:
    resolutions = BrandResolver().resolve_many("리바로젯과 리바로 둘 다 관련 뉴스", allow_default=False)

    assert [item.canonical_brand for item in resolutions] == ["리바로젯", "리바로"]


def test_related_news_filters_relevance_and_or_membership() -> None:
    news = _news(
        {
            "리바로": {
                "data": {
                    "events": [
                        {
                            "title": "공동 관련 뉴스",
                            "source": "약업신문",
                            "date": "2026-06-11",
                            "url": "https://news.example/shared",
                            "impact_score": 91,
                            "on_list": True,
                            "summary": "두 브랜드에 같이 수록",
                        },
                        {
                            "title": "리바로 단독 뉴스",
                            "source": "약업신문",
                            "date": "2026-06-10",
                            "url": "https://news.example/livalo-only",
                            "impact_score": 90,
                            "on_list": True,
                            "summary": "리바로 row만 수록",
                        },
                    ]
                }
            },
            "리바로젯": {
                "data": {
                    "events": [
                        {
                            "title": "공동 관련 뉴스",
                            "source": "약업신문",
                            "date": "2026-06-11",
                            "url": "https://news.example/shared",
                            "impact_score": 91,
                            "on_list": True,
                            "summary": "두 브랜드에 같이 수록",
                        },
                        {
                            "title": "리바로젯 단독 뉴스",
                            "source": "약업신문",
                            "date": "2026-06-09",
                            "url": "https://news.example/zet-only",
                            "impact_score": 89,
                            "on_list": True,
                            "summary": "리바로젯 row만 수록",
                        },
                    ]
                }
            },
        }
    )

    and_result = ChatAgent(news=news).answer("리바로젯과 리바로 둘 다 관련 뉴스")
    or_result = ChatAgent(news=news).answer("리바로젯 또는 리바로 관련 뉴스")

    assert [item["title"] for item in and_result["tool_calls"][0]["render_data"]["items"]] == ["공동 관련 뉴스"]
    assert [item["title"] for item in or_result["tool_calls"][0]["render_data"]["items"]] == [
        "공동 관련 뉴스",
        "리바로 단독 뉴스",
        "리바로젯 단독 뉴스",
    ]


def test_related_news_combines_relevance_and_text_no_data_transparently(tmp_path: Path) -> None:
    news = _news(
        {
            "리바로": {
                "data": {
                    "events": [
                        {
                            "title": "공동 관련 뉴스",
                            "source": "약업신문",
                            "date": "2026-06-11",
                            "url": "https://news.example/shared",
                            "impact_score": 91,
                            "on_list": True,
                            "summary": "검색어 없음",
                        }
                    ]
                }
            },
            "아토젯": {"data": {"events": []}},
        }
    )

    result = ChatAgent(news=news, resolver=_resolver(tmp_path, ("리바로", "아토젯"))).answer("리바로 아토젯 둘 다 관련 뉴스 중 제목에 약가")

    call = result["tool_calls"][0]
    assert call["render_data"]["items"] == []
    assert call["render_data"]["status"] == "no_data"
    assert call["applied_filters"]["relevance_brands"] == "리바로 AND 아토젯"
    assert call["applied_filters"]["title_contains"] == "약가"
    assert "relevance_brands=리바로 AND 아토젯 조건 0건" in call["render_data"]["message"]
