#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "httpx2[http2,brotli,zstd]",
#     "pymysql",
#     "rich",
#     "typer",
# ]
# ///

# --- How to run ---
# 1. Install uv (if not installed):
#      curl -LsSf https://astral.sh/uv/install.sh | sh
# 2. Run dry-run artifacts:
#      uv run pipeline/scripts/analysis/brand_activity/model_cmp/run_model_cmp.py
# 3. Run bounded GenOS PoC:
#      uv run pipeline/scripts/analysis/brand_activity/model_cmp/run_model_cmp.py --execute
# ------------------

from __future__ import annotations

from pathlib import Path
import os
import sys
import time

import typer
from rich.console import Console


REPO_ROOT = Path(__file__).resolve().parents[5]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pipeline.scripts.analysis.brand_activity.model_cmp.audit import (  # noqa: E402
    AUDIT_DIR,
    DOCS_DIR,
    create_zip_package,
    generated_files,
    raw_text_scan,
    write_git_status,
    write_json,
    write_manifest,
    write_text,
)
from pipeline.scripts.analysis.brand_activity.model_cmp.data_source import (  # noqa: E402
    DICTIONARY_PATH,
    connect_mariadb,
    fetch_keyword_rows,
    fetch_snapshot,
    read_env_file,
)
from pipeline.scripts.analysis.brand_activity.model_cmp.dictionary import (  # noqa: E402
    dictionary_baseline,
    load_redesign_dictionary,
    seed_for_atc4_values,
)
from pipeline.scripts.analysis.brand_activity.model_cmp.execution import (  # noqa: E402
    REPRESENTATIVE_SAMPLE_KEY,
    build_call_plan,
    execute_model_calls,
    execution_summary,
    skipped_execution,
)
from pipeline.scripts.analysis.brand_activity.model_cmp.market_groups import build_market_group_model, model_to_json  # noqa: E402
from pipeline.scripts.analysis.brand_activity.model_cmp.models import JsonValue, KeywordRow, MarketGroupModel  # noqa: E402
from pipeline.scripts.analysis.brand_activity.model_cmp.privacy import redacted_rows_for_audit  # noqa: E402
from pipeline.scripts.analysis.brand_activity.model_cmp.prompts import PROMPT_VERSION, prompt_template_manifest  # noqa: E402
from pipeline.scripts.analysis.brand_activity.model_cmp.reports import (  # noqa: E402
    render_design_md,
    render_group_model_md,
    render_reco_md,
    render_results_md,
)
from pipeline.scripts.analysis.brand_activity.model_cmp.sampling import (  # noqa: E402
    SCOPE_SPECS,
    all_atc4_values,
    build_axis_samples,
    build_brand_samples,
)


CONSOLE = Console()


class SourceTextLeakError(RuntimeError):
    """Raised when generated artifacts contain exact sampled raw keyword text."""


def main(execute: bool = False, axis_per_brand: int = 12, brand_rows: int = 15, tag: str = "") -> None:
    """Generate market-group and 3-model comparison deliverables."""
    result = run_pipeline(execute=execute, axis_per_brand=axis_per_brand, brand_rows=brand_rows, tag=tag or _timestamp_tag())
    CONSOLE.print_json(data=result)


def run_pipeline(*, execute: bool, axis_per_brand: int, brand_rows: int, tag: str) -> dict[str, JsonValue]:
    """Run read-only data collection, optional GenOS calls, report writing, and packaging."""
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    model = build_market_group_model()
    dictionary = load_redesign_dictionary(DICTIONARY_PATH)
    db_snapshot_before, keyword_rows, db_snapshot_after = _load_rows_from_stage()
    axis_samples = build_axis_samples(keyword_rows, per_brand=axis_per_brand)
    brand_samples = build_brand_samples(keyword_rows, limit=brand_rows)
    call_plan = build_call_plan(axis_samples, brand_samples)
    token = os.environ.get("GENOS_BEARER_TOKEN", "")
    auth_mode = "bearer" if token else "no_auth_dev_gateway"
    execution = execute_model_calls(token=token, dictionary=dictionary, axis_samples=axis_samples, brand_samples=brand_samples) if execute else skipped_execution()
    report_payload = _report_payload(
        execute=execute,
        auth_mode=auth_mode,
        model_summary=execution_summary(execution, keyword_rows, call_plan),
        dictionary_baseline_payload=_dictionary_baselines(dictionary, brand_samples),
    )
    _write_reports(model, report_payload)
    _write_audit(
        db_snapshot_before=db_snapshot_before,
        db_snapshot_after=db_snapshot_after,
        axis_samples=axis_samples,
        brand_samples=brand_samples,
        call_plan=call_plan,
        report_payload=report_payload,
        auth_mode=auth_mode,
    )
    sampled_rows = _all_sampled_rows(axis_samples, brand_samples)
    paths_for_scan = generated_files()
    scan = raw_text_scan(paths_for_scan, sampled_rows)
    write_json(AUDIT_DIR / "raw_text_scan.json", scan)
    if scan.get("leak_count") != 0:
        raise SourceTextLeakError(f"sample source text leaked into generated artifacts: {scan.get('leak_count')}")
    write_git_status(AUDIT_DIR / "git_status.txt")
    manifest_inputs = [path for path in generated_files() if path.name != "manifest_sha256.csv"]
    write_manifest(manifest_inputs, AUDIT_DIR / "manifest_sha256.csv")
    zip_result = create_zip_package(generated_files(), tag=tag)
    _write_zip_sidecar(zip_result.tmp_zip, zip_result.sha256, zip_result.backup_zip)
    return {
        "tag": tag,
        "execute": execute,
        "planned_calls": len(call_plan),
        "auth_mode": auth_mode,
        "tmp_zip": str(zip_result.tmp_zip),
        "backup_zip": str(zip_result.backup_zip),
        "zip_sha256": zip_result.sha256,
        "raw_text_leak_count": scan.get("leak_count"),
        "open_questions": len(_open_questions()),
        "representative_repeat_sample": REPRESENTATIVE_SAMPLE_KEY,
    }


