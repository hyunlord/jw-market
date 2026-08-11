from __future__ import annotations

import json

import pytest

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
from bundle_builder.agent2_zero_template import KpiSnapshot


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


def test_idempotency_key_separates_analysis_variants():
    short = compute_idempotency_key("리바로젯", "sha256:abc", 3727, "wf217-order2-v10.3", analysis_variant="short")
    long = compute_idempotency_key("리바로젯", "sha256:abc", 3727, "wf217-order2-v10.3", analysis_variant="long")

    assert short.endswith("|variant:short")
    assert long.endswith("|variant:long")
    assert short != long


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


@pytest.mark.parametrize(
    "value",
    ["0.0023%", "0.0006%", "0.00057%", "0.0015%", "0.0015%p", "0.0007%"],
)
def test_formatter_contract_accepts_measured_tiny_percentages(value):
    parsed = _parsed_with_counts(bullet_count=4, sentence_count=4)
    parsed["phenomenon"]["body"] += f" 근거값은 {value}입니다."

    result = validate_formatter_contract(parsed, brand="극소값", mode="full")

    assert not [error for error in result.errors if error["type"] == "three_plus_decimal"]


@pytest.mark.parametrize("value", ["1.234%", "0.0123%", "0.0001234%", "0.00057배", "0.00057"])
def test_formatter_contract_keeps_non_tiny_or_overprecise_values_blocked(value):
    parsed = _parsed_with_counts(bullet_count=4, sentence_count=4)
    parsed["phenomenon"]["body"] += f" 근거값은 {value}입니다."

    result = validate_formatter_contract(parsed, brand="일반값", mode="full")

    assert {"type": "three_plus_decimal", "path": "phenomenon.body", "value": value} in result.errors


def test_formatter_contract_keeps_full_strict_but_allows_compact_and_recap_thresholds():
    full = validate_formatter_contract(_parsed_with_counts(bullet_count=3, sentence_count=3), brand="테스트", mode="full")
    compact = validate_formatter_contract(_parsed_with_counts(bullet_count=2, sentence_count=3), brand="테스트", mode="compact")
    recap = validate_formatter_contract(_parsed_with_counts(bullet_count=1, sentence_count=1), brand="테스트", mode="recap")

    assert any(error["type"] == "too_few_bullets" for error in full.errors)
    assert any(error["type"] == "body_too_short" for error in full.errors)
    assert compact.valid
    assert recap.valid


def test_formatter_contract_allows_two_sentence_compact_body_but_rejects_one_sentence():
    compact_two = validate_formatter_contract(_parsed_with_counts(bullet_count=2, sentence_count=2), brand="테스트", mode="compact")
    compact_one = validate_formatter_contract(_parsed_with_counts(bullet_count=2, sentence_count=1), brand="테스트", mode="compact")

    assert compact_two.valid
    assert any(error["type"] == "body_too_short" for error in compact_one.errors)


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


def test_run_store_seeds_successful_db_rows_without_repeating_llm(tmp_path) -> None:
    # Given one successful output retained in the run database after a Pod failure
    store = JsonRunStore(tmp_path / "manifest.json")
    seeded = store.seed_successes(
        [
            {
                "run_id": 84550,
                "brand": "가다실",
                "bundle_hash": "sha256:existing",
                "snapshot_at": "2026-08-09 17:16:02",
            }
        ],
        workflow_revision_id=3727,
        formatter_version="wf217-order2-v10.3",
        analysis_variant="short",
    )
    ports = DependencyPorts(
        build_bundle=lambda brand: {
            "bundle_meta": {"bundle_hash": "sha256:existing", "brand": brand},
            "brand_context": {"brand_name": brand},
            "market_views": [],
        },
        call_llm=lambda _bundle: pytest.fail("seeded success must not call the LLM"),
        validate=lambda _parsed, _bundle: pytest.fail("seeded success must not validate"),
        compose=lambda *_args: pytest.fail("seeded success must not compose"),
    )

    # When the failed snapshot is resumed with the same bundle identity
    manifest = Agent2RegenOrchestrator(
        workflow_revision_id=3727,
        formatter_version="wf217-order2-v10.3",
        run_store=store,
        ports=ports,
        dry_run=True,
    ).run(["가다실"], analysis_variant="short")

    # Then the retained row is reused without another model call
    assert seeded == 1
    assert manifest["brands"]["가다실"]["status"] == "skipped"
    assert manifest["brands"]["가다실"]["previous"]["run_id"] == 84550


