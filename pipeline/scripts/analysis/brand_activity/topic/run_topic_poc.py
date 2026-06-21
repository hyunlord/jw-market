from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[5]))

from pipeline.scripts.analysis.brand_activity.topic.compare import (  # noqa: E402
    compare_rule_cluster_alignment,
    proxy_f1_estimate,
)
from pipeline.scripts.analysis.brand_activity.topic.data_source import (  # noqa: E402
    connect_mariadb,
    fetch_messages,
    fetch_table_snapshot,
)
from pipeline.scripts.analysis.brand_activity.topic.embedding import (  # noqa: E402
    cluster_market,
    cluster_member_weights,
    cluster_silhouette,
    summarize_clusters,
)
from pipeline.scripts.analysis.brand_activity.topic.models import (  # noqa: E402
    AlignmentRow,
    ClusterSummary,
    JsonValue,
    MessageRecord,
    text_sha256,
)
from pipeline.scripts.analysis.brand_activity.topic.privacy import audit_safe_record  # noqa: E402
from pipeline.scripts.analysis.brand_activity.topic.profile import (  # noqa: E402
    deduplicate_messages,
    profile_by_market,
)
from pipeline.scripts.analysis.brand_activity.topic.reports import (  # noqa: E402
    render_comparison_doc,
    render_eval_doc,
    render_profile_doc,
    render_track_a_doc,
    render_track_b_doc,
)
from pipeline.scripts.analysis.brand_activity.topic.rules import build_seed_rules, match_rules  # noqa: E402


def parse_args() -> argparse.Namespace:
    """Parse the read-only local PoC command line."""
    parser = argparse.ArgumentParser(description="Run Keyword/Meeting topic extraction PoC.")
    parser.add_argument("--audit-dir", type=Path, default=Path("audit/brand_activity_topic"))
    parser.add_argument("--output-dir", type=Path, default=Path("output/brand_activity_topic"))
    parser.add_argument("--docs-dir", type=Path, default=Path("docs/research/brand_activity/topic"))
    parser.add_argument("--env-path", type=Path, default=Path("pipeline/docker/.env"))
    parser.add_argument("--stage-schema", default="jw_brand_activity_stage")
    parser.add_argument("--top-markets", type=int, default=5)
    return parser.parse_args()


def ensure_dirs(*paths: Path) -> None:
    """Create local analysis output directories."""
    for path in paths:
        path.mkdir(parents=True, exist_ok=True)


def write_json(path: Path, payload: Any) -> None:
    """Write deterministic UTF-8 JSON with Korean text preserved."""
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    """Write a UTF-8 text artifact."""
    path.write_text(text, encoding="utf-8")


