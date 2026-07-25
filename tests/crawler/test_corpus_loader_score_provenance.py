from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
from pathlib import Path


MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "pipeline"
    / "scripts"
    / "agent_2"
    / "corpus_loader.py"
)
SPEC = importlib.util.spec_from_file_location("corpus_loader", MODULE_PATH)
assert SPEC is not None
assert SPEC.loader is not None
corpus_loader = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = corpus_loader
SPEC.loader.exec_module(corpus_loader)


def test_build_rows_preserves_score_provenance(tmp_path: Path) -> None:
    # Given
    catalog = tmp_path / "catalog.json"
    catalog.write_text(
        '{"리바로":{"brand_id":"brand-livalo","is_jw":true}}',
        encoding="utf-8",
    )
    resolver = corpus_loader.BrandResolver(catalog)
    article_path = tmp_path / "news_5years_hitnews_processed" / "article.json"
    item = {
        "date": "2026-07-24",
        "tag": "정책/규제",
        "title": "리바로 정책 뉴스",
        "matches": [{"drug": "리바로", "score": 58, "reason": "직접 언급"}],
    }

    # When
    _, event, scores = corpus_loader.build_rows(
        article_path,
        item,
        resolver,
        processed_by="workflow_196_rev5674",
        tier=1,
        collected_at=datetime(2026, 7, 24, 12, 0, 0),
    )

    # Then
    assert scores == [
        {
            "event_id": event["event_id"],
            "news_id": event["news_id"],
            "brand_name": "리바로",
            "brand_canonical": "리바로",
            "brand_id": "brand-livalo",
            "ml_id": None,
            "cd_id": None,
            "is_jw": 1,
            "score": 58,
            "score_tier": "brief_paragraph",
            "reason": "직접 언급",
            "source_processor": "workflow_196_rev5674",
            "derivation": "llm_direct",
            "tag": "정책/규제",
            "tier": 1,
            "collected_at": "2026-07-24 12:00:00",
        }
    ]