def _load_rows_from_stage() -> tuple[dict[str, JsonValue], list[KeywordRow], dict[str, JsonValue]]:
    """Load only required stage rows and table fingerprints from MariaDB."""
    connection = connect_mariadb(read_env_file())
    try:
        before = fetch_snapshot(connection)
        rows = fetch_keyword_rows(connection, all_atc4_values())
        after = fetch_snapshot(connection)
    finally:
        connection.close()
    return before, rows, after


def _report_payload(
    *,
    execute: bool,
    auth_mode: str,
    model_summary: dict[str, JsonValue],
    dictionary_baseline_payload: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    """Combine execution metrics and static recommendation text for reports."""
    execution_status = _execution_status(execute, model_summary)
    return {
        **model_summary,
        "execution_mode": "bounded_real_genos_calls" if execute else "dry_run_no_genos_calls",
        "execution_status": execution_status,
        "auth_mode": auth_mode,
        "prompt_version": PROMPT_VERSION,
        "dictionary_baseline": dictionary_baseline_payload,
        "tuning_vs_logic": _tuning_vs_logic(execution_status),
        "model_recommendation": "Flash를 운영 기본 후보로 두고, 시장 공통축 생성과 월간 QC에는 Pro를 병행한다. Lite는 비용 민감한 초안/재시도 후보로만 둔다.",
        "dictionary_comparison": "REDESIGN 사전은 재현성과 설명가능성이 강하지만 다중 ATC4 그룹 축과 맥락형 메시지 압축에는 LLM 2단 구조가 더 유연하다. 운영은 LLM 축/비율을 기본으로, 사전 hit를 sanity-check로 병행하는 혼합안이 가장 보수적이다.",
        "open_questions": _open_questions(),
    }


def _execution_status(execute: bool, payload: dict[str, JsonValue]) -> str:
    """Classify whether GenOS execution succeeded for reporting."""
    if not execute:
        return "dry_run"
    logs = payload.get("call_logs")
    if not isinstance(logs, list):
        return "executed_no_logs"
    error_count = sum(1 for item in logs if isinstance(item, dict) and item.get("status") != "ok")
    return "executed" if error_count == 0 else f"executed_with_{error_count}_non_ok_calls"


def _dictionary_baselines(dictionary: dict[str, JsonValue], brand_samples: dict[str, list[KeywordRow]]) -> dict[str, JsonValue]:
    """Create dictionary-baseline rows for the same brand samples used by LLM."""
    results: dict[str, JsonValue] = {}
    for scope in SCOPE_SPECS:
        for atc4, brand in scope.share_brands:
            sample_key = f"{scope.scope_id}:{atc4}:{brand}"
            results[sample_key] = dictionary_baseline(brand_samples[sample_key], seed_for_atc4_values(dictionary, (atc4,)))
    return results


def _write_reports(model: MarketGroupModel, payload: dict[str, JsonValue]) -> None:
    """Write the five human-readable and machine-readable deliverables."""
    write_json(DOCS_DIR / "GROUP_01_MARKET_MODEL.json", model_to_json(model))
    write_text(DOCS_DIR / "GROUP_01_MARKET_MODEL.md", render_group_model_md(model))
    write_text(DOCS_DIR / "MODEL_CMP_01_DESIGN.md", render_design_md(model, payload))
    write_text(DOCS_DIR / "MODEL_CMP_02_RESULTS.md", render_results_md(payload))
    write_text(DOCS_DIR / "MODEL_CMP_03_RECO.md", render_reco_md(payload))


def _write_audit(
    *,
    db_snapshot_before: dict[str, JsonValue],
    db_snapshot_after: dict[str, JsonValue],
    axis_samples: dict[str, list[KeywordRow]],
    brand_samples: dict[str, list[KeywordRow]],
    call_plan: list[dict[str, JsonValue]],
    report_payload: dict[str, JsonValue],
    auth_mode: str,
) -> None:
    """Write sanitized audit files with no raw keyword text."""
    write_json(AUDIT_DIR / "db_snapshot.json", {"before": db_snapshot_before, "after": db_snapshot_after})
    write_json(AUDIT_DIR / "prompt_templates.json", prompt_template_manifest())
    write_json(AUDIT_DIR / "call_plan.json", call_plan)
    write_json(AUDIT_DIR / "call_log_sanitized.json", report_payload.get("call_logs", []))
    write_json(AUDIT_DIR / "axis_results_sanitized.json", report_payload.get("axis_results", {}))
    write_json(AUDIT_DIR / "brand_results_sanitized.json", report_payload.get("brand_results", {}))
    write_json(AUDIT_DIR / "nondeterminism.json", report_payload.get("nondeterminism", {}))
    write_json(AUDIT_DIR / "model_quality_summary.json", {"axis_overlap": report_payload.get("axis_overlap", {}), "token_latency_by_model": report_payload.get("token_latency_by_model", {})})
    write_json(AUDIT_DIR / "dictionary_baseline.json", report_payload.get("dictionary_baseline", {}))
    write_json(AUDIT_DIR / "credential_check.json", {"auth_mode": auth_mode, "token_env": "GENOS_BEARER_TOKEN", "production_policy": "PL/GenOS 확인 필요"})
    write_json(AUDIT_DIR / "input_rows_redacted.json", {"axis_samples": _redacted_sample_map(axis_samples), "brand_samples": _redacted_sample_map(brand_samples)})
    write_json(AUDIT_DIR / "run_summary.json", {"zip_sha256": "computed after package creation; see completion report and .sha256 sidecar", **report_payload})


def _redacted_sample_map(samples: dict[str, list[KeywordRow]]) -> dict[str, JsonValue]:
    """Redact sampled prompt rows for audit storage."""
    return {key: redacted_rows_for_audit(rows) for key, rows in samples.items()}


def _all_sampled_rows(axis_samples: dict[str, list[KeywordRow]], brand_samples: dict[str, list[KeywordRow]]) -> list[KeywordRow]:
    """Flatten sampled rows while preserving duplicates for leakage scanning."""
    rows: list[KeywordRow] = []
    for sample_rows in axis_samples.values():
        rows.extend(sample_rows)
    for sample_rows in brand_samples.values():
        rows.extend(sample_rows)
    return rows


def _tuning_vs_logic(execution_status: str) -> str:
    """Return the operational interpretation of the bounded comparison."""
    if execution_status == "dry_run":
        return "dry-run에서는 판단 보류. 실호출 결과 기준으로 업데이트 필요."
    return "2단 공통축→브랜드비율 구조는 유지하고, 모델 등급/프롬프트 튜닝으로 품질을 조정하는 쪽이 우선이다."


def _open_questions() -> list[str]:
    """List unresolved PL/GenOS decisions carried into the recommendation."""
    return [
        "dev gateway no-auth 허용이 PoC 환경 사실인지, 운영 bearer 강제 정책인지 GenOS 확인 필요.",
        "리바로하이/피나스타/제이다트 absent_in_csd 멤버의 담당부서 매핑 시트 수신 후 갱신 필요.",
        "GenOS 모델별 단가표가 없어 비용은 토큰량/호출수 기준 상대 비교로 남김.",
    ]


def _write_zip_sidecar(tmp_zip: Path, sha256: str, backup_zip: Path) -> None:
    """Write SHA256 sidecars next to the /tmp zip and permanent backup."""
    text = f"{sha256}  {tmp_zip}\n"
    tmp_zip.with_suffix(tmp_zip.suffix + ".sha256").write_text(text, encoding="utf-8")
    backup_zip.with_suffix(backup_zip.suffix + ".sha256").write_text(text.replace(str(tmp_zip), str(backup_zip)), encoding="utf-8")


def _timestamp_tag() -> str:
    """Return a sortable local timestamp tag for artifacts."""
    return time.strftime("%Y%m%d_%H%M%S")


if __name__ == "__main__":
    typer.run(main)
