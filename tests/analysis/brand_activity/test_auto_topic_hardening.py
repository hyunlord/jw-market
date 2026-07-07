from __future__ import annotations

import json
import sys
import time
import types

try:
    import httpx2 as _httpx2  # noqa: F401
except ModuleNotFoundError:
    fake_httpx2 = types.ModuleType("httpx2")
    fake_httpx2.HTTPError = RuntimeError
    sys.modules["httpx2"] = fake_httpx2

from pipeline.scripts.analysis.brand_activity.auto_topic import execution, llm
from pipeline.scripts.analysis.brand_activity.auto_topic.models import BrandDescription, CallLog, JsonValue, KeywordRow, ModelSpec
from pipeline.scripts.analysis.brand_activity.auto_topic.response import normalize_share_payload


def _row(row_id: int, atc4: str = "A", brand: str = "BRAND") -> KeywordRow:
    return KeywordRow(
        row_id=row_id,
        period_ym="2025-10",
        atc4=atc4,
        brand=brand,
        keyword_text="bounded sample message",
        interest="VERY USEFUL",
        prescription_frequency="increase",
        prescription_evolution="increase",
        promotional_lit="YES",
        abstract_lit="NO",
        patient_lit="NO",
        specialty="내과",
        visit_location="clinic",
        stage_row_sha256=f"hash-{row_id}",
    )


def _description(atc4: str, brand: str) -> BrandDescription:
    return BrandDescription(
        brand=brand,
        atc4=atc4,
        kr_canonical=None,
        is_jw=False,
        molecule=(),
        manufacturer=(),
        representing_company=(),
    )


def _log(task: str, atc4: str, brand: str, status: str = "ok") -> CallLog:
    return CallLog(
        task=task,
        model_key="flash",
        serving_id="163",
        scope_id=f"atc4:{atc4}",
        atc4=atc4,
        brand=brand,
        status=status,
        latency_ms=1,
        prompt_tokens=0,
        completion_tokens=0,
        total_tokens=0,
        estimated_input_tokens=0,
        input_hash=f"{task}:{atc4}:{brand}",
        output_sha256="",
        output_length=0,
    )


def test_call_genos_json_watchdog_returns_error_when_client_stalls(monkeypatch) -> None:
    class SlowClient:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def chat(self, _messages: list[dict[str, str]]) -> dict[str, JsonValue]:
            time.sleep(0.25)
            return {
                "status": "ok",
                "latency_ms": 250,
                "content": json.dumps({"topics": []}),
                "usage": {},
                "error_type": "",
                "error_message": "",
            }

    monkeypatch.setenv("GENOS_WATCHDOG_TIMEOUT_S", "0.05")
    monkeypatch.delenv("GENOS_LLM_BACKEND", raising=False)
    monkeypatch.setattr(llm, "DirectServingClient", SlowClient)

    payload, log = llm.call_genos_json(
        token="",
        spec=ModelSpec("flash", "163", "Flash Lite"),
        task="market_axis",
        scope_id="atc4:A",
        atc4="A",
        brand="*",
        messages=[{"role": "user", "content": "no raw source"}],
        rows=[_row(1)],
        input_hash="hash",
    )

    assert payload["status"] == "error"
    assert log.status == "error"
    assert log.error_type == "CallWatchdogTimeout"
    assert log.backend == "direct_serving"