def test_run_store_reuses_validated_bundle_when_only_snapshot_changes(tmp_path) -> None:
    # Given a previously validated bundle whose only stale field is snapshot_at
    store = JsonRunStore(tmp_path / "manifest.json")
    seeded = store.seed_successes(
        [
            {
                "run_id": 84550,
                "brand": "가다실",
                "bundle_hash": "sha256:legacy-includes-snapshot",
                "snapshot_at": "2026-08-08 17:16:02",
                "input_bundle": json.dumps(
                    {
                        "bundle_meta": {
                            "brand": "가다실",
                            "snapshot_at": "2026-08-08T17:16:02",
                            "bundle_hash": "sha256:legacy-includes-snapshot",
                        },
                        "brand_context": {"brand_name": "가다실"},
                        "market_views": [
                            {"snapshot_at": "2026-08-08T17:16:02", "value": 7}
                        ],
                    },
                    ensure_ascii=False,
                ),
            }
        ],
        workflow_revision_id=3727,
        formatter_version="wf217-order2-v10.3",
        analysis_variant="short",
    )
    ports = DependencyPorts(
        build_bundle=lambda brand: {
            "bundle_meta": {
                "brand": brand,
                "snapshot_at": "2026-08-09T17:16:02",
                "bundle_hash": None,
            },
            "brand_context": {"brand_name": brand},
            "market_views": [
                {"snapshot_at": "2026-08-09T17:16:02", "value": 7}
            ],
        },
        call_llm=lambda _bundle: pytest.fail("stable prior success must not call the LLM"),
        validate=lambda _parsed, _bundle: pytest.fail("stable prior success must not validate"),
        compose=lambda *_args: pytest.fail("stable prior success must not compose"),
    )

    # When the same material bundle is rebuilt at a later snapshot
    manifest = Agent2RegenOrchestrator(
        workflow_revision_id=3727,
        formatter_version="wf217-order2-v10.3",
        run_store=store,
        ports=ports,
        dry_run=True,
    ).run(["가다실"], analysis_variant="short")

    # Then the validated output is reused without recomputation
    assert seeded == 1
    assert manifest["brands"]["가다실"]["status"] == "skipped"
    assert manifest["brands"]["가다실"]["previous"]["run_id"] == 84550


def test_orchestrator_passes_analysis_variant_to_llm_and_compose(tmp_path):
    calls = {"llm_variant": "", "compose_variant": ""}

    def build_bundle(brand: str):
        return {
            "bundle_meta": {"bundle_hash": "sha256:testhash", "brand": brand},
            "brand_context": {"brand_name": brand},
            "market_views": [],
        }

    def call_llm(bundle, analysis_variant="legacy"):
        calls["llm_variant"] = analysis_variant
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

    def compose(brand, bundle, llm_result, validation, analysis_variant="legacy"):
        calls["compose_variant"] = analysis_variant
        return {"run_id": 101, "status": "ok", "cache_updated": False}

    orchestrator = Agent2RegenOrchestrator(
        workflow_revision_id=3727,
        formatter_version="wf217-order2-v10.3",
        run_store=JsonRunStore(tmp_path / "manifest.json"),
        ports=DependencyPorts(build_bundle, call_llm, validate, compose),
        dry_run=True,
    )

    record = orchestrator._run_brand("리바로젯", analysis_variant="short")

    assert record["status"] == "validated"
    assert record["analysis_variant"] == "short"
    assert calls == {"llm_variant": "short", "compose_variant": "short"}


