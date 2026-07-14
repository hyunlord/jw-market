from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.scripts.crawler.tier2_full_scoring_runner import (
    DEFAULT_DEPLOYMENT_ID,
    DEFAULT_WORKFLOW_ID,
    DEFAULT_WORKFLOW_REV,
    DEFAULT_WORKFLOW_URL,
    PENDING_SOURCE_PROCESSOR,
    MatchedBrand,
    ParsedTier2Score,
    StagedScoreRow,
    build_workflow_payload,
    find_workflow_text,
    insert_live_rows,
    parse_wf324_response,
    score_tier,
    scoped_event_id,
    sync_missing_events_raw,
    update_live_tier2_categories,
)


class RecordingCursor:
    def __init__(self) -> None:
        self.sql = ""
        self.params: list[tuple[object, ...]] = []
        self.rowcount = 0

    def __enter__(self) -> "RecordingCursor":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def executemany(self, sql: str, params: list[tuple[object, ...]]) -> None:
        self.sql = sql
        self.params = params
        self.rowcount = len(params)


class RecordingConnection:
    def __init__(self) -> None:
        self.cursor_obj = RecordingCursor()
        self.commits = 0

    def cursor(self) -> RecordingCursor:
        return self.cursor_obj

    def commit(self) -> None:
        self.commits += 1


class SyncCursor:
    def __init__(self) -> None:
        self.statements: list[str] = []
        self.rowcount = 55

    def __enter__(self) -> "SyncCursor":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, _params: object = None) -> None:
        self.statements.append(sql)
        if "SELECT COUNT(*) AS gap" in sql:
            self.rowcount = 0

    def fetchone(self) -> dict[str, int]:
        return {"gap": 0}


class SyncConnection:
    def __init__(self) -> None:
        self.cursor_obj = SyncCursor()
        self.commits = 0

    def cursor(self) -> SyncCursor:
        return self.cursor_obj

    def commit(self) -> None:
        self.commits += 1


class CategoryCursor:
    def __init__(self) -> None:
        self.sql = ""
        self.params: tuple[object, ...] = ()
        self.rowcount = 7

    def __enter__(self) -> "CategoryCursor":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, sql: str, params: tuple[object, ...]) -> None:
        self.sql = sql
        self.params = params


class CategoryConnection:
    def __init__(self) -> None:
        self.cursor_obj = CategoryCursor()
        self.commits = 0

    def cursor(self) -> CategoryCursor:
        return self.cursor_obj

    def commit(self) -> None:
        self.commits += 1


def test_default_workflow_targets_ga_rebuild() -> None:
    assert DEFAULT_DEPLOYMENT_ID == 1453
    assert DEFAULT_WORKFLOW_ID == 337
    assert DEFAULT_WORKFLOW_REV == 5671
    assert DEFAULT_WORKFLOW_URL == "http://workflow-337.llmops.svc.cluster.local:8080/run/v2"
    assert PENDING_SOURCE_PROCESSOR == "tier2_llm_v2_rev5671"


def test_scoped_event_id_keeps_news_identity_while_avoiding_exact_row_collision() -> None:
    event_id = scoped_event_id("4b9063c28ad45d38", PENDING_SOURCE_PROCESSOR)

    assert event_id == "4b9063c28ad45d38:t2v2r5671"
    assert len(event_id) <= 64


def test_scoped_event_id_hashes_long_news_identity_within_schema_limit() -> None:
    event_id = scoped_event_id("n" * 64, PENDING_SOURCE_PROCESSOR)

    assert event_id.startswith("t2v2:")
    assert len(event_id) <= 64


def test_live_append_uses_plain_insert_and_new_marker() -> None:
    conn = RecordingConnection()
    rows = [
        StagedScoreRow(
            event_id="4b9063c28ad45d38",
            news_id="4b9063c28ad45d38",
            brand_name="스타빅",
            brand_canonical="스타빅",
            score=58,
            score_tier="moderate",
            reason="직접 언급",
            tag="외부/트렌드",
            summary="요약",
            llm_meta="{}",
            collected_at=None,
            expire_at=None,
        )
    ]

    inserted = insert_live_rows(
        conn,
        rows=rows,
        source_processor=PENDING_SOURCE_PROCESSOR,
    )

    assert inserted == 1
    assert "ON DUPLICATE KEY UPDATE" not in conn.cursor_obj.sql
    assert conn.cursor_obj.params[0][0] == "4b9063c28ad45d38:t2v2r5671"
    assert conn.cursor_obj.params[0][6] == PENDING_SOURCE_PROCESSOR
    assert conn.commits == 1


def test_events_raw_sync_inserts_only_missing_rows_and_requires_zero_gap() -> None:
    conn = SyncConnection()

    result = sync_missing_events_raw(conn)
    sql = "\n".join(conn.cursor_obj.statements)

    assert result == {"inserted": 55, "gap": 0}
    assert "LEFT JOIN events_raw e ON e.news_id = n.news_id" in sql
    assert "WHERE e.news_id IS NULL" in sql
    assert "ON DUPLICATE KEY UPDATE" not in sql
    assert "UPDATE events_raw" not in sql
    assert conn.commits == 1