def test_call_genos_json_retries_429_and_logs_attempts(monkeypatch) -> None:
    calls = {"count": 0}

    def fake_watchdog(**_kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            return {
                "status": "error",
                "serving_id": "163",
                "latency_ms": 1,
                "ttfb_ms": 1,
                "read_ms": 0,
                "phase": "ttfb",
                "content": "",
                "usage": {},
                "error_type": "HTTPStatusError",
                "error_message": "429 Too Many Requests",
            }
        return {
            "status": "ok",
            "serving_id": "163",
            "latency_ms": 2,
            "ttfb_ms": 1,
            "read_ms": 1,
            "phase": "complete",
            "content": json.dumps({"topics": [{"topic_id": "T1", "label": "효능", "definition": "효능", "keywords": []}]}),
            "usage": {"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7},
            "error_type": "",
            "error_message": "",
        }

    monkeypatch.setenv("GENOS_MAX_RETRIES", "1")
    monkeypatch.setenv("GENOS_CALL_PACING_MS", "0")
    monkeypatch.setattr(llm, "_chat_with_process_watchdog", fake_watchdog)
    monkeypatch.setattr(llm, "_retry_delay_s", lambda _input_hash, _attempt: 0.0)

    payload, log = llm.call_genos_json(
        token="",
        spec=ModelSpec("flash", "163", "Flash Lite"),
        task="market_axis",
        scope_id="atc4:A",
        atc4="A",
        brand="*",
        messages=[{"role": "user", "content": "no raw source"}],
        rows=[_row(1)],
        input_hash="hash",
    )

    assert payload["status"] == "ok"
    assert calls["count"] == 2
    assert log.retry_count == 1
    assert log.retry_reasons == ("http_429",)


def test_call_genos_json_uses_direct_serving_backend_alias(monkeypatch) -> None:
    captured: dict[str, JsonValue] = {}

    def fake_watchdog(**kwargs):
        captured.update(kwargs)
        return {
            "status": "ok",
            "serving_id": "163",
            "latency_ms": 2,
            "ttfb_ms": 1,
            "read_ms": 1,
            "phase": "complete",
            "content": json.dumps({"topics": [{"topic_id": "T1", "label": "효능", "definition": "효능", "keywords": []}]}),
            "usage": {"prompt_tokens": 4, "completion_tokens": 3, "total_tokens": 7},
            "error_type": "",
            "error_message": "",
        }

    monkeypatch.setenv("GENOS_LLM_BACKEND", "direct_serving")
    monkeypatch.setenv("GENOS_DIRECT_BASE_URL", "https://jwai-dev.jwhealthcare.com")
    monkeypatch.setenv("GENOS_DIRECT_MODEL_FLASH", "genos-flash")
    monkeypatch.setenv("GENOS_CALL_PACING_MS", "0")
    monkeypatch.setattr(llm, "_chat_with_process_watchdog", fake_watchdog)

    payload, log = llm.call_genos_json(
        token="transient-token",
        spec=ModelSpec("flash", "163", "Flash Lite"),
        task="market_axis",
        scope_id="atc4:A",
        atc4="A",
        brand="*",
        messages=[{"role": "user", "content": "no raw source"}],
        rows=[_row(1)],
        input_hash="hash",
    )

    backend = captured["backend"]

    assert payload["status"] == "ok"
    assert backend.backend_key == "direct_serving"
    assert backend.base_url == "https://jwai-dev.jwhealthcare.com"
    assert backend.model_id == "genos-flash"
    assert backend.serving_id == "163"
    assert log.backend == "direct_serving"
    assert log.model_id == "genos-flash"


def test_call_log_to_json_includes_direct_serving_metadata() -> None:
    log = _log("market_axis", "A", "*")
    direct_log = CallLog(
        task=log.task,
        model_key=log.model_key,
        serving_id=log.serving_id,
        scope_id=log.scope_id,
        atc4=log.atc4,
        brand=log.brand,
        status=log.status,
        latency_ms=log.latency_ms,
        prompt_tokens=log.prompt_tokens,
        completion_tokens=log.completion_tokens,
        total_tokens=log.total_tokens,
        estimated_input_tokens=log.estimated_input_tokens,
        input_hash=log.input_hash,
        output_sha256=log.output_sha256,
        output_length=log.output_length,
        backend="direct_serving",
        endpoint="https://jwai-dev.jwhealthcare.com/api/gateway/rep/serving/163/chat/completions",
        model_id="genos-flash",
    )

    serialized = llm.call_log_to_json(direct_log)

    assert serialized["backend"] == "direct_serving"
    assert serialized["endpoint"] == "https://jwai-dev.jwhealthcare.com/api/gateway/rep/serving/163/chat/completions"
    assert serialized["model_id"] == "genos-flash"


def test_execute_calls_quarantines_market_when_axis_call_errors(monkeypatch) -> None:
    share_calls: list[tuple[str, str]] = []

    def fake_axis(_token, _dictionary, atc4, _scope_metadata, _rows, *, task, model_key):
        if atc4 == "A":
            return {"status": "error", "error_type": "CallWatchdogTimeout"}, _log(task, atc4, "*", "error")
        return {
            "status": "ok",
            "topics": [
                {"topic_id": "T1", "label": "효능", "definition": "효능", "keywords": ["효능"]},
                {"topic_id": "T2", "label": "안전성", "definition": "안전성", "keywords": ["안전성"]},
                {"topic_id": "T3", "label": "편의", "definition": "편의", "keywords": ["편의"]},
            ],
        }, _log(task, atc4, "*", "ok")

    def fake_share(_token, _scope_key, _scope_metadata, atc4, brand, _rows, _description, _topics, _brand_specific_topics, _model_key, *, task):
        share_calls.append((atc4, brand))
        return {
            "status": "ok",
            "brand": brand,
            "atc4": atc4,
            "topic_shares": [{"topic_id": "T1", "label": "효능", "affected_row_count": len(_rows)}],
        }, _log(task, atc4, brand, "ok")

    monkeypatch.setattr(execution, "_call_axis", fake_axis)
    monkeypatch.setattr(execution, "_call_share", fake_share)

    result = execution.execute_calls(
        token="",
        dictionary={},
        axis_samples={"A": [_row(1, "A", "A_BRAND")], "B": [_row(2, "B", "B_BRAND")]},
        brand_samples={"A:A_BRAND": [_row(3, "A", "A_BRAND")], "B:B_BRAND": [_row(4, "B", "B_BRAND")]},
        descriptions={"A:A_BRAND": _description("A", "A_BRAND"), "B:B_BRAND": _description("B", "B_BRAND")},
        markets=("A", "B"),
        large_markets=(),
    )

    assert ("A", "A_BRAND") not in share_calls
    assert ("B", "B_BRAND") in share_calls
    assert result.brand_results["A:A_BRAND"]["status"] == "quarantined_axis_failed"


def test_execute_calls_generates_brand_specific_topics_before_fixed_vocab_share(monkeypatch) -> None:
    observed: dict[str, JsonValue] = {"brand_specific_rows": 0, "share_topic_ids": []}

    def fake_axis(_token, _dictionary, atc4, _scope_metadata, _rows, *, task, model_key):
        return {
            "status": "ok",
            "topics": [
                {"topic_id": "T1", "label": "효능", "definition": "효능", "keywords": ["효능"]},
                {"topic_id": "T2", "label": "안전성", "definition": "안전성", "keywords": ["안전성"]},
                {"topic_id": "T3", "label": "편의", "definition": "편의", "keywords": ["편의"]},
            ],
        }, _log(task, atc4, "*", "ok")

    def fake_brand_specific(_token, _scope_key, _scope_metadata, atc4, brand, rows, _description, _market_topics, _model_key, *, task):
        observed["brand_specific_rows"] = len(rows)
        return [execution.TopicDefinition("B1", "제형 편의", "제형 편의", ())], _log(task, atc4, brand, "ok")

    def fake_share(_token, _scope_key, _scope_metadata, atc4, brand, _rows, _description, topics, brand_specific_topics, _model_key, *, task):
        observed["share_topic_ids"] = [topic.topic_id for topic in topics] + [topic.topic_id for topic in brand_specific_topics]
        return {
            "status": "ok",
            "brand": brand,
            "atc4": atc4,
            "topic_shares": [{"topic_id": "T1", "label": "효능", "affected_row_count": len(_rows)}],
            "brand_specific_topics": [{"topic_id": "B1", "label": "제형 편의", "affected_row_count": len(_rows)}],
        }, _log(task, atc4, brand, "ok")

    monkeypatch.setattr(execution, "_call_axis", fake_axis)
    monkeypatch.setattr(execution, "_call_brand_specific_axis", fake_brand_specific)
    monkeypatch.setattr(execution, "_call_share", fake_share)

    result = execution.execute_calls(
        token="",
        dictionary={},
        axis_samples={"A": [_row(1, "A", "A_BRAND")]},
        brand_samples={"A:A_BRAND": [_row(2, "A", "A_BRAND"), _row(3, "A", "A_BRAND")]},
        brand_axis_samples={"A:A_BRAND": [_row(3, "A", "A_BRAND")]},
        descriptions={"A:A_BRAND": _description("A", "A_BRAND")},
        markets=("A",),
        large_markets=(),
    )

    assert observed["brand_specific_rows"] == 1
    assert observed["share_topic_ids"] == ["T1", "T2", "T3", "B1"]
    assert result.brand_results["A:A_BRAND"]["brand_specific_topics"][0]["topic_id"] == "B1"


def test_aggregate_share_batches_sums_affected_rows_without_normalizing() -> None:
    topics = [
        execution.TopicDefinition("T1", "효능", "효능 메시지", ()),
        execution.TopicDefinition("T2", "안전성", "안전성 메시지", ()),
    ]
    successful_left = {
        "status": "ok",
        "row_count": 100,
        "topic_shares": [{"topic_id": "T1", "label": "효능", "affected_row_count": 60}],
    }
    failed_middle = {
        "status": "error",
        "row_count": 200,
        "reason": "ReadTimeout",
        "topic_shares": [],
    }
    successful_right = {
        "status": "ok",
        "row_count": 100,
        "topic_shares": [{"topic_id": "T2", "label": "안전성", "affected_row_count": 50}],
    }

    aggregate = execution._aggregate_share_batches(
        [successful_left, failed_middle, successful_right],
        brand="JAQBO",
        atc4="L04D0",
        scope_id="atc4:L04D0",
        axis_version="v1",
        topics=topics,
        token_budget=8000,
    )

    total_pct = round(sum(float(row["share_pct"]) for row in aggregate["topic_shares"]), 1)
    guard = execution.mechanical_guard(aggregate, valid_topic_ids={"T1", "T2"}, brand_total_rows=aggregate["row_count"])

    assert aggregate["status"] == "ok"
    assert aggregate["partial_failure"] is True
    assert aggregate["row_count"] == 400
    assert aggregate["source_row_count"] == 400
    assert aggregate["denominator"] == "brand_total_row_count"
    assert total_pct == 27.5
    assert "etc_pct" not in aggregate
    assert guard["status"] == "pass"


def test_aggregate_share_batches_backfills_missing_topic_id_from_label() -> None:
    topics = [
        execution.TopicDefinition("T1", "효능", "효능 메시지", ()),
        execution.TopicDefinition("T2", "안전성", "안전성 메시지", ()),
    ]
    batch = {
        "status": "ok",
        "row_count": 100,
        "topic_shares": [
            {"topic_id": "", "label": " 효 능 ", "affected_row_count": 60},
            {"topic_id": None, "label": "안전성", "affected_row_count": 25},
        ],
    }

    aggregate = execution._aggregate_share_batches(
        [batch],
        brand="RABEKHAN DUO",
        atc4="A02B2",
        scope_id="atc4:A02B2",
        axis_version="v1",
        topics=topics,
        token_budget=5000,
    )
    guard = execution.mechanical_guard(aggregate, valid_topic_ids={"T1", "T2"}, brand_total_rows=aggregate["row_count"])

    assert [row["topic_id"] for row in aggregate["topic_shares"]] == ["T1", "T2"]
    assert aggregate["topic_id_backfill_count"] == 2
    assert guard["status"] == "pass"


def test_aggregate_share_batches_keeps_top_two_brand_specific_topics() -> None:
    topics = [execution.TopicDefinition("T1", "효능", "효능 메시지", ())]
    left = {
        "status": "ok",
        "row_count": 50,
        "topic_shares": [{"topic_id": "T1", "label": "효능", "affected_row_count": 20}],
        "brand_specific_topics": [{"topic_id": "B1", "label": "제형 편의", "affected_row_count": 15}],
    }
    right = {
        "status": "ok",
        "row_count": 50,
        "topic_shares": [{"topic_id": "T1", "label": "효능", "affected_row_count": 10}],
        "brand_specific_topics": [
            {"topic_id": "B1", "label": "제형 편의", "affected_row_count": 10},
            {"topic_id": "B2", "label": "보험 메시지", "affected_row_count": 5},
            {"topic_id": "B3", "label": "초과 특화", "affected_row_count": 5},
        ],
    }

    aggregate = execution._aggregate_share_batches(
        [left, right],
        brand="WINUF",
        atc4="K01D2",
        scope_id="atc4:K01D2",
        axis_version="v1",
        topics=topics,
        token_budget=5000,
    )

    assert [row["label"] for row in aggregate["brand_specific_topics"]] == ["제형 편의", "보험 메시지"]
    assert aggregate["brand_specific_topics"][0]["affected_row_count"] == 25
    assert aggregate["brand_specific_topics"][0]["share_pct"] == 25.0


def test_aggregate_share_batches_merges_duplicate_brand_specific_before_top_two() -> None:
    topics = [execution.TopicDefinition("T1", "효능", "효능 메시지", ())]
    batch = {
        "status": "ok",
        "row_count": 100,
        "topic_shares": [{"topic_id": "T1", "label": "효능", "affected_row_count": 55}],
        "brand_specific_topics": [
            {"topic_id": "B1", "label": "국산 신약 브랜드 가치", "affected_row_count": 12},
            {"topic_id": "B2", "label": "국산 신약 가치", "affected_row_count": 8},
            {"topic_id": "B3", "label": "제형 편의", "affected_row_count": 7},
        ],
    }

    aggregate = execution._aggregate_share_batches(
        [batch],
        brand="JAQBO",
        atc4="L04D0",
        scope_id="atc4:L04D0",
        axis_version="v1",
        topics=topics,
        token_budget=5000,
    )

    assert [row["label"] for row in aggregate["brand_specific_topics"]] == ["국산 신약 브랜드 가치", "제형 편의"]
    assert aggregate["brand_specific_dedup_count"] == 1
    assert aggregate["brand_specific_dedup_log"][0]["dropped_label"] == "국산 신약 가치"


def test_normalize_share_payload_keeps_independent_topic_totals() -> None:
    payload = {
        "status": "ok",
        "topic_shares": [
            {"topic_id": "T1", "label": "효능", "affected_row_count": 60},
            {"topic_id": "T2", "label": "편의", "affected_row_count": 25},
        ],
    }

    normalized = normalize_share_payload(
        payload,
        brand="THRUPAS",
        atc4="G04C2",
        scope_id="atc4:G04C2",
        axis_version="v1",
        row_count=100,
    )

    total_pct = round(sum(float(row["share_pct"]) for row in normalized["topic_shares"]), 1)

    assert total_pct == 85.0
    assert "etc_pct" not in normalized


def test_normalize_share_payload_allows_sum_above_100_for_overlapping_topics() -> None:
    payload = {
        "status": "ok",
        "topic_shares": [
            {"topic_id": "T1", "label": "효능", "affected_row_count": 70},
            {"topic_id": "T2", "label": "안전성", "affected_row_count": 50},
            {"topic_id": "T3", "label": "편의", "affected_row_count": 20},
        ],
    }

    normalized = normalize_share_payload(
        payload,
        brand="THRUPAS",
        atc4="G04C2",
        scope_id="atc4:G04C2",
        axis_version="v1",
        row_count=100,
    )
    total_pct = round(sum(float(row["share_pct"]) for row in normalized["topic_shares"]), 1)

    assert total_pct == 140.0
    assert "etc_pct" not in normalized