def test_orchestrator_validation_failure_retains_raw_parsed_and_full_validation_detail(tmp_path):
    parsed = _parsed_with_counts(bullet_count=4, sentence_count=4)

    def build_bundle(brand: str):
        return {"bundle_meta": {"bundle_hash": "sha256:testhash", "brand": brand}, "brand_context": {"brand_name": brand}}

    def call_llm(bundle):
        return LLMCallResult(
            success=True,
            parsed_output=parsed,
            raw_response=json.dumps({"raw": True}),
            tokens_in=1,
            tokens_out=2,
            duration_sec=0.1,
            model_version="genos_workflow_217",
            retry_count=0,
            error=None,
        )

    def validate(parsed_output, bundle):
        return ValidationOutcome(
            valid=False,
            summary={"layer1_valid": False, "verdict": "FAIL"},
            details={
                "unmatched_numbers": [{"raw_text": "999,999", "pattern": "comma_raw_value"}],
                "layers": {"layer1_metric_validator": {"valid": False}},
                "warnings": [{"pattern": "source_label_missing"}],
            },
        )

    def compose(brand, bundle, llm_result, validation):
        raise AssertionError("compose must not run after validation failure")

    orchestrator = Agent2RegenOrchestrator(
        workflow_revision_id=3727,
        formatter_version="wf217-order2-v10.3",
        run_store=JsonRunStore(tmp_path / "manifest.json"),
        ports=DependencyPorts(build_bundle, call_llm, validate, compose),
        dry_run=True,
    )

    manifest = orchestrator.run(["테스트"])
    detail = manifest["brands"]["테스트"]["detail"]

    assert detail["validation"] == {"layer1_valid": False, "verdict": "FAIL"}
    assert detail["validation_detail"]["unmatched_numbers"][0]["raw_text"] == "999,999"
    assert detail["llm"]["raw_response"] == json.dumps({"raw": True})
    assert detail["llm"]["parsed_output"] == parsed


def test_orchestrator_llm_failure_retains_raw_response_when_parse_fails(tmp_path):
    def build_bundle(brand: str):
        return {"bundle_meta": {"bundle_hash": "sha256:testhash", "brand": brand}, "brand_context": {"brand_name": brand}}

    def call_llm(bundle):
        return LLMCallResult(
            success=False,
            parsed_output={},
            raw_response="not json",
            tokens_in=0,
            tokens_out=0,
            duration_sec=0.1,
            model_version="genos_workflow_217",
            retry_count=1,
            error="ValueError: GenOS response does not contain the required 4-stage JSON object",
        )

    def validate(parsed_output, bundle):
        raise AssertionError("validate must not run after LLM failure")

    def compose(brand, bundle, llm_result, validation):
        raise AssertionError("compose must not run after LLM failure")

    orchestrator = Agent2RegenOrchestrator(
        workflow_revision_id=3727,
        formatter_version="wf217-order2-v10.3",
        run_store=JsonRunStore(tmp_path / "manifest.json"),
        ports=DependencyPorts(build_bundle, call_llm, validate, compose),
        dry_run=True,
    )

    manifest = orchestrator.run(["테스트"])
    detail = manifest["brands"]["테스트"]["detail"]

    assert detail["error"] == "ValueError: GenOS response does not contain the required 4-stage JSON object"
    assert detail["llm"]["raw_response"] == "not json"
    assert detail["llm"]["parsed_output"] == {}
    assert detail["llm"]["retry_count"] == 1


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
    args = parse_args(
        ["--brand-source", "general-density", "--route-plan-only", "--brand-keys-file", "sample.json"]
    )

    assert args.brand_source == "general-density"
    assert args.route_plan_only is True
    assert args.brand_keys_file == "sample.json"


def test_routed_run_uses_zero_template_without_llm(tmp_path) -> None:
    calls = {"bundle": 0, "llm": 0, "compose": 0}
    zero_evidence_brands = (
        "10%글리세린5%과당 HK이노엔",
        "5-에이치티피",
        "5-에프유 중외",
        "5-엠씨",
        "5%포도당가0.45%염화나트륨 중외",
        "96%에탄올 스테롭",
    )

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
            brand_key=f"zero-key-{index}",
            canonical_brand_name=brand_name,
            route=RouteDecision(
                f"zero-key-{index}",
                0,
                "zero",
                ProcessingMode.TEMPLATE_ZERO,
                (),
            ),
        )
        for index, brand_name in enumerate(zero_evidence_brands)
    ]

    manifest = orchestrator.run_routed(worklist)

    assert [
        manifest["brands"][f"zero-key-{index}"]["canonical_brand_name"]
        for index in range(len(zero_evidence_brands))
    ] == list(zero_evidence_brands)
    assert all(
        manifest["brands"][f"zero-key-{index}"]["status"] == "template_zero"
        for index in range(len(zero_evidence_brands))
    )
    assert all(
        manifest["brands"][f"zero-key-{index}"]["template"]["phenomenon"]["evidence_none"] is True
        for index in range(len(zero_evidence_brands))
    )
    assert calls == {"bundle": 0, "llm": 0, "compose": 0}


