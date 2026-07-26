from __future__ import annotations

import argparse
import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from statistics import median

from .repository import Connection, connect_from_env
from .typed_extraction import StructuredParseResult, parse_stored_raw_text


@dataclass(frozen=True, slots=True)
class ReparseSource:
    source_notice_id: str
    raw_text: str


@dataclass(frozen=True, slots=True)
class ReparseRecord:
    source_notice_id: str
    raw_text: str
    parsed: StructuredParseResult


@dataclass(frozen=True, slots=True)
class ReparseSummary:
    population: int
    parse_status: dict[str, int]
    field_nonempty: dict[str, int]
    target_suffix_count: int
    target_raw_ratio_median: float


def load_reparse_rows(conn: Connection) -> tuple[ReparseSource, ...]:
    """Load only the persisted identity and parser input."""

    with conn.cursor() as cursor:
        cursor.execute(
            """
            SELECT source_notice_id, raw_text
            FROM hira_benefit_notice
            ORDER BY source_notice_id
            """
        )
        rows = cursor.fetchall()
    return tuple(
        ReparseSource(
            source_notice_id=str(row["source_notice_id"]),
            raw_text=str(row["raw_text"]),
        )
        for row in rows
    )


def build_reparse_plan(rows: Sequence[ReparseSource]) -> tuple[ReparseRecord, ...]:
    """Build a deterministic in-memory plan without database writes."""

    return tuple(
        ReparseRecord(
            source_notice_id=row.source_notice_id,
            raw_text=row.raw_text,
            parsed=parse_stored_raw_text(row.raw_text),
        )
        for row in rows
    )


def summarize_reparse_plan(plan: Sequence[ReparseRecord]) -> ReparseSummary:
    statuses = Counter(record.parsed.parse_status.value for record in plan)
    ratios = [
        len(record.parsed.target_condition) / len(record.raw_text)
        for record in plan
        if record.parsed.target_condition and record.raw_text
    ]
    return ReparseSummary(
        population=len(plan),
        parse_status=dict(sorted(statuses.items())),
        field_nonempty={
            "target_condition": sum(
                record.parsed.target_condition is not None for record in plan
            ),
            "exclusion_rule": sum(
                record.parsed.exclusion_rule is not None for record in plan
            ),
            "dosage_limit": sum(
                record.parsed.dosage_limit is not None for record in plan
            ),
        },
        target_suffix_count=sum(
            bool(record.parsed.target_condition)
            and record.raw_text.endswith(record.parsed.target_condition or "")
            for record in plan
        ),
        target_raw_ratio_median=median(ratios) if ratios else 0.0,
    )


def apply_reparse_plan(
    conn: Connection,
    plan: Sequence[ReparseRecord],
) -> None:
    """Update only typed parser outputs and commit the full plan atomically."""

    committed = False
    try:
        with conn.cursor() as cursor:
            for record in plan:
                updated_rows = cursor.execute(
                    """
                    UPDATE hira_benefit_notice
                    SET target_condition=%s,
                        exclusion_rule=%s,
                        dosage_limit=%s,
                        parse_status=%s,
                        parse_failed_fields_json=%s
                    WHERE source_notice_id=%s
                    """,
                    (
                        record.parsed.target_condition,
                        record.parsed.exclusion_rule,
                        record.parsed.dosage_limit,
                        record.parsed.parse_status.value,
                        json.dumps(
                            record.parsed.failed_fields,
                            ensure_ascii=False,
                        ),
                        record.source_notice_id,
                    ),
                )
                if updated_rows != 1:
                    raise RuntimeError(
                        "expected one updated row for "
                        f"source_notice_id={record.source_notice_id!r}, "
                        f"actual={updated_rows}"
                    )
        conn.commit()
        committed = True
    finally:
        if not committed:
            conn.rollback()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Reparse persisted HIRA raw_text without recrawling."
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--execute", action="store_true")
    parser.add_argument("--expected-population", type=int)
    return parser


def main() -> int:
    args = _parser().parse_args()
    conn = connect_from_env()
    try:
        plan = build_reparse_plan(load_reparse_rows(conn))
        if args.expected_population is not None and len(plan) != args.expected_population:
            raise RuntimeError(
                "population mismatch: "
                f"expected={args.expected_population} actual={len(plan)}"
            )
        if args.execute:
            if args.expected_population is None:
                raise RuntimeError("--execute requires --expected-population")
            apply_reparse_plan(conn, plan)
        print(
            json.dumps(
                asdict(summarize_reparse_plan(plan)),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