def utc_timestamp() -> str:
    """Return an ISO timestamp for audit lineage."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def keyword_rows(rows: list[MessageRecord]) -> list[MessageRecord]:
    """Select Keyword messages as the primary coverage denominator."""
    return [row for row in rows if row.source == "keyword"]


def meeting_auxiliary_rows(rows: list[MessageRecord]) -> list[MessageRecord]:
    """Select Meeting topic/verbatim messages used as auxiliary sanity evidence."""
    return [row for row in rows if row.source != "keyword"]


def market_order(rows: list[MessageRecord], limit: int) -> list[str]:
    """Return top markets by raw Keyword row volume."""
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.market] = counts.get(row.market, 0) + row.frequency
    return [market for market, _ in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]]


def rows_by_market(rows: list[MessageRecord], markets: list[str]) -> dict[str, list[MessageRecord]]:
    """Group deduplicated records for selected markets only."""
    grouped = {market: [] for market in markets}
    for row in rows:
        if row.market in grouped:
            grouped[row.market].append(row)
    return grouped


def model_info() -> dict[str, JsonValue]:
    """Record executed local embedding and checked neural model candidates."""
    return {
        "executed_embedding": {
            "name": "local_tfidf_char_ngram_svd",
            "dimension": "up to 64",
            "normalization": "L2",
            "execution_location": "local Codex workspace",
            "license": "scikit-learn BSD-3-Clause runtime dependency",
            "external_api_calls": "0",
            "reason": "Reproducible local embedding/clustering without model download or LLM/API cost.",
        },
        "candidate_models": [
            {
                "model": "intfloat/multilingual-e5-base",
                "dimension": 768,
                "license": "mit",
                "status": "preferred DGX/local neural candidate; not downloaded in this run",
                "url": "https://huggingface.co/intfloat/multilingual-e5-base",
                "sha_checked": "d128750597153bb5987e10b1c3493a34e5a4502a",
            },
            {
                "model": "BAAI/bge-m3",
                "dimension": 1024,
                "license": "mit",
                "status": "strong multilingual candidate; larger model, not downloaded in this run",
                "url": "https://huggingface.co/BAAI/bge-m3",
                "sha_checked": "5617a9f61b028005a4858fdac845db406aefb181",
            },
            {
                "model": "jhgan/ko-sroberta-multitask",
                "dimension": 768,
                "license": "not declared in fetched HF card/API",
                "status": "Korean-only candidate requiring license review before production use",
                "url": "https://huggingface.co/jhgan/ko-sroberta-multitask",
                "sha_checked": "8fca7c9c98c26599be0e14b9916b11a756a26f19",
            },
        ],
        "cluster_methods": ["kmeans", "birch"],
        "unavailable_methods": ["hdbscan"],
    }


def cluster_selected_markets(
    grouped_rows: dict[str, list[MessageRecord]],
) -> tuple[list[ClusterSummary], dict[str, dict[str, dict[str, int]]], dict[str, float | None]]:
    """Run Track B clustering for each selected market and method."""
    rules = build_seed_rules()
    summaries: list[ClusterSummary] = []
    clusters: dict[str, dict[str, dict[str, int]]] = {}
    silhouettes: dict[str, float | None] = {}
    for market, market_rows in grouped_rows.items():
        labels_by_method = cluster_market(market_rows)
        for method, labels in labels_by_method.items():
            key = f"{market}:{method}"
            summaries.extend(summarize_clusters(market, market_rows, labels, method, rules))
            clusters[key] = cluster_member_weights(market_rows, labels, market, method)
            silhouettes[key] = cluster_silhouette(market_rows, labels)
    return summaries, clusters, silhouettes


def redacted_cluster_payload(summaries: list[ClusterSummary]) -> list[dict[str, JsonValue]]:
    """Serialize cluster summaries without representative raw sentences."""
    payload = []
    for summary in summaries:
        row = asdict(summary)
        row.pop("representative_sentences", None)
        payload.append(row)
    return payload


def redacted_profile_payload(profiles: dict[str, dict[str, JsonValue]]) -> dict[str, dict[str, JsonValue]]:
    """Serialize profile metrics while hashing raw token and n-gram strings."""
    redacted: dict[str, dict[str, JsonValue]] = {}
    for market, profile in profiles.items():
        market_profile = dict(profile)
        for key in ("top_tokens", "top_bigrams", "top_trigrams"):
            market_profile[key] = [
                {"term_hash": text_sha256(str(term)), "term_len": len(str(term)), "count": count}
                for term, count in profile[key]
            ]
        redacted[market] = market_profile
    return redacted


def comparison_payload(
    top_markets: list[str],
    match_assignments: dict[str, tuple[str, ...]],
    match_weights: dict[str, int],
    rule_stats: dict[str, Any],
    cluster_weights: dict[str, dict[str, dict[str, int]]],
) -> tuple[list[dict[str, JsonValue]], list[AlignmentRow]]:
    """Build market comparison rows and primary KMeans crosswalks."""
    comparison_rows: list[dict[str, JsonValue]] = []
    all_alignments: list[AlignmentRow] = []
    for market in top_markets:
        stats = rule_stats[market]
        clusters = cluster_weights.get(f"{market}:kmeans", {})
        alignments = compare_rule_cluster_alignment(match_assignments, clusters)
        all_alignments.extend(alignments)
        weighted_alignment = weighted_alignment_share(alignments)
        coverage = stats.matched_weight / stats.total_weight if stats.total_weight else 0.0
        llm_need = stats.unmatched_weight / stats.total_weight if stats.total_weight else 0.0
        metrics = proxy_f1_estimate(coverage, weighted_alignment)
        row: dict[str, JsonValue] = {
            "market": market,
            "rule_coverage": round(coverage, 4),
            "llm_need_estimate": round(llm_need, 4),
            "alignment_share": round(weighted_alignment, 4),
            "assigned_weight": match_weights.get(market, 0),
        }
        row.update(metrics)
        comparison_rows.append(row)
    return comparison_rows, all_alignments


def weighted_alignment_share(alignments: list[AlignmentRow]) -> float:
    """Average cluster-label alignment by cluster size."""
    total = sum(row.weighted_size for row in alignments)
    if total == 0:
        return 0.0
    return sum(row.weighted_label_share * row.weighted_size for row in alignments) / total


def labels_by_market(rules: list[Any]) -> dict[str, list[str]]:
    """List draft labels by ATC4 for evaluation setup."""
    labels: dict[str, list[str]] = {}
    for rule in rules:
        labels.setdefault(rule.market, []).append(rule.label)
    return labels


def alignment_table_rows(alignments: list[AlignmentRow]) -> list[list[object]]:
    """Return compact Track A/B crosswalk rows for the comparison report."""
    return [
        [row.cluster_id, row.top_rule_label, f"{row.weighted_label_share * 100:.1f}%", row.weighted_size]
        for row in sorted(alignments, key=lambda item: (item.cluster_id))
    ]


def redaction_scan(paths: list[Path], source_rows: list[MessageRecord]) -> dict[str, JsonValue]:
    """Scan audit/output files for accidental full raw message strings."""
    sensitive_values = sorted({row.message_text for row in source_rows if len(row.message_text) >= 8})
    matches: list[dict[str, str]] = []
    scanned_files = 0
    for root in paths:
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in {".json", ".csv", ".tsv", ".txt", ".log"}:
                continue
            scanned_files += 1
            content = path.read_text(encoding="utf-8", errors="ignore")
            for value in sensitive_values:
                if value in content:
                    matches.append({"file": str(path), "message_hash": hashlib.sha256(value.encode()).hexdigest()})
                    break
    return {
        "scanned_files": scanned_files,
        "sensitive_values_tested": len(sensitive_values),
        "raw_sensitive_matches": len(matches),
        "matches": matches[:20],
    }


def sha256_file(path: Path) -> str:
    """Hash an artifact for the manifest."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest(paths: list[Path]) -> list[dict[str, str]]:
    """Create a SHA256 manifest for generated files."""
    entries = []
    for root in paths:
        for path in sorted(root.rglob("*")):
            if "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            if path.is_file():
                entries.append({"path": str(path), "sha256": sha256_file(path)})
    return entries


