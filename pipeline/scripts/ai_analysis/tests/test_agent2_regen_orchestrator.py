from __future__ import annotations

import json

from agent2_density_worklist import RoutedAgent2Brand
from agent2_regen_orchestrator import (
    Agent2RegenOrchestrator,
    DependencyPorts,
    JsonRunStore,
    LLMCallResult,
    ValidationOutcome,
    check_upstream_freshness,
    compute_idempotency_key,
    _load_brand_list,
    _load_mart_brand_universe,
    parse_args,
    validate_formatter_contract,
)
from agent2_processing_modes import trim_bundle_for_mode
from bundle_builder.agent2_density_router import ProcessingMode, RouteDecision


def _valid_stage(body: str = "문장입니다. 문장입니다. 문장입니다. 문장입니다. 문장입니다. 문장입니다."):
    return {
        "title": "제목",
        "body": body,
        "bullets": ["a", "b", "c", "d"],
        "evidence": [],
    }


def _parsed(body: str | None = None):
    stage = _valid_stage(body or "리바로젯 매출은 1,234원(ML·UBIST·매출·2026-04)입니다. 문장입니다. 문장입니다. 문장입니다. 문장입니다. 문장입니다.")
    return {
        "phenomenon": stage,
        "cause": stage,
        "prediction": stage,
        "recommendation": stage,
        "evidence_pool": [],
    }


def _stage_with_counts(bullet_count: int, sentence_count: int) -> dict:
    sentences = ["근거입니다(ML·UBIST·매출·2026-04)."]
    sentences.extend("문장입니다." for _ in range(max(sentence_count - 1, 0)))
    body = " ".join(sentences)
    return {"title": "제목", "body": body, "bullets": [str(idx) for idx in range(bullet_count)], "evidence": []}


def _parsed_with_counts(bullet_count: int, sentence_count: int) -> dict:
    stage = _stage_with_counts(bullet_count, sentence_count)
    return {"phenomenon": stage, "cause": stage, "prediction": stage, "recommendation": stage, "evidence_pool": []}


def test_idempotency_key_includes_brand_hash_revision_and_formatter():
    key = compute_idempotency_key("리바로젯", "sha256:abc", 3727, "wf217-order2-v10.3")

    assert key == "리바로젯|sha256:abc|rev:3727|formatter:wf217-order2-v10.3"


def test_formatter_contract_rejects_damaged_dates_and_double_formatting():
    parsed = _parsed("HHI 지수는 2,026.00-04 기준 570.86(ML·UBIST·매출·2026-04)입니다. 문장입니다. 문장입니다. 문장입니다. 문장입니다. 문장입니다.")

    result = validate_formatter_contract(parsed, brand="리바로젯")

    assert not result.valid
    assert any(error["type"] == "damaged_date_or_double_format" for error in result.errors)


def test_formatter_contract_warns_on_decimal_krw_or_quantity_without_blocking():
    parsed = _parsed(
        "가드렛 매출은 176,193,950.22원(Market Landscape · UBIST 기준)입니다. "
        "처방량은 477,490.38Rx(Market Landscape · UBIST 기준)입니다. "
        "문장입니다. 문장입니다. 문장입니다. 문장입니다."
    )

    result = validate_formatter_contract(parsed, brand="가드렛", mode="full")

    assert result.valid
    assert not any(error["type"] == "krw_or_qty_decimal" for error in result.errors)
    assert any(warning["type"] == "krw_or_qty_decimal" for warning in result.warnings)


def test_formatter_contract_keeps_full_strict_but_allows_compact_and_recap_thresholds():
    full = validate_formatter_contract(_parsed_with_counts(bullet_count=3, sentence_count=3), brand="테스트", mode="full")
    compact = validate_formatter_contract(_parsed_with_counts(bullet_count=2, sentence_count=3), brand="테스트", mode="compact")
    recap = validate_formatter_contract(_parsed_with_counts(bullet_count=1, sentence_count=1), brand="테스트", mode="recap")

    assert any(error["type"] == "too_few_bullets" for error in full.errors)
    assert any(error["type"] == "body_too_short" for error in full.errors)
    assert compact.valid
    assert recap.valid


