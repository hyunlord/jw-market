from __future__ import annotations

from collections import defaultdict

from .models import ClusterSummary, JsonValue, MessageRecord, RuleMatchResult, TopicRule
from .text_tokens import token_counts


def pct(value: float) -> str:
    """Format a ratio as a report percentage."""
    return f"{value * 100:.1f}%"


def markdown_table(headers: list[str], rows: list[list[object]]) -> str:
    """Render a compact GitHub-flavored Markdown table."""
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    lines.extend("| " + " | ".join(str(value) for value in row) + " |" for row in rows)
    return "\n".join(lines)


def clip_text(value: str, limit: int = 120) -> str:
    """Limit representative review text so docs do not become raw dumps."""
    return value if len(value) <= limit else value[: limit - 1] + "..."


def render_profile_doc(
    generated_at: str,
    source_snapshot: dict[str, int],
    profiles: dict[str, dict[str, JsonValue]],
    top_markets: list[str],
) -> str:
    """Render TOPIC_01 data profile from measured stage data."""
    total_keyword = source_snapshot["km_keyword_event_stage"]
    rows = []
    for market, profile in sorted(profiles.items(), key=lambda item: int(item[1]["rows"]), reverse=True):
        langs = profile["language_counts"]
        assert isinstance(langs, dict)
        rows.append(
            [
                market,
                profile["rows"],
                profile["unique_messages"],
                pct(float(profile["duplicate_rate"])),
                pct(int(profile["rows"]) / total_keyword),
                langs.get("korean", 0),
                langs.get("mixed_ko_en", 0),
                langs.get("english", 0),
                profile["length_median"],
                profile["length_p90"],
            ]
        )
    sections = [
        "# TOPIC_01_DATA_PROFILE",
        "",
        f"- Generated: {generated_at}",
        "- Sensitivity: no raw source messages are stored in audit/output; this report shows aggregate tokens and counts for PL review.",
        "- Scope: local stage DB read-only profile. Audit/output artifacts keep hashes and aggregates only.",
        "",
        "## Source Row Snapshot",
        "",
        markdown_table(["stage table", "rows"], [[table, count] for table, count in source_snapshot.items()]),
        "",
        "## Keyword Market Profile",
        "",
        markdown_table(
            ["ATC4", "rows", "unique", "dup", "share", "ko", "mixed", "en", "median len", "p90 len"],
            rows,
        ),
    ]
    for market in top_markets:
        profile = profiles[market]
        sections.extend(
            [
                "",
                f"## {market} Token/N-gram Signals",
                "",
                markdown_table(["token", "count"], [list(item) for item in profile["top_tokens"][:15]]),
                "",
                markdown_table(["bigram", "count"], [list(item) for item in profile["top_bigrams"][:10]]),
            ]
        )
    return "\n".join(sections) + "\n"


def render_track_a_doc(
    generated_at: str,
    top_markets: list[str],
    rules: list[TopicRule],
    match_result: RuleMatchResult,
    rows_by_id: dict[str, MessageRecord],
    auxiliary_match_result: RuleMatchResult | None = None,
) -> str:
    """Render TOPIC_02 semi-automatic rule/dictionary results."""
    coverage_rows = []
    for market in top_markets:
        stats = match_result.market_stats[market]
        serialized = stats.to_dict()
        coverage_rows.append(
            [
                market,
                serialized["total_rows"],
                serialized["total_weight"],
                pct(float(serialized["matched_weight_rate"])),
                stats.unmatched_weight,
                stats.multilabel_rows,
            ]
        )
    sections = [
        "# TOPIC_02_TRACK_A_RULE_SEMIAUTO",
        "",
        f"- Generated: {generated_at}",
        "- Sensitivity: representative sentences below are limited for PL label review; do not redistribute as raw audit data.",
        "",
        "## Rule Coverage",
        "",
        markdown_table(
            ["ATC4", "dedup rows", "weighted rows", "matched weight", "unmatched weight", "multilabel rows"],
            coverage_rows,
        ),
    ]
    if auxiliary_match_result is not None:
        sections.extend(["", "## Meeting Auxiliary Rule Coverage", ""])
        aux_rows = []
        for market in top_markets:
            stats = auxiliary_match_result.market_stats.get(market)
            if stats is None:
                continue
            serialized = stats.to_dict()
            aux_rows.append(
                [
                    market,
                    serialized["total_rows"],
                    serialized["total_weight"],
                    pct(float(serialized["matched_weight_rate"])),
                    stats.unmatched_weight,
                    stats.multilabel_rows,
                ]
            )
        sections.append(
            markdown_table(
                ["ATC4", "dedup aux rows", "weighted aux rows", "matched weight", "unmatched weight", "multilabel rows"],
                aux_rows,
            )
        )
    by_market: defaultdict[str, list[TopicRule]] = defaultdict(list)
    for rule in rules:
        by_market[rule.market].append(rule)
    label_to_messages = representative_by_label(match_result, rows_by_id)
    for market in top_markets:
        sections.extend(["", f"## {market} Label Candidates", ""])
        rows = []
        for rule in by_market.get(market, []):
            reps = "<br>".join(clip_text(text) for text in label_to_messages.get((market, rule.label), [])[:3])
            count = match_result.label_counts.get(market, {}).get(rule.label, 0)
            rows.append([rule.label, count, ", ".join(rule.keywords[:8]), reps])
        sections.append(markdown_table(["candidate label", "weighted hits", "draft keywords", "representative sentences"], rows))
        unmatched = [
            rows_by_id[assignment.message_id].message_text
            for assignment in match_result.assignments.values()
            if assignment.market == market and not assignment.labels
        ]
        missing_terms = ", ".join(term for term, _ in token_counts(unmatched).most_common(15))
        sections.extend(["", f"Unmatched feedback terms: {missing_terms or 'none'}"])
    return "\n".join(sections) + "\n"


