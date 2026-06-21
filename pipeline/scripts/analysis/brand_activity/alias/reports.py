from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
import json

from pipeline.scripts.analysis.brand_activity.alias.builder import AliasBuildResult, AliasRecord


def write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def render_mapping_json(
    result: AliasBuildResult,
    snapshot: dict[str, object],
    metadata: dict[str, object],
) -> dict[str, object]:
    return {
        "metadata": metadata,
        "stage_snapshot": snapshot["distincts_and_intersections"],
        "records": [record.to_json_dict() for record in result.records],
    }


def _table(headers: tuple[str, ...], rows: list[tuple[object, ...]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(str(cell) if cell not in (None, "") else "-" for cell in row) + " |")
    return lines


def _market_key(record: AliasRecord) -> str:
    if record.atc4:
        return ",".join(record.atc4)
    if record.csd_market:
        return ",".join(record.csd_market)
    return "NO_MARKET"


def render_review_md(
    result: AliasBuildResult,
    jw_canonicals: tuple[str, ...],
    unresolved_questions: list[str],
) -> str:
    grouped: defaultdict[str, list[AliasRecord]] = defaultdict(list)
    for record in result.records:
        grouped[_market_key(record)].append(record)
    lines = [
        "# ALIAS_02_REVIEW",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        "## Summary",
        "",
        *[
            f"- English anchors: {result.stats.anchor_count}",
            f"- Configured spelling-variant rules: {result.stats.configured_variant_rule_count}",
            f"- Observed multi-variant groups: {result.stats.observed_multi_variant_rule_count}",
            f"- CSD-uncovered anchors: {result.stats.csd_uncovered_count}",
            f"- JW canonical mapped: {result.stats.jw_mapped_count}/{len(jw_canonicals)}",
        ],
        "",
        "## Review Needed",
        "",
    ]
    pending = [record for record in result.records if record.mapping_status == "pending"]
    lines.extend(_table(
        ("iqvia_en", "sources", "atc4", "csd_market", "note"),
        [
            (
                record.iqvia_en,
                ",".join(key for key, value in record.sources.items() if value),
                ",".join(record.atc4),
                ",".join(record.csd_market),
                record.note,
            )
            for record in pending[:80]
        ],
    ))
    if len(pending) > 80:
        lines.append(f"\nPending list truncated in review view: {len(pending)} total. Full list is in ALIAS_01_MAPPING.json.")
    lines.extend(["", "## CSD Uncovered", ""])
    uncovered = [record for record in result.records if record.csd_uncovered]
    lines.extend(_table(
        ("iqvia_en", "atc4", "sources", "kr_canonical", "note"),
        [
            (
                record.iqvia_en,
                ",".join(record.atc4),
                ",".join(key for key, value in record.sources.items() if value),
                record.kr_canonical,
                record.note,
            )
            for record in uncovered
        ],
    ))
    lines.extend(["", "## Market Product Lists", ""])
    for market in sorted(grouped):
        records = grouped[market]
        lines.extend([f"### {market}", ""])
        lines.extend(_table(
            ("iqvia_en", "variants", "sources", "kr_canonical", "status", "company"),
            [
                (
                    record.iqvia_en,
                    ", ".join(record.variants),
                    ",".join(key for key, value in record.sources.items() if value),
                    record.kr_canonical,
                    record.mapping_status,
                    ", ".join(record.representing_company),
                )
                for record in records
            ],
        ))
        lines.append("")
    lines.extend(["## Open Questions", ""])
    lines.extend(f"- {question}" for question in unresolved_questions)
    return "\n".join(lines).rstrip() + "\n"


def render_validation_md(
    result: AliasBuildResult,
    snapshot: dict[str, object],
    jw_canonicals: tuple[str, ...],
    unresolved_questions: list[str],
) -> str:
    mapped_jw = sorted({record.kr_canonical for record in result.records if record.kr_canonical})
    missing_jw = [name for name in jw_canonicals if name not in mapped_jw]
    by_anchor = result.by_anchor
    nonmerge_pairs = (
        ("TENELA", "TENELIA"),
        ("NEUSTATIN", "NEUSTATIN-A"),
        ("NEUSTATIN", "NEUSTATIN-R"),
    )
    status_rows = [(status, count) for status, count in result.stats.status_distribution.items()]
    source_combo_counts = Counter(
        "+".join(key for key, value in record.sources.items() if value) for record in result.records
    )
    lines = [
        "# ALIAS_03_VALIDATION",
        "",
        f"Generated: {datetime.now(timezone.utc).isoformat()}",
        "",
        "## Input Snapshot",
        "",
        *[
            f"- `{key}`: {value}"
            for key, value in dict(snapshot["distincts_and_intersections"]).items()
        ],
        "",
        "## Variant Handling",
        "",
        *[
            f"- Configured variant groups: {result.stats.configured_variant_rule_count}",
            f"- Observed multi-variant groups: {result.stats.observed_multi_variant_rule_count}",
            f"- `A-PITO` and `APITO` anchor: {'APITO' in by_anchor}",
            f"- `LOWOSMOPERI` and `LOW OSMO PERI` anchor: {'LOW OSMO PERI' in by_anchor}",
        ],
        "",
        "## Similar Pair Nonmerge Check",
        "",
    ]
    lines.extend(_table(
        ("left", "right", "left_exists", "right_exists", "separate"),
        [
            (left, right, left in by_anchor, right in by_anchor, left in by_anchor and right in by_anchor and left != right)
            for left, right in nonmerge_pairs
        ],
    ))
    lines.extend(["", "## JW 25 Canonical Coverage", ""])
    lines.extend(_table(
        ("metric", "value"),
        [
            ("mapped", f"{result.stats.jw_mapped_count}/{len(jw_canonicals)}"),
            ("missing", ", ".join(missing_jw) if missing_jw else "none"),
        ],
    ))
    lines.extend(["", "## ATC4 And Status", ""])
    lines.extend(_table(
        ("metric", "value"),
        [
            ("atc4_attached", f"{result.stats.atc4_attached_count}/{result.stats.anchor_count}"),
            ("csd_uncovered", result.stats.csd_uncovered_count),
        ],
    ))
    lines.extend(["", "### mapping_status distribution", ""])
    lines.extend(_table(("mapping_status", "count"), status_rows))
    lines.extend(["", "### source combination distribution", ""])
    lines.extend(_table(("sources", "count"), sorted(source_combo_counts.items())))
    lines.extend(["", "## Open Questions", ""])
    lines.extend(f"- {question}" for question in unresolved_questions)
    return "\n".join(lines).rstrip() + "\n"