def test_formatter_contract_counts_full_source_labels_for_dual_source_gate():
    parsed = _parsed_with_counts(bullet_count=4, sentence_count=6)
    parsed["phenomenon"]["body"] += " 가드렛은 0.14%(Market Landscape · UBIST 기준)입니다."
    parsed["prediction"]["body"] += " 가드렛은 0.15%(Competitive Dynamics · IQVIA 기준)입니다."

    result = validate_formatter_contract(parsed, brand="가드렛", mode="full")

    assert result.valid
    assert result.summary["source_tag_count"] >= 2
    assert result.summary["source_mentions"] == ["IQVIA", "UBIST"]


def test_formatter_contract_warns_instead_of_blocking_when_full_source_tags_missing():
    parsed = _parsed_with_counts(bullet_count=4, sentence_count=6)
    for stage in ("phenomenon", "cause", "prediction", "recommendation"):
        parsed[stage]["body"] = "충분한 본문입니다. 문장입니다. 문장입니다. 문장입니다. 문장입니다. 문장입니다."

    result = validate_formatter_contract(parsed, brand="가드렛", mode="full")

    assert result.valid
    assert not result.errors
    assert result.summary["source_tag_blocking"] is False
    assert any(warning["type"] == "inline_source_tag_missing" for warning in result.warnings)


def test_formatter_contract_does_not_hardcode_dual_source_brand_errors():
    parsed = _parsed_with_counts(bullet_count=4, sentence_count=6)
    parsed["phenomenon"]["body"] += " 가드렛은 0.14%(Market Landscape · UBIST 기준)입니다."

    result = validate_formatter_contract(parsed, brand="가드렛", mode="full")

    assert result.valid
    assert not any(error["type"] == "dual_source_missing" for error in result.errors)
    assert not any(warning["type"] == "dual_source_missing" for warning in result.warnings)


def test_formatter_contract_accepts_full_source_label_for_compact_inline_gate():
    parsed = _parsed_with_counts(bullet_count=2, sentence_count=3)
    for stage in ("phenomenon", "cause", "prediction", "recommendation"):
        parsed[stage]["body"] = parsed[stage]["body"].replace("(ML·UBIST·매출·2026-04)", "(Market Landscape · UBIST 기준)")

    result = validate_formatter_contract(parsed, brand="바스티난 엠알", mode="compact")

    assert result.valid
    assert result.summary["source_tag_count"] == 4
    assert result.summary["source_tag_required"] is False


def test_formatter_contract_does_not_require_source_tags_for_compact():
    parsed = _parsed_with_counts(bullet_count=2, sentence_count=3)
    for stage in ("phenomenon", "cause", "prediction", "recommendation"):
        parsed[stage]["body"] = "요약입니다. 문장입니다. 문장입니다."

    result = validate_formatter_contract(parsed, brand="바스티난 엠알", mode="compact")

    assert result.valid
    assert result.summary["source_tag_required"] is False


def test_formatter_contract_exempts_recap_from_inline_source_gate():
    parsed = _parsed_with_counts(bullet_count=1, sentence_count=1)
    for stage in ("phenomenon", "cause", "prediction", "recommendation"):
        parsed[stage]["body"] = "요약입니다."

    result = validate_formatter_contract(parsed, brand="메디로텐", mode="recap")

    assert result.valid
    assert result.summary["source_tag_required"] is False


def test_trim_bundle_for_mode_rehashes_trimmed_compact_bundle():
    bundle = {
        "bundle_meta": {"brand": "테스트", "bundle_hash": "sha256:full"},
        "event_bundle": {
            "events_brand_centric": [{"id": idx} for idx in range(5)],
            "events_market_trend": [{"id": idx} for idx in range(4)],
            "cross_match_events": [{"id": idx} for idx in range(3)],
        },
        "competitor_events": {
            "by_view": {
                "v1": {
                    "competitors": [
                        {"brand_name": "A", "events": [{"id": 1}, {"id": 2}]},
                        {"brand_name": "B", "events": [{"id": 3}]},
                    ]
                }
            }
        },
        "forecast_simulation": {"available": True, "by_view": {"v1": {"horizons": [1, 2]}, "v2": {"horizons": [3]}}},
    }

    trimmed = trim_bundle_for_mode(bundle, "compact")

    assert len(trimmed["event_bundle"]["events_brand_centric"]) == 3
    assert len(trimmed["event_bundle"]["events_market_trend"]) == 2
    assert len(trimmed["event_bundle"]["cross_match_events"]) == 1
    assert len(trimmed["competitor_events"]["by_view"]["v1"]["competitors"]) == 1
    assert len(trimmed["competitor_events"]["by_view"]["v1"]["competitors"][0]["events"]) == 1
    assert list(trimmed["forecast_simulation"]["by_view"]) == ["v1"]
    assert trimmed["bundle_meta"]["bundle_hash"] != "sha256:full"