def test_routed_zero_template_uses_kpi_snapshot_without_bundle_or_llm(tmp_path) -> None:
    calls = {"bundle": 0, "llm": 0, "compose": 0, "kpi": []}

    def build_bundle(brand):
        calls["bundle"] += 1
        return {"bundle_meta": {"bundle_hash": "should-not-run"}, "brand_context": {"brand_name": brand}}

    def call_llm(bundle):
        calls["llm"] += 1
        return LLMCallResult(False, {}, "", 0, 0, 0.0, "", 0, "should_not_call")

    def validate(parsed_output, bundle):
        return ValidationOutcome(valid=True, summary={"verdict": "PASS"}, details={})

    def compose(brand, bundle, llm_result, validation):
        calls["compose"] += 1
        return {}

    class FakeKpiProvider:
        def get_snapshot(self, brand_key: str, brand_name: str):
            calls["kpi"].append((brand_key, brand_name))
            return KpiSnapshot(
                brand=brand_name,
                market_name="테스트 시장",
                rank=2,
                share_pct=18.4,
                cagr_pct=0.3,
                market_size_recent=123456.0,
            )

    orchestrator = Agent2RegenOrchestrator(
        workflow_revision_id=3727,
        formatter_version="wf217-order2-v10.3",
        run_store=JsonRunStore(tmp_path / "manifest.json"),
        ports=DependencyPorts(build_bundle, call_llm, validate, compose),
        zero_kpi_provider=FakeKpiProvider(),
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
    template = manifest["brands"]["zero-key"]["template"]["phenomenon"]

    assert template["template_type"] == "leader"
    assert "2위" in template["body"]
    assert calls == {"bundle": 0, "llm": 0, "compose": 0, "kpi": [("zero-key", "제로브랜드")]}


def test_routed_run_uses_canonical_name_for_nonzero_work(tmp_path) -> None:
    calls = {"brand": "", "brand_key": "", "mode": "", "brand_centric": 0}

    def build_bundle(brand: str, brand_key: str):
        calls["brand"] = brand
        calls["brand_key"] = brand_key
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
    assert calls["brand_key"] == "capital-key"
    assert calls["mode"] == "compact"
    assert calls["brand_centric"] == 3
    assert manifest["brands"]["capital-key"]["status"] == "validated"
    assert manifest["brands"]["capital-key"]["density_route"]["mode"] == "llm_compact"


def test_weekly_quality_failures_do_not_stop_later_tiers(tmp_path) -> None:
    orchestrator = Agent2RegenOrchestrator(
        workflow_revision_id=3727,
        formatter_version="wf217-order2-v10.3",
        run_store=JsonRunStore(tmp_path / "manifest.json"),
        ports=DependencyPorts(lambda _brand: {}, lambda _bundle: None, lambda _a, _b: None, lambda *_args: {}),
        dry_run=True,
        fail_threshold=5,
        continue_on_quality_failure=True,
    )
    calls: list[str] = []

    def forced_result(brand: str, *_args, **_kwargs):
        calls.append(brand)
        return (
            {"brand": brand, "status": "failed", "reason": "forced_failure"}
            if brand != "tier2-tail"
            else {"brand": brand, "status": "validated"}
        )

    orchestrator._run_brand = forced_result  # type: ignore[method-assign]
    worklist = [
        RoutedAgent2Brand(
            brand_key=f"key-{index}",
            canonical_brand_name=("tier2-tail" if index == 7 else f"failed-{index}"),
            route=RouteDecision(f"key-{index}", 10, "full", ProcessingMode.LLM_FULL, ()),
            tier=0 if index == 0 else 2,
            cohort="jw" if index == 0 else "nonstrategic",
        )
        for index in range(8)
    ]

    manifest = orchestrator.run_routed(worklist, analysis_variant="short")

    assert len(calls) == 8
    assert calls[-1] == "tier2-tail"
    assert manifest["failure_count"] == 7
    assert "abort_reason" not in manifest
    assert manifest["brands"]["key-7"]["status"] == "validated"