def test_category_refresh_includes_v1_and_v2_but_excludes_tier1_news() -> None:
    conn = CategoryConnection()

    updated = update_live_tier2_categories(conn)

    assert updated == 7
    assert "FROM event_brand_scores" in conn.cursor_obj.sql
    assert "source_processor IN (%s, %s)" in conn.cursor_obj.sql
    assert "tier1.news_id IS NULL" in conn.cursor_obj.sql
    assert "workflow_196_rev5674" in conn.cursor_obj.params
    assert "tier2_llm_v1" in conn.cursor_obj.params
    assert "tier2_llm_v2_rev5671" in conn.cursor_obj.params
    assert conn.commits == 1


def test_category_refresh_cronjob_is_registered_suspended() -> None:
    root = Path(__file__).resolve().parents[2]
    manifest = (root / "deploy/k8s/crawler/tier2-category-refresh-cronjob.yaml").read_text(
        encoding="utf-8"
    )

    assert "suspend: true" in manifest
    assert "refresh-live-categories" in manifest
    assert "append-live" not in manifest
    assert "replace-live" not in manifest


def _brands() -> list[MatchedBrand]:
    return [
        MatchedBrand(
            brand_key="brand-a",
            brand_name="프랄런트",
            match_source="body",
            matched_keywords=("프랄런트",),
        ),
        MatchedBrand(
            brand_key="brand-b",
            brand_name="레파타",
            match_source="body",
            matched_keywords=("레파타",),
        ),
    ]


def test_build_payload_uses_target_brands_as_upper_bound() -> None:
    payload = build_workflow_payload(
        news_id="news-1",
        title="PCSK9 시장 경쟁",
        body="프랄런트와 레파타가 함께 언급됐다.",
        source_name="test",
        article_url="https://example.test/a",
        published_date="2026-07-01",
        brands=_brands(),
    )

    assert payload["article"]["news_id"] == "news-1"
    assert [row["brand_key"] for row in payload["target_brands"]] == ["brand-a", "brand-b"]
    assert payload["target_brands"][0]["matched_keywords"] == ["프랄런트"]


def test_find_workflow_text_prefers_data_text_over_echoed_question() -> None:
    raw = {
        "data": {
            "text": "```json\n{\"ok\": true}\n```",
            "agentFlowExecutedData": [
                {"data": {"input": {"question": "{\"echo\": true}"}}},
                {"data": {"output": {"content": "{\"ok\": true}"}}},
            ],
        }
    }

    assert find_workflow_text(raw) == "```json\n{\"ok\": true}\n```"


def test_parse_response_returns_category_and_brand_scores() -> None:
    raw = json.dumps(
        {
            "tag": "신약/R&D",
            "category_label": "신약/R&D",
            "category_code": "rd",
            "brand_scores": [
                {"brand_key": "brand-a", "brand_name": "프랄런트", "score": 65, "reason": "직접 비교"},
                {"brand_key": "brand-b", "brand_name": "레파타", "score": 42, "reason": "보조 언급"},
            ],
        },
        ensure_ascii=False,
    )

    parsed = parse_wf324_response(raw, _brands())

    assert parsed.category_label == "신약/R&D"
    assert parsed.category_code == "rd"
    assert parsed.scores == (
        ParsedTier2Score("brand-a", "프랄런트", 65, "직접 비교"),
        ParsedTier2Score("brand-b", "레파타", 42, "보조 언급"),
    )


def test_parse_response_rejects_out_of_candidate_brand() -> None:
    raw = json.dumps(
        {
            "tag": "정책/규제",
            "category_label": "정책/규제",
            "category_code": "policy",
            "brand_scores": [
                {"brand_key": "brand-a", "brand_name": "프랄런트", "score": 70, "reason": "직접"},
                {"brand_key": "brand-x", "brand_name": "후보밖", "score": 60, "reason": "초과"},
            ],
        },
        ensure_ascii=False,
    )

    with pytest.raises(ValueError, match="out-of-candidate"):
        parse_wf324_response(raw, _brands())


def test_parse_response_rejects_missing_candidate_brand() -> None:
    raw = json.dumps(
        {
            "tag": "정책/규제",
            "category_label": "정책/규제",
            "category_code": "policy",
            "brand_scores": [
                {"brand_key": "brand-a", "brand_name": "프랄런트", "score": 70, "reason": "직접"}
            ],
        },
        ensure_ascii=False,
    )

    with pytest.raises(ValueError, match="omitted"):
        parse_wf324_response(raw, _brands())


def test_parse_response_rejects_invalid_category_code_pair() -> None:
    raw = json.dumps(
        {
            "tag": "신약/R&D",
            "category_label": "신약/R&D",
            "category_code": "policy",
            "brand_scores": [
                {"brand_key": "brand-a", "brand_name": "프랄런트", "score": 65, "reason": "직접"},
                {"brand_key": "brand-b", "brand_name": "레파타", "score": 42, "reason": "보조"},
            ],
        },
        ensure_ascii=False,
    )

    with pytest.raises(ValueError, match="category_code"):
        parse_wf324_response(raw, _brands())


def test_score_tier_matches_existing_wf196_thresholds() -> None:
    assert score_tier(0) == "very_weak"
    assert score_tier(30) == "weak"
    assert score_tier(50) == "moderate"
    assert score_tier(70) == "strong"
    assert score_tier(85) == "very_strong"