def main() -> int:
    """Run the full read-only topic extraction PoC."""
    args = parse_args()
    ensure_dirs(args.audit_dir, args.audit_dir / "logs", args.output_dir, args.docs_dir)
    generated_at = utc_timestamp()
    connection = connect_mariadb(args.env_path)
    try:
        before_snapshot = fetch_table_snapshot(connection, args.stage_schema)
        all_rows = fetch_messages(connection, args.stage_schema)
        after_snapshot = fetch_table_snapshot(connection, args.stage_schema)
    finally:
        connection.close()

    primary_rows = keyword_rows(all_rows)
    auxiliary_rows = meeting_auxiliary_rows(all_rows)
    top_markets = market_order(primary_rows, args.top_markets)
    deduped = deduplicate_messages(primary_rows)
    auxiliary_deduped = deduplicate_messages(auxiliary_rows)
    selected_rows = [row for row in deduped if row.market in top_markets]
    selected_auxiliary_rows = [row for row in auxiliary_deduped if row.market in top_markets]
    selected_by_market = rows_by_market(selected_rows, top_markets)
    rows_by_id = {row.message_id: row for row in selected_rows}

    profiles = profile_by_market(primary_rows)
    rules = build_seed_rules()
    match_result = match_rules(selected_rows, rules)
    auxiliary_match_result = match_rules(selected_auxiliary_rows, rules)
    cluster_summaries, cluster_weights, silhouettes = cluster_selected_markets(selected_by_market)

    assignment_labels = {key: assignment.labels for key, assignment in match_result.assignments.items()}
    weights_by_market = {
        market: sum(row.frequency for row in market_rows) for market, market_rows in selected_by_market.items()
    }
    comparison_rows, alignments = comparison_payload(
        top_markets,
        assignment_labels,
        weights_by_market,
        match_result.market_stats,
        cluster_weights,
    )

    snapshot = {
        **before_snapshot,
        "meeting_auxiliary_text_rows": len([row for row in all_rows if row.source != "keyword"]),
        "db_non_write_before": before_snapshot,
        "db_non_write_after": after_snapshot,
        "db_non_write_equal": before_snapshot == after_snapshot,
    }
    info = model_info()
    write_json(args.audit_dir / "input_row_snapshot.json", snapshot)
    write_json(args.audit_dir / "model_info.json", info)
    write_json(args.output_dir / "data_profile.json", redacted_profile_payload(profiles))
    write_json(args.output_dir / "rule_summary.json", {
        "market_stats": {market: stats.to_dict() for market, stats in match_result.market_stats.items()},
        "meeting_auxiliary_market_stats": {
            market: stats.to_dict() for market, stats in auxiliary_match_result.market_stats.items()
        },
        "label_counts": match_result.label_counts,
    })
    write_json(args.output_dir / "cluster_summary_redacted.json", redacted_cluster_payload(cluster_summaries))
    write_json(args.output_dir / "comparison_summary.json", comparison_rows)
    write_json(args.output_dir / "message_rows_redacted_sample.json", [audit_safe_record(row) for row in selected_rows[:200]])

    write_text(args.docs_dir / "TOPIC_01_DATA_PROFILE.md", render_profile_doc(generated_at, snapshot, profiles, top_markets))
    write_text(
        args.docs_dir / "TOPIC_02_TRACK_A_RULE_SEMIAUTO.md",
        render_track_a_doc(generated_at, top_markets, rules, match_result, rows_by_id, auxiliary_match_result),
    )
    write_text(args.docs_dir / "TOPIC_03_TRACK_B_EMBEDDING_AUTO.md", render_track_b_doc(generated_at, info, cluster_summaries, silhouettes))
    write_text(args.docs_dir / "TOPIC_04_COMPARISON_AND_RECO.md", render_comparison_doc(generated_at, comparison_rows, alignment_table_rows(alignments)))
    write_text(args.docs_dir / "TOPIC_05_EVAL_SETUP.md", render_eval_doc(generated_at, top_markets, labels_by_market(rules)))

    scan = redaction_scan([args.audit_dir, args.output_dir], all_rows)
    write_json(args.audit_dir / "redaction_scan.json", scan)
    manifest = build_manifest([args.audit_dir, args.output_dir, args.docs_dir, Path(__file__).parent, Path("tests/analysis")])
    write_json(args.audit_dir / "topic_poc_manifest.json", manifest)
    print(json.dumps({"top_markets": top_markets, "comparison": comparison_rows, "redaction_scan": scan}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