def test_trim_bundle_for_mode_leaves_full_bundle_unchanged():
    bundle = {
        "bundle_meta": {"brand": "테스트", "bundle_hash": "sha256:full"},
        "event_bundle": {"events_brand_centric": [{"id": 1}]},
    }

    assert trim_bundle_for_mode(bundle, "full") is bundle


def test_orchestrator_dry_run_second_run_skips_successful_idempotency_key(tmp_path):
    calls = {"bundle": 0, "llm": 0, "compose": 0}

    def build_bundle(brand: str):
        calls["bundle"] += 1
        return {
            "bundle_meta": {"bundle_hash": "sha256:testhash", "brand": brand},
            "brand_context": {"brand_name": brand},
            "market_views": [],
        }

    def call_llm(bundle):
        calls["llm"] += 1
        return LLMCallResult(
            success=True,
            parsed_output=_parsed(),
            raw_response=json.dumps({"ok": True}),
            tokens_in=1,
            tokens_out=2,
            duration_sec=0.1,
            model_version="genos_workflow_217",
            retry_count=0,
            error=None,
        )

    def validate(parsed_output, bundle):
        return ValidationOutcome(valid=True, summary={"verdict": "PASS"}, details={})

    def compose(brand, bundle, llm_result, validation):
        calls["compose"] += 1
        return {"run_id": 100 + calls["compose"], "status": "ok", "cache_updated": False}

    store = JsonRunStore(tmp_path / "manifest.json")
    ports = DependencyPorts(
        build_bundle=build_bundle,
        call_llm=call_llm,
        validate=validate,
        compose=compose,
    )
    orchestrator = Agent2RegenOrchestrator(
        workflow_revision_id=3727,
        formatter_version="wf217-order2-v10.3",
        run_store=store,
        ports=ports,
        dry_run=True,
    )

    first = orchestrator.run(["리바로젯"])
    second = orchestrator.run(["리바로젯"])

    assert first["brands"]["리바로젯"]["status"] == "validated"
    assert "processing_mode" not in first["brands"]["리바로젯"]
    assert second["brands"]["리바로젯"]["status"] == "skipped"
    assert "processing_mode" not in second["brands"]["리바로젯"]
    assert calls == {"bundle": 2, "llm": 1, "compose": 1}
    assert second["swap_plan"]["mode"] == "dry-run"


def test_upstream_freshness_requires_cache_tables_but_allows_missing_mart_tables():
    class Cursor:
        def __init__(self):
            self.table = ""

        def execute(self, sql):
            self.table = sql.split("FROM ", 1)[1].strip()
            if self.table.startswith("mart_"):
                raise Exception(f"Table jw_mart.{self.table} doesn't exist")

        def fetchone(self):
            return {"c": 25 if self.table == "cache_deep_analysis_ai_analysis" else 100}

    class Conn:
        def cursor(self):
            return Cursor()

    result = check_upstream_freshness(Conn())

    assert result["valid"] is True
    assert result["tables"]["cache_cause"]["row_count"] == 100
    assert result["tables"]["cache_deep_analysis"]["row_count"] == 100
    assert result["tables"]["cache_deep_analysis_ai_analysis"]["row_count"] == 25
    assert result["tables"]["mart_strategic_ml_brand_metric"]["required"] is False


def test_default_worklist_still_uses_ai_analysis_cache_sink():
    class Cursor:
        def execute(self, sql):
            self.sql = sql

        def fetchall(self):
            return [{"brand": "가드렛"}, {"brand": "리바로젯"}]

    class Conn:
        def __init__(self):
            self.cursor_obj = Cursor()

        def cursor(self):
            return self.cursor_obj

    conn = Conn()

    assert _load_brand_list(conn, fallback=["fallback"]) == ["가드렛", "리바로젯"]
    assert "cache_deep_analysis_ai_analysis" in conn.cursor_obj.sql


def test_mart_universe_worklist_is_explicit_and_reads_ml_mart():
    class Cursor:
        def execute(self, sql):
            self.sql = sql

        def fetchall(self):
            return [{"brand": "가드렛"}, {"brand": "확장브랜드"}]

    class Conn:
        def __init__(self):
            self.cursor_obj = Cursor()

        def cursor(self):
            return self.cursor_obj

    conn = Conn()

    assert _load_mart_brand_universe(conn) == ["가드렛", "확장브랜드"]
    assert "mart_strategic_ml_brand_metric" in conn.cursor_obj.sql
    assert "brand_name" in conn.cursor_obj.sql


