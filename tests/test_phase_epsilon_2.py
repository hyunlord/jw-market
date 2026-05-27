from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_loader_builds_rows_from_option_b_scored_json(tmp_path: Path) -> None:
    loader = load_module(
        "corpus_loader_v2",
        ROOT / "pipeline" / "scripts" / "agent_2" / "corpus_loader_v2.py",
    )
    batch_dir = tmp_path / "news_2026-05"
    raw_path = batch_dir / "dgx" / "news_5years_test" / "article.json"
    scored_path = batch_dir / "_scored" / "dgx" / "article_scored.json"
    raw_path.parent.mkdir(parents=True)
    scored_path.parent.mkdir(parents=True)
    raw = {
        "title": "가드메트 임상 데이터 공개",
        "content": "가드메트와 제미메트가 함께 언급됐다.",
        "date": "2026-05-07",
        "search_keyword": "가드메트",
        "matched_search_keywords": ["가드메트", "제미메트"],
        "matched_jw_search_contexts": [
            {"jw_brand": "가드메트", "matched_keywords": ["가드메트", "제미메트"]}
        ],
        "sources": [{"source": "테스트뉴스", "url": "https://example.test/news"}],
    }
    scored = {
        "source_path": "dgx/news_5years_test/article.json",
        "scored_at": "2026-05-24T06:00:00Z",
        "workflow_id": 196,
        "catalog_version": "catalog-sha1",
        "tag": "기타",
        "summary": "요약",
        "matches": [
            {"drug": "가드메트", "score": 41, "reason": "직접 관련"},
            {"drug": "가드렛", "score": 86, "reason": "강한 관련"},
        ],
        "llm_meta": {"duration_sec": 1.2},
    }
    raw_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    scored_path.write_text(json.dumps(scored, ensure_ascii=False), encoding="utf-8")

    catalog = loader.CatalogResolver({"가드메트": "desc", "가드렛": "desc"})
    records = loader.build_records(batch_dir, scored_path, catalog, workflow_id=196)

    assert len(records.news["news_id"]) == 16
    assert all(ch in "0123456789abcdef" for ch in records.news["news_id"])
    assert records.event["event_id"] == records.news["news_id"]
    assert records.event["category"] == "external"
    assert records.event["category_label"] == "기타"
    assert records.news["source_name"] == "테스트뉴스"
    assert records.news["scored"] == 1
    assert records.scores[0]["brand_canonical"] == "가드메트"
    assert records.scores[0]["source_processor"] == "workflow_196_optionB"
    assert records.scores[0]["derivation"] == "llm_direct"
    assert records.scores[0]["score_tier"] == "weak"
    assert records.scores[1]["score_tier"] == "very_strong"


def test_cross_match_derives_non_jw_keyword_average_rows() -> None:
    adapter = load_module(
        "cross_match_adapter",
        ROOT / "pipeline" / "scripts" / "agent_2" / "cross_match_adapter.py",
    )
    contexts = [
        {"jw_brand": "가드메트", "matched_keywords": ["가드메트", "제미메트", "가드렛"]},
        {"jw_brand": "가드렛", "matched_keywords": ["트라젠타", "제미메트"]},
        {"jw_brand": "리바로젯", "matched_keywords": ["아토젯"]},
    ]
    direct_scores = [
        {"brand_name": "가드메트", "score": 40},
        {"brand_name": "가드렛", "score": 80},
    ]

    rows = adapter.derive_cross_match_rows(
        news_id="abc123",
        event_id="abc123",
        contexts=contexts,
        direct_scores=direct_scores,
        jw25={"가드메트", "가드렛", "리바로젯"},
    )

    by_brand = {row["brand_name"]: row for row in rows}
    assert set(by_brand) == {"제미메트", "트라젠타"}
    assert by_brand["제미메트"]["score"] == 60
    assert by_brand["제미메트"]["mirrored_from_jw_brands"] == ["가드렛", "가드메트"]
    assert by_brand["제미메트"]["derivation"] == "cross_match"
    assert by_brand["제미메트"]["is_jw"] == 0
    assert by_brand["트라젠타"]["score"] == 80