def representative_by_label(
    match_result: RuleMatchResult,
    rows_by_id: dict[str, MessageRecord],
) -> dict[tuple[str, str], list[str]]:
    """Select high-frequency representative text for each rule label."""
    buckets: defaultdict[tuple[str, str], list[MessageRecord]] = defaultdict(list)
    for assignment in match_result.assignments.values():
        row = rows_by_id[assignment.message_id]
        for label in assignment.labels:
            buckets[(assignment.market, label)].append(row)
    return {
        key: [row.message_text for row in sorted(items, key=lambda item: (-item.frequency, item.message_hash))]
        for key, items in buckets.items()
    }


def render_track_b_doc(
    generated_at: str,
    model_info: dict[str, JsonValue],
    cluster_summaries: list[ClusterSummary],
    silhouettes: dict[str, float | None],
) -> str:
    """Render TOPIC_03 local embedding and clustering results."""
    sections = [
        "# TOPIC_03_TRACK_B_EMBEDDING_AUTO",
        "",
        f"- Generated: {generated_at}",
        "- Sensitivity: representative sentences below are limited for PL label review; audit/output JSON uses hashes only.",
        "- External LLM API calls: none.",
        "",
        "## Executed Embedding",
        "",
        markdown_table(
            ["field", "value"],
            [[key, value] for key, value in model_info["executed_embedding"].items()],
        ),
        "",
        "## Neural Candidate Check",
        "",
        markdown_table(
            ["model", "dimension", "license", "status"],
            [
                [item["model"], item["dimension"], item["license"], item["status"]]
                for item in model_info["candidate_models"]
            ],
        ),
        "",
        "## Method Scores",
        "",
        markdown_table(["market/method", "silhouette"], [[key, value] for key, value in silhouettes.items()]),
        "",
        "## Seed-Anchor Variant",
        "",
        "Cluster labels are not generated by an LLM. The PoC computes cluster top terms, then anchors them against the Track A market-specific seed dictionaries. When no seed overlaps, the label falls back to the top cluster terms and is marked as a 신규 후보 for PL review.",
        "",
        "## Fit Caveat",
        "",
        "Silhouette values are low because the messages are short, overlapping, and often intentionally multi-topic. Treat clusters as discovery/audit candidates, not final business labels.",
    ]
    by_market_method: defaultdict[tuple[str, str], list[ClusterSummary]] = defaultdict(list)
    for summary in cluster_summaries:
        by_market_method[(summary.market, summary.method)].append(summary)
    for key, summaries in sorted(by_market_method.items()):
        market, method = key
        sections.extend(["", f"## {market} {method} Clusters", ""])
        rows = []
        for summary in sorted(summaries, key=lambda item: (-item.weighted_size, item.cluster_id)):
            reps = "<br>".join(clip_text(text) for text in summary.representative_sentences[:3])
            rows.append(
                [
                    summary.cluster_id,
                    summary.weighted_size,
                    ", ".join(summary.top_terms[:8]),
                    summary.suggested_label,
                    reps,
                ]
            )
        sections.append(markdown_table(["cluster", "weighted size", "top terms", "auto label", "representatives"], rows))
    return "\n".join(sections) + "\n"


