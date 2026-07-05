from __future__ import annotations

from datetime import date

from jw_chat_agent_poc import ChatAgent
from jw_chat_agent_poc.orchestrator.markdown_response import MarkdownResponseBuilder
from jw_chat_agent_poc.service.genos_client import GenosClient
from jw_chat_agent_poc.tools.deep_analysis import (
    DeepAnalysisNewsTool,
    StaticDeepAnalysisNewsReader,
)
from jw_chat_agent_poc.tools.deep_analysis.news_corpus import events_from_corpus_rows


def test_corpus_rows_use_full_events_schema_fields() -> None:
    rows = [
        {
            "event_id": 42,
            "date": "2026-04-12",
            "title": "리바로젯 경쟁 구도 기사",
            "summary": "복합제 경쟁 구도를 다룬 기사",
            "body_full": "리바로젯과 아토젯의 시장 흐름을 함께 설명했다.",
            "source_name": "약업신문",
            "source_url": "https://news.example/full-corpus",
            "category_label": "경쟁",
            "impact_score": 77.5,
        }
    ]

    events = events_from_corpus_rows(rows)

    assert len(events) == 1
    event = events[0]
    assert event.title == "리바로젯 경쟁 구도 기사"
    assert event.source == "약업신문"
    assert event.url == "https://news.example/full-corpus"
    assert event.summary == "복합제 경쟁 구도를 다룬 기사"
    assert event.body_full == "리바로젯과 아토젯의 시장 흐름을 함께 설명했다."
    assert event.category == "경쟁"
    assert event.impact_score == 77.5


def test_corpus_rows_preserve_database_date_objects() -> None:
    rows = [
        {
            "event_id": 43,
            "date": date(2026, 6, 2),
            "title": "리바로 날짜 보존 기사",
            "summary": "날짜 타입 회귀 테스트",
            "body_full": "DB DATE 객체가 문자열로 보존되어야 한다.",
            "source_name": "의학신문",
            "source_url": "https://news.example/date-object",
            "category_label": "실적",
            "impact_score": 90.0,
        }
    ]

    events = events_from_corpus_rows(rows)

    assert len(events) == 1
    assert events[0].date == "2026-06-02"


def test_related_news_answer_uses_curated_events_without_metrics() -> None:
    news = DeepAnalysisNewsTool(
        reader=StaticDeepAnalysisNewsReader(
            {
                "리바로": {
                    "data": {
                        "events": [
                            {
                                "title": "리바로 신규 처방 동향",
                                "source": "메디컬타임즈",
                                "date": "2026-06-11",
                                "url": "https://news.example/livalo",
                                "impact_score": 91,
                                "on_list": True,
                                "summary": "리바로 처방 관련 기사 원문 요약",
                            },
                            {
                                "title": "낮은 영향도 기사",
                                "source": "테스트뉴스",
                                "date": "2026-06-10",
                                "impact_score": 12,
                                "on_list": False,
                            },
                        ]
                    }
                }
            }
        )
    )

    result = ChatAgent(news=news).answer("리바로 관련 뉴스")

    assert result["sources"] == ["deep_analysis_events"]
    assert result["decomposition"][0]["sources"] == ("deep_analysis_events",)
    assert [call["tool"] for call in result["tool_calls"]] == ["deep_analysis_related_news"]
    assert "get_brand_metric" not in {call["tool"] for call in result["tool_calls"]}
    assert "2026-06-11" in result["answer"]
    assert "리바로 신규 처방 동향" in result["answer"]
    assert "메디컬타임즈" in result["answer"]
    assert "91" in result["answer"]
    assert "낮은 영향도 기사" not in result["answer"]
    assert "뉴스/이슈" in result["answer"]
    assert "내부 심층분석" not in result["answer"]
    assert "cache(events)" not in result["answer"]


def test_related_news_applies_source_filter_from_question() -> None:
    news = DeepAnalysisNewsTool(
        reader=StaticDeepAnalysisNewsReader(
            {
                "리바로": {
                    "data": {
                        "events": [
                            {
                                "title": "약업신문 리바로 기사",
                                "source": "약업신문",
                                "date": "2026-06-11",
                                "impact_score": 91,
                                "on_list": True,
                            },
                            {
                                "title": "데일리팜 리바로 기사",
                                "source": "데일리팜",
                                "date": "2026-06-10",
                                "impact_score": 92,
                                "on_list": True,
                            },
                        ]
                    }
                }
            }
        )
    )

    result = ChatAgent(news=news).answer("리바로 뉴스 약업신문 것만")

    call = result["tool_calls"][0]
    assert call["deterministic"] is True
    assert call["applied_filters"] == {"source": "약업신문"}
    assert [item["source"] for item in call["render_data"]["items"]] == ["약업신문"]
    assert "약업신문 리바로 기사" in result["answer"]
    assert "데일리팜 리바로 기사" not in result["answer"]


