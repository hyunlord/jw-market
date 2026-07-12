from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final, Literal

import pymysql

from agent2_variant_contract import VariantLineage, validate_variant_payload
from agent2_variant_candidate_builder import _lineage, _load_outputs, _load_payload, _select_runs
from agent2_variant_promotion import VariantRecord
from bundle_builder.agent2_zero_template import KpiSnapshot, render_zero_template
from bundle_builder.zero_kpi_provider import BatchGeneralZeroKpiSnapshotProvider


FALLBACK_STATUS: Final = "complete_template_fallback"
FALLBACK_MODEL: Final = "deterministic-template-fallback"
Variant = Literal["short", "long"]
_TABLE_NAME = re.compile(r"^[a-zA-Z0-9_]+$")


def build_fallback_record(
    *,
    brand_key: str,
    variant: Variant,
    snapshot: KpiSnapshot,
    source_epoch: str,
    generated_at: datetime,
) -> VariantRecord:
    stages = render_zero_template(snapshot)
    payload: dict[str, Any] = {
        **stages,
        "analysis_variant": variant,
        "generated_at": generated_at.isoformat(),
        "model_version": FALLBACK_MODEL,
        "phase_zeta_stage": "deterministic_template_fallback",
        "run_id_phase_zeta": None,
        "reload_reason": "Approved deterministic KPI fallback for a persistently failed short/long variant.",
    }
    canonical_input = json.dumps(
        {"brand_key": brand_key, "variant": variant, "source_epoch": source_epoch, "stages": stages},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    input_hash = hashlib.sha256(canonical_input.encode()).hexdigest()
    lineage = VariantLineage(
        workflow_id=None,
        workflow_revision_id=None,
        generation_id=f"template-fallback-{brand_key}-{variant}-{input_hash[:12]}",
        input_hash=input_hash,
        generated_at=generated_at,
        source_epoch=source_epoch,
        generation_status=FALLBACK_STATUS,
        deterministic=True,
    )
    validate_variant_payload(payload, variant)
    return VariantRecord(payload=payload, lineage=lineage)


def repair_variant_sql(table: str, variant: Variant) -> str:
    if not _TABLE_NAME.fullmatch(table):
        raise ValueError(f"unsafe table name: {table!r}")
    return (
        f"INSERT INTO {table} (brand, brand_key, ai_analysis_{variant}_json, "
        f"{variant}_workflow_id, {variant}_workflow_revision_id, {variant}_generation_id, "
        f"{variant}_input_hash, {variant}_generated_at, {variant}_source_epoch, "
        f"{variant}_generation_status) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON DUPLICATE KEY UPDATE "
        f"ai_analysis_{variant}_json = VALUES(ai_analysis_{variant}_json), "
        f"{variant}_workflow_id = VALUES({variant}_workflow_id), "
        f"{variant}_workflow_revision_id = VALUES({variant}_workflow_revision_id), "
        f"{variant}_generation_id = VALUES({variant}_generation_id), "
        f"{variant}_input_hash = VALUES({variant}_input_hash), "
        f"{variant}_generated_at = VALUES({variant}_generated_at), "
        f"{variant}_source_epoch = VALUES({variant}_source_epoch), "
        f"{variant}_generation_status = VALUES({variant}_generation_status)"
    )


def _missing_pairs(path: Path) -> list[tuple[str, str, Variant]]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    return [
        (str(row["brand_key"]), str(row["brand"]), variant)
        for row in rows
        for variant in str(row["missing"]).split("|")
        if variant in {"short", "long"}
    ]


def _connect(args: argparse.Namespace) -> Any:
    return pymysql.connect(
        host=args.db_host,
        port=args.db_port,
        user=args.db_user,
        password=os.environ.get("DB_PASSWORD", args.db_password),
        database=args.db_name,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


def _needs_repair(conn: Any, table: str, brand_key: str, variant: Variant) -> bool:
    with conn.cursor() as cursor:
        cursor.execute(
            f"SELECT ai_analysis_{variant}_json AS payload, {variant}_generation_status AS status "
            f"FROM {table} WHERE brand_key = %s",
            (brand_key,),
        )
        row = cursor.fetchone()
    return row is None or row["payload"] is None or row["status"] not in {"complete", FALLBACK_STATUS}


def _repair_value(record: VariantRecord, brand_key: str, brand_name: str) -> tuple[Any, ...]:
    lineage = record.lineage
    return (
        brand_name,
        brand_key,
        json.dumps(record.payload, ensure_ascii=False),
        lineage.workflow_id,
        lineage.workflow_revision_id,
        lineage.generation_id,
        lineage.input_hash,
        lineage.generated_at,
        lineage.source_epoch,
        lineage.generation_status,
    )


def load_validated_trace_records(
    conn: Any,
    pairs: list[tuple[str, str, Variant]],
) -> dict[tuple[str, Variant], VariantRecord]:
    records: dict[tuple[str, Variant], VariantRecord] = {}
    for variant in ("short", "long"):
        names = [brand_name for _, brand_name, pair_variant in pairs if pair_variant == variant]
        runs = _select_runs(conn, names, variant, False)
        outputs = _load_outputs(conn, [int(run["run_id"]) for run in runs.values()])
        for brand_name, run in runs.items():
            payload = _load_payload(run, outputs.get(int(run["run_id"]), []), variant)
            lineage = VariantLineage(**_lineage(run, False))
            records[(brand_name, variant)] = VariantRecord(payload=payload, lineage=lineage)
    return records


def main() -> int:
    parser = argparse.ArgumentParser(description="Fill only missing Agent2 variants with approved KPI templates")
    parser.add_argument("--missing", type=Path, required=True)
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--source", choices=("validated-trace", "template-fallback"), default="template-fallback")
    parser.add_argument("--db-host", default="127.0.0.1")
    parser.add_argument("--db-port", type=int, default=3306)
    parser.add_argument("--db-user", default="root")
    parser.add_argument("--db-password", default="")
    parser.add_argument("--db-name", default="jw_mart_d2_stage_20260630_r2")
    args = parser.parse_args()
    conn = _connect(args)
    repaired: list[dict[str, str]] = []
    try:
        pairs = _missing_pairs(args.missing)
        trace_records = load_validated_trace_records(conn, pairs) if args.source == "validated-trace" else {}
        provider = BatchGeneralZeroKpiSnapshotProvider(conn) if args.source == "template-fallback" else None
        generated_at = datetime.now(timezone.utc)
        for brand_key, brand_name, variant in pairs:
            if not _needs_repair(conn, args.candidate, brand_key, variant):
                continue
            record = trace_records.get((brand_name, variant))
            if record is None and args.source == "validated-trace":
                continue
            if record is None:
                assert provider is not None
                snapshot = provider.get_snapshot(brand_key, brand_name)
                source_epoch_value = getattr(snapshot, "source_epoch", None)
                if not source_epoch_value:
                    raise RuntimeError(f"KPI snapshot has no source period: {brand_key}")
                record = build_fallback_record(
                    brand_key=brand_key,
                    variant=variant,
                    snapshot=snapshot,
                    source_epoch=str(source_epoch_value),
                    generated_at=generated_at,
                )
            with conn.cursor() as cursor:
                affected = cursor.execute(
                    repair_variant_sql(args.candidate, variant),
                    _repair_value(record, brand_key, brand_name),
                )
            if affected != 1:
                raise RuntimeError(f"repair affected {affected} rows for {brand_key}/{variant}")
            repaired.append(
                {"brand_key": brand_key, "variant": variant, "status": record.lineage.generation_status}
            )
        conn.commit()
    finally:
        conn.close()
    print(json.dumps({"repaired": len(repaired), "rows": repaired}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
