from __future__ import annotations

from pipeline.scripts.analysis.brand_activity.auto_topic.models import KeywordRow
from pipeline.scripts.analysis.brand_activity.auto_topic.source_sanitize import sanitize_source_text_carryover


def _row(text: str) -> KeywordRow:
    return KeywordRow(
        row_id=1,
        period_ym="2025-06",
        atc4="C11A1",
        brand="AMOSARTAN Q",
        keyword_text=text,
        interest="NOT AT ALL",
        prescription_frequency="occasionally",
        prescription_evolution="remain unchanged",
        promotional_lit="NO",
        abstract_lit="NO",
        patient_lit="NO",
        specialty="Neuro",
        visit_location="HOSPITAL",
        stage_row_sha256="stage-sha",
    )


def test_sanitize_source_text_carryover_replaces_definition_when_source_text_is_copied() -> None:
    source_text = "가나다라마바사아자차카타파하ABCDEFGHIJ"
    payload = {
        "brand_results": {
            "C11A1:AMOSARTAN Q": {
                "brand_specific_topics": [
                    {
                        "topic_id": "B1",
                        "label": "브랜드 특화",
                        "definition": f"{source_text} 추가 설명",
                        "affected_row_count": 3,
                    }
                ]
            }
        }
    }

    report = sanitize_source_text_carryover(payload, [_row(source_text)])

    topic = payload["brand_results"]["C11A1:AMOSARTAN Q"]["brand_specific_topics"][0]
    assert topic["definition"] == "'브랜드 특화' 관련 브랜드 고유 메시지(원문 인용 제거)"
    assert topic["sanitized"] is True
    assert topic["sanitized_fields"] == ["definition"]
    assert report["sanitized_topic_count"] == 1


def test_sanitize_source_text_carryover_keeps_unmatched_definition_unchanged() -> None:
    payload = {
        "brand_results": {
            "C11A1:AMOSARTAN Q": {
                "brand_specific_topics": [
                    {
                        "topic_id": "B1",
                        "label": "브랜드 특화",
                        "definition": "시장축 밖 브랜드 고유 메시지",
                        "affected_row_count": 3,
                    }
                ]
            }
        }
    }

    report = sanitize_source_text_carryover(payload, [_row("가나다라마바사아자차카타파하ABCDEFGHIJ")])

    topic = payload["brand_results"]["C11A1:AMOSARTAN Q"]["brand_specific_topics"][0]
    assert topic["definition"] == "시장축 밖 브랜드 고유 메시지"
    assert "sanitized" not in topic
    assert report["sanitized_topic_count"] == 0