def test_related_news_applies_recent_month_and_impact_filters() -> None:
    news = DeepAnalysisNewsTool(
        reader=StaticDeepAnalysisNewsReader(
            {
                "리바로": {
                    "data": {
                        "events": [
                            {
                                "title": "최근 고영향 기사",
                                "source": "약업신문",
                                "date": "2026-06-11",
                                "impact_score": 91,
                                "on_list": True,
                            },
                            {
                                "title": "최근 저영향 기사",
                                "source": "약업신문",
                                "date": "2026-06-10",
                                "impact_score": 20,
                                "on_list": True,
                            },
                            {
                                "title": "오래된 고영향 기사",
                                "source": "약업신문",
                                "date": "2026-05-01",
                                "impact_score": 99,
                                "on_list": True,
                            },
                        ]
                    }
                }
            }
        )
    )

    recent_result = ChatAgent(news=news).answer("리바로 최근 한 달 뉴스")
    impact_result = ChatAgent(news=news).answer("리바로 중요한 뉴스만")

    recent_items = recent_result["tool_calls"][0]["render_data"]["items"]
    impact_items = impact_result["tool_calls"][0]["render_data"]["items"]
    assert [item["title"] for item in recent_items] == ["최근 고영향 기사", "최근 저영향 기사"]
    assert impact_result["tool_calls"][0]["applied_filters"] == {"min_impact_score": 60}
    assert [item["title"] for item in impact_items] == ["오래된 고영향 기사", "최근 고영향 기사"]


def test_related_news_reports_unsupported_source_filter() -> None:
    news = DeepAnalysisNewsTool(
        reader=StaticDeepAnalysisNewsReader(
            {
                "리바로": {
                    "data": {
                        "events": [
                            {
                                "title": "약업신문 리바로 기사",
                                "source": "약업신문",
                                "date": "2026-06-11",
                                "impact_score": 91,
                                "on_list": True,
                            }
                        ]
                    }
                }
            }
        )
    )

    result = ChatAgent(news=news).answer("리바로 OOO신문 뉴스")

    call = result["tool_calls"][0]
    assert call["applied_filters"] == {}
    assert call["unsupported_filters"] == [{"field": "source", "value": "OOO신문", "reason": "지원하지 않는 뉴스 출처"}]
    assert call["render_data"]["items"] == []
    assert "지원하지 않는 뉴스 출처" in result["answer"]


def test_related_news_title_text_search_is_fail_loud_not_generic_news() -> None:
    news = DeepAnalysisNewsTool(
        reader=StaticDeepAnalysisNewsReader(
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
                            }
                        ]
                    }
                }
            }
        )
    )

    result = ChatAgent(news=news).answer("리바로 뉴스 중 제목에 아토젯 있는거")

    call = result["tool_calls"][0]
    assert call["applied_filters"] == {"title_contains": "아토젯"}
    assert call["unsupported_filters"] == []
    assert call["render_data"]["items"] == []
    assert "title_contains=아토젯 조건 0건" in result["answer"]
    assert "리바로 신규 처방 동향" not in result["answer"]


def test_related_news_shows_body_match_excerpt_when_title_and_summary_do_not_match() -> None:
    news = DeepAnalysisNewsTool(
        reader=StaticDeepAnalysisNewsReader(
            {
                "리바로": {
                    "data": {
                        "events": [
                            {
                                "title": "리바로 분기 매출 500억원 돌파",
                                "source": "약업신문",
                                "date": "2026-04-12",
                                "impact_score": 82,
                                "on_list": True,
                                "summary": "리바로 매출 흐름을 정리한 기사",
                                "body_full": "종근당 고지혈증 치료제 아토젯 261억원, 리바로는 500억원대 매출을 기록했다.",
                            }
                        ]
                    }
                }
            }
        )
    )

    call = news.related_news("리바로", filter_entries=(("text_contains", "아토젯"),))
    markdown = MarkdownResponseBuilder().build(brand="리바로", calls=[call], sources=["deep_analysis_events"]).markdown

    assert call["applied_filters"] == {"text_contains": "아토젯"}
    assert call["render_data"]["items"][0]["match_excerpt"]
    assert "아토젯 261억원" in call["render_data"]["items"][0]["match_excerpt"]
    assert "매칭 발췌" in markdown
    assert "아토젯 261억원" in markdown
    interpretation = markdown.split("## 데이터", maxsplit=1)[0]
    assert "cache" not in interpretation
    assert "내부 심층분석" not in interpretation


def test_related_news_gracefully_reports_empty_events() -> None:
    news = DeepAnalysisNewsTool(reader=StaticDeepAnalysisNewsReader({"리바로": {"data": {"events": []}}}))

    result = ChatAgent(news=news).answer("리바로 소식")

    assert result["sources"] == ["deep_analysis_events"]
    assert "관련 뉴스 없음" in result["answer"]


def test_news_answers_are_treated_as_deterministic_cache_outputs() -> None:
    agent_result = {
        "answer": "## 답변\n\n뉴스 원문 표",
        "sources": ["deep_analysis_events"],
        "tool_calls": [{"source": "deep_analysis_events"}],
    }

    assert "".join(GenosClient(token="dummy-token").stream_answer("리바로 뉴스", agent_result)) == agent_result["answer"]
