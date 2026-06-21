from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from pipeline.scripts.analysis.brand_activity.recheck.inventory import FileRecord


JsonObject = dict[str, Any]


def markdown_table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    """Render a compact GitHub Markdown table for deterministic reports."""
    if not rows:
        return "_없음_\n"
    header = "| " + " | ".join(headers) + " |"
    divider = "| " + " | ".join("---" for _ in headers) + " |"
    body = ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows]
    return "\n".join([header, divider, *body]) + "\n"


def file_rows(records: Sequence[FileRecord]) -> list[list[object]]:
    """Convert file records into non-sensitive inventory table rows."""
    return [
        [
            record.kind,
            record.month_ym or "",
            record.file_name,
            record.bytes,
            record.sha256,
            len(record.sheet_names),
            str(record.path),
        ]
        for record in records
    ]


def diff_rows(records: Sequence[FileRecord]) -> list[list[object]]:
    """Convert diff records into concise file/month/hash rows."""
    return [
        [record.kind, record.month_ym or "", record.file_name, record.sha256]
        for record in records
    ]


def write_report(path: Path, title: str, sections: Sequence[tuple[str, str]]) -> None:
    """Write one Markdown report with stable section ordering."""
    path.parent.mkdir(parents=True, exist_ok=True)
    parts = [f"# {title}\n"]
    for heading, body in sections:
        parts.append(f"\n## {heading}\n\n{body.rstrip()}\n")
    path.write_text("\n".join(parts), encoding="utf-8")


def bullet_list(items: Sequence[object]) -> str:
    """Render bullets or an explicit empty marker."""
    if not items:
        return "_없음_\n"
    return "\n".join(f"- {item}" for item in items) + "\n"


def json_code(payload: object) -> str:
    """Render small JSON-like evidence blocks without raw source messages."""
    import json

    return "```json\n" + json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n```\n"


def period_range(distribution: dict[str, int]) -> str:
    """Summarize a month distribution as count and min-max range."""
    if not distribution:
        return "0 rows"
    periods = sorted(distribution)
    return f"{sum(distribution.values()):,} rows / {periods[0]}~{periods[-1]}"


def render_reports(docs_dir: Path, payload: JsonObject) -> None:
    """Render all RECHECK Markdown reports from measured audit payloads."""
    diff = payload["manifest_diff"]
    write_report(
        docs_dir / "RECHECK_00_FILE_INVENTORY.md",
        "RECHECK_00_FILE_INVENTORY",
        [
            ("스캔 루트", bullet_list(payload["scan_roots"]) + "\n누락 루트:\n" + bullet_list(payload["missing_roots"])),
            ("월 커버리지", json_code(payload["month_coverage"])),
            ("신규/변경/삭제 요약", json_code(payload["manifest_diff_counts"])),
            ("신규 파일", markdown_table(["kind", "month", "file", "sha256"], diff_rows(diff["new"]))),
            ("변경 파일", markdown_table(["kind", "month", "file", "sha256"], diff_rows(diff["changed"]))),
            ("삭제 파일", markdown_table(["kind", "month", "file", "sha256"], diff_rows(diff["deleted"]))),
            ("전체 현재 파일", markdown_table(["kind", "month", "file", "bytes", "sha256", "sheets", "path"], file_rows(payload["current_records"]))),
        ],
    )
    _render_csd_report(docs_dir, payload)
    _render_km_report(docs_dir, payload)
    write_report(
        docs_dir / "RECHECK_03_DELTA_AND_RISKS.md",
        "RECHECK_03_DELTA_AND_RISKS",
        [
            ("기존 적재 대비 행수 diff", markdown_table(["table", "previous_rows", "current_rows", "delta", "current_period"], payload["table_delta_rows"])),
            ("깨진 가정 종합", bullet_list(payload["broken_assumptions"])),
            ("PL 결정 필요 항목", bullet_list(payload["pl_decision_items"])),
            ("기준/DB 스냅샷", json_code({"baseline_from_prior_artifacts": payload["baseline_from_prior_artifacts"], "live_before_final_reload": payload["db_before"], "after": payload["db_after"]})),
        ],
    )


def _render_csd_report(docs_dir: Path, payload: JsonObject) -> None:
    """Render the CSD-specific regression report."""
    csd = payload["csd_validation"]
    write_report(
        docs_dir / "RECHECK_01_CSD.md",
        "RECHECK_01_CSD",
        [
            ("구조 회귀 검증", json_code(payload["csd_header_summary"])),
            ("격리 재적재 결과", json_code({"stage_rows": csd["stage_rows_after_dedup"], "db_load": csd["db_load"]})),
            ("Region TOTAL 필터", json_code(csd["region_filter"])),
            ("Dedup/Conflict", json_code(csd["dedup_report"])),
            ("JW Channel/Measure", json_code(csd["enum_and_measure_checks"])),
            ("시장 요약", markdown_table(["market", "rows", "period_min", "period_max", "product_details"], [[row["market"], row["rows"], row["period_min"], row["period_max"], row["product_details"]] for row in csd["market_summary"]])),
            ("깨진 가정", bullet_list(payload["broken_csd_assumptions"])),
        ],
    )


def _render_km_report(docs_dir: Path, payload: JsonObject) -> None:
    """Render the Keyword/Meeting-specific regression report."""
    km = payload["km_validation"]
    write_report(
        docs_dir / "RECHECK_02_KEYWORD_MEETING.md",
        "RECHECK_02_KEYWORD_MEETING",
        [
            ("격리 재적재 결과", json_code({"core_rows": km["core_rows"], "db_load": km["db_load"]["tables"]})),
            ("기간 분포", json_code({"keyword": period_range(km["core_period_distribution"]["keyword"]), "meeting": period_range(km["core_period_distribution"]["meeting"])})),
            ("1파일=1월 검증", json_code(payload["km_one_month_violations"])),
            ("완전중복 보존 근거", json_code(km["duplicate_hash_summary"])),
            ("Message Count 중복월 정합", json_code(km["message_count_overlap"])),
            ("신규 enum 값", json_code(payload["new_enum_values"])),
            ("토픽 후속 입력 Top ATC4", json_code(payload["top_class_counts"])),
            ("LOWOSMOPERI 변형", bullet_list(payload["pl_product_variants"])),
            ("패키지 privacy/scope sanitization", json_code(payload["shareable_sanitization"])),
            ("깨진 가정", bullet_list(payload["broken_km_assumptions"])),
        ],
    )