def test_parse_args_accepts_general_density_source() -> None:
    args = parse_args(["--brand-source", "general-density", "--route-plan-only"])

    assert args.brand_source == "general-density"
    assert args.route_plan_only is True


def test_routed_run_uses_zero_template_without_llm(tmp_path) -> None:
    calls = {"bundle": 0, "llm": 0, "compose": 0}

    def build_bundle(brand: str):
        calls["bundle"] += 1
        return {"bundle_meta": {"bundle_hash": "sha256:test"}, "brand_context": {"brand_name": brand}}

    def call_llm(bundle):
        calls["llm"] += 1
        return LLMCallResult(False, {}, "", 0, 0, 0.0, "", 0, "should_not_call")

    def validate(parsed_output, bundle):
        return ValidationOutcome(valid=True, summary={"verdict": "PASS"}, details={})

    def compose(brand, bundle, llm_result, validation):
        calls["compose"] += 1
        return {}

    orchestrator = Agent2RegenOrchestrator(
        workflow_revision_id=3727,
        formatter_version="wf217-order2-v10.3",
        run_store=JsonRunStore(tmp_path / "manifest.json"),
        ports=DependencyPorts(build_bundle, call_llm, validate, compose),
        dry_run=True,
    )
    worklist = [
        RoutedAgent2Brand(
            brand_key="zero-key",
            canonical_brand_name="제로브랜드",
            route=RouteDecision("zero-key", 0, "zero", ProcessingMode.TEMPLATE_ZERO, ()),
        )
    ]

    manifest = orchestrator.run_routed(worklist)

    assert manifest["brands"]["zero-key"]["status"] == "template_zero"
    assert manifest["brands"]["zero-key"]["canonical_brand_name"] == "제로브랜드"
    assert manifest["brands"]["zero-key"]["template"]["phenomenon"]["evidence_none"] is True
    assert calls == {"bundle": 0, "llm": 0, "compose": 0}


def test_routed_run_uses_canonical_name_for_nonzero_work(tmp_path) -> None:
    calls = {"brand": "", "mode": "", "brand_centric": 0}

    def build_bundle(brand: str):
        calls["brand"] = brand
        return {
            "bundle_meta": {"bundle_hash": "sha256:testhash", "brand": brand},
            "brand_context": {"brand_name": brand},
            "market_views": [],
            "event_bundle": {
                "events_brand_centric": [{"id": idx} for idx in range(5)],
                "events_market_trend": [],
                "cross_match_events": [],
            },
        }

    def call_llm(bundle):
        calls["mode"] = bundle["bundle_meta"]["processing_mode"]
        calls["brand_centric"] = len(bundle["event_bundle"]["events_brand_centric"])
        return LLMCallResult(
            success=True,
            parsed_output=_parsed_with_counts(bullet_count=2, sentence_count=3),
            raw_response=json.dumps({"ok": True}),
            tokens_in=1,
            tokens_out=2,
            duration_sec=0.1,
            model_version="genos_workflow_217",
            retry_count=0,
            error=None,
        )

    def validate(parsed_output, bundle):
        return ValidationOutcome(valid=True, summary={"verdict": "PASS"}, details={})

    def compose(brand, bundle, llm_result, validation):
        return {"status": "ok", "cache_updated": False}

    orchestrator = Agent2RegenOrchestrator(
        workflow_revision_id=3727,
        formatter_version="wf217-order2-v10.3",
        run_store=JsonRunStore(tmp_path / "manifest.json"),
        ports=DependencyPorts(build_bundle, call_llm, validate, compose),
        dry_run=True,
    )
    worklist = [
        RoutedAgent2Brand(
            brand_key="capital-key",
            canonical_brand_name="자본브랜드",
            route=RouteDecision("capital-key", 3, "mid", ProcessingMode.LLM_COMPACT, ("tier2_llm_v1",)),
        )
    ]

    manifest = orchestrator.run_routed(worklist)

    assert calls["brand"] == "자본브랜드"
    assert calls["mode"] == "compact"
    assert calls["brand_centric"] == 3
    assert manifest["brands"]["capital-key"]["status"] == "validated"
    assert manifest["brands"]["capital-key"]["density_route"]["mode"] == "llm_compact"
