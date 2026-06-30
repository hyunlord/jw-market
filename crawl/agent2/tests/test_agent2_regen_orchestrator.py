from __future__ import annotations

import json

from agent2_regen_orchestrator import (
    Agent2RegenOrchestrator,
    DependencyPorts,
    JsonRunStore,
    LLMCallResult,
    ValidationOutcome,
    check_upstream_freshness,
    compute_idempotency_key,
    validate_formatter_contract,
)


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


def test_idempotency_key_includes_brand_hash_revision_and_formatter():
    key = compute_idempotency_key("리바로젯", "sha256:abc", 3727, "wf217-order2-v10.3")

    assert key == "리바로젯|sha256:abc|rev:3727|formatter:wf217-order2-v10.3"


def test_idempotency_key_includes_variant_when_not_legacy():
    key = compute_idempotency_key("리바로젯", "sha256:abc", 3727, "wf217-order2-v10.3", "short")

    assert key == "리바로젯|sha256:abc|rev:3727|formatter:wf217-order2-v10.3|variant:short"


def test_formatter_contract_rejects_damaged_dates_and_double_formatting():
    parsed = _parsed("HHI 지수는 2,026.00-04 기준 570.86(ML·UBIST·매출·2026-04)입니다. 문장입니다. 문장입니다. 문장입니다. 문장입니다. 문장입니다.")

    result = validate_formatter_contract(parsed, brand="리바로젯")

    assert not result.valid
    assert any(error["type"] == "damaged_date_or_double_format" for error in result.errors)


def test_formatter_contract_rejects_obvious_defects():
    parsed = _parsed(
        "리바로젯은 반드시 성장합니다. news id 0f282b5c1cfebdf8입니다. "
        "점유율은 5.321%입니다. ★ 표시가 있습니다. 문장입니다. 문장입니다."
    )

    result = validate_formatter_contract(parsed, brand="리바로젯")

    error_types = {error["type"] for error in result.errors}
    assert {"prediction_certainty_phrase", "news_id_hex_present", "three_plus_decimal", "forbidden_marker"} <= error_types


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
    assert second["brands"]["리바로젯"]["status"] == "skipped"
    assert calls == {"bundle": 2, "llm": 1, "compose": 1}
    assert second["swap_plan"]["mode"] == "dry-run"


def test_orchestrator_repairs_zero_decimal_units_before_formatter_and_validator(tmp_path):
    seen = {"validate_body": "", "compose_body": ""}

    def build_bundle(brand: str):
        return {
            "bundle_meta": {"bundle_hash": "sha256:repair", "brand": brand},
            "brand_context": {"brand_name": brand},
            "market_views": [],
        }

    def call_llm(bundle):
        return LLMCallResult(
            success=True,
            parsed_output=_parsed("매출은 1,234.0원(ML·UBIST·매출·2026-04)입니다. 문장입니다. 문장입니다. 문장입니다. 문장입니다. 문장입니다."),
            raw_response=json.dumps({"ok": True}),
            tokens_in=1,
            tokens_out=2,
            duration_sec=0.1,
            model_version="genos_workflow_217",
            retry_count=0,
            error=None,
        )

    def validate(parsed_output, bundle):
        seen["validate_body"] = parsed_output["phenomenon"]["body"]
        return ValidationOutcome(valid=True, summary={"verdict": "PASS"}, details={})

    def compose(brand, bundle, llm_result, validation):
        seen["compose_body"] = llm_result.parsed_output["phenomenon"]["body"]
        return {"run_id": 1, "status": "ok", "cache_updated": False}

    orchestrator = Agent2RegenOrchestrator(
        workflow_revision_id=3727,
        formatter_version="wf217-order2-v10.3",
        run_store=JsonRunStore(tmp_path / "manifest.json"),
        ports=DependencyPorts(build_bundle=build_bundle, call_llm=call_llm, validate=validate, compose=compose),
        dry_run=True,
    )

    result = orchestrator.run(["리바로젯"])

    assert result["brands"]["리바로젯"]["status"] == "validated"
    assert "1,234원(ML·UBIST·매출·2026-04)" in seen["validate_body"]
    assert "1,234원(ML·UBIST·매출·2026-04)" in seen["compose_body"]
    assert result["brands"]["리바로젯"]["repair_summary"]["changes"][0]["type"] == "zero_decimal_unit"


def test_orchestrator_writes_failure_artifacts_for_validation_failures(tmp_path):
    def build_bundle(brand: str):
        return {
            "bundle_meta": {"bundle_hash": "sha256:failure", "brand": brand},
            "brand_context": {"brand_name": brand},
            "market_views": [],
        }

    def call_llm(bundle):
        return LLMCallResult(
            success=True,
            parsed_output=_parsed("근거 없는 수치 99.9%(ML·UBIST·매출·2026-04)입니다. 문장입니다. 문장입니다. 문장입니다. 문장입니다. 문장입니다."),
            raw_response=json.dumps({"raw": "response"}),
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
                "layers": {
                    "layer1_metric_validator": {
                        "unmatched_numbers": [
                            {
                                "stage": "phenomenon",
                                "context": "body",
                                "value": 99.9,
                                "raw_text": "99.9%",
                                "number_type": "percent",
                                "matched_path": None,
                                "context_text": "근거 없는 수치 99.9%",
                            }
                        ]
                    }
                }
            },
        )

    def compose(brand, bundle, llm_result, validation):
        raise AssertionError("compose must not run for validation failures")

    orchestrator = Agent2RegenOrchestrator(
        workflow_revision_id=3727,
        formatter_version="wf217-order2-v10.3",
        run_store=JsonRunStore(tmp_path / "manifest.json"),
        ports=DependencyPorts(build_bundle=build_bundle, call_llm=call_llm, validate=validate, compose=compose),
        dry_run=True,
    )

    result = orchestrator.run(["리바로하이"])
    record = result["brands"]["리바로하이"]

    assert record["status"] == "failed"
    assert record["detail"]["offending"][0]["raw_text"] == "99.9%"
    artifacts = record["failure_artifacts"]
    for key in ("raw_response", "parsed_before_repair", "parsed_after_repair", "validation", "failure_summary"):
        assert artifacts[key]
        assert (tmp_path / "failure_diagnostics" / "리바로하이" / f"{key}.json").exists()


def test_upstream_freshness_requires_source_cache_but_allows_missing_ai_and_mart_tables():
    class Cursor:
        def __init__(self):
            self.table = ""

        def execute(self, sql):
            self.table = sql.split("FROM ", 1)[1].strip()
            if self.table == "cache_deep_analysis_ai_analysis" or self.table.startswith("mart_"):
                raise Exception(f"Table jw_mart.{self.table} doesn't exist")

        def fetchone(self):
            return {"c": 100}

    class Conn:
        def cursor(self):
            return Cursor()

    result = check_upstream_freshness(Conn())

    assert result["valid"] is True
    assert result["tables"]["cache_cause"]["row_count"] == 100
    assert result["tables"]["cache_deep_analysis"]["row_count"] == 100
    assert result["tables"]["cache_deep_analysis_ai_analysis"]["required"] is False
    assert result["tables"]["mart_strategic_ml_brand_metric"]["required"] is False