def render_comparison_doc(
    generated_at: str,
    comparison_rows: list[dict[str, JsonValue]],
    alignment_rows: list[list[object]],
) -> str:
    """Render TOPIC_04 comparison, evaluation proxy, and recommendation."""
    recommendation = (
        "Codex recommendation: operate a market-specific deterministic dictionary as the first decision layer, "
        "send only unmatched or low-confidence new clusters to GenOS Flash/Lite after manual label-set approval, "
        "and keep embedding clustering as a monthly discovery/audit loop rather than a direct production classifier."
    )
    sections = [
        "# TOPIC_04_COMPARISON_AND_RECO",
        "",
        f"- Generated: {generated_at}",
        "- Sensitivity: this report uses aggregate counts and cluster/rule IDs; representative raw sentences are confined to TOPIC_02 and TOPIC_03.",
        "- Ground truth status: `2025 Message Count` is not a topic label source; F1 values below are proxy estimates pending manual labels.",
        "",
        "## Market-Level Decision Split",
        "",
        markdown_table(
            [
                "ATC4",
                "rule coverage",
                "LLM need est.",
                "A/B alignment",
                "rule F1 proxy",
                "embedding F1 proxy",
                "hybrid F1 proxy",
            ],
            [
                [
                    row["market"],
                    pct(float(row["rule_coverage"])),
                    pct(float(row["llm_need_estimate"])),
                    pct(float(row["alignment_share"])),
                    row["rule_f1_proxy"],
                    row["embedding_f1_proxy"],
                    row["hybrid_f1_proxy"],
                ]
                for row in comparison_rows
            ],
        ),
        "",
        "## Track A/B Crosswalk",
        "",
        markdown_table(["cluster", "top rule label", "weighted share", "weighted size"], alignment_rows),
        "",
        "## Operating Recommendation",
        "",
        recommendation,
        "",
        "## GenOS Tier Design Estimate",
        "",
        "- No GenOS/API calls were made. The monthly call count should be the cache-missed unique unmatched messages only.",
        "- Flash is the default fit for schema-constrained multi-label classification; Lite is acceptable only after the manual set proves no material F1 loss.",
        "- JSON schema, label enum, temperature 0, input hash cache, and retry quarantine are mandatory controls.",
        "- Cost formula: `monthly_unmatched_unique * avg_prompt_tokens * selected_tier_unit_price`; stage data should supply the first two terms before procurement fills price.",
    ]
    return "\n".join(sections) + "\n"


def render_eval_doc(generated_at: str, top_markets: list[str], labels_by_market: dict[str, list[str]]) -> str:
    """Render TOPIC_05 manual truth-set and metric design."""
    sample_rows = [[market, "50-100 unique messages", ", ".join(labels_by_market.get(market, [])[:10]), "기타/신규 후보 허용"] for market in top_markets]
    sections = [
        "# TOPIC_05_EVAL_SETUP",
        "",
        f"- Generated: {generated_at}",
        "- Sensitivity: manual labeling samples should be distributed only to PL/marketing reviewers and stored with message hashes plus restricted review text.",
        "- Message Count is not ground truth; this setup assumes PL/marketing manual multi-labeling.",
        "",
        "## Stratified Labeling Sample",
        "",
        markdown_table(["ATC4", "sample size", "draft labels", "notes"], sample_rows),
        "",
        "## Labeling Guide",
        "",
        "1. Assign every business topic that is explicitly supported by the message.",
        "2. Use market-local labels only; do not carry C10 labels into PPI/DPP-4 markets.",
        "3. Use `기타/신규 후보` when a message is meaningful but outside the approved label set.",
        "4. Mark `판단불가` only when the message is too vague after PL review.",
        "",
        "## Metrics",
        "",
        "- Multi-label micro/macro precision, recall, and F1.",
        "- Coverage: percentage of messages with at least one non-other label.",
        "- Other/new-topic rate by market and by month.",
        "- Inter-annotator agreement on 20% overlap before final adjudication.",
        "",
        "## Approval Workflow",
        "",
        "1. Machine candidates from Track A/B are merged and renamed by PL.",
        "2. Marketing owners approve 5-10 labels per ATC4 plus boundary rules.",
        "3. Two reviewers label the stratified sample; disagreements become label-definition updates.",
        "4. Freeze dictionary/model/prompt versions and re-run the PoC metrics before operational design.",
    ]
    return "\n".join(sections) + "\n"
