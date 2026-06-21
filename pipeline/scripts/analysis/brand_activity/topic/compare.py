"""Comparison helpers for Track A rules and Track B clusters."""

from __future__ import annotations

from collections import defaultdict

from .models import AlignmentRow


def compare_rule_cluster_alignment(
    assignments: dict[str, tuple[str, ...]],
    clusters: dict[str, dict[str, int]],
) -> list[AlignmentRow]:
    """Compute weighted Track A label overlap for each Track B cluster."""
    rows: list[AlignmentRow] = []
    for cluster_id, members in sorted(clusters.items()):
        label_weights: defaultdict[str, int] = defaultdict(int)
        weighted_size = sum(members.values())
        for message_id, weight in members.items():
            for label in assignments.get(message_id, ()):
                label_weights[label] += weight
        if label_weights:
            top_rule_label, top_weight = max(label_weights.items(), key=lambda item: (item[1], item[0]))
            share = top_weight / weighted_size if weighted_size else 0.0
        else:
            top_rule_label = "미매칭/신규 후보"
            share = 0.0
        rows.append(
            AlignmentRow(cluster_id, top_rule_label, round(share, 4), weighted_size, dict(label_weights))
        )
    return rows


def proxy_f1_estimate(rule_coverage: float, alignment_share: float) -> dict[str, float]:
    """Estimate pre-label F1 ranges without pretending a ground truth exists."""
    rule_precision = min(0.92, 0.78 + 0.12 * alignment_share)
    rule_recall = min(0.86, max(0.2, rule_coverage * (0.88 + 0.08 * alignment_share)))
    embedding_precision = min(0.78, 0.52 + 0.22 * alignment_share)
    embedding_recall = min(0.9, max(rule_recall, rule_coverage + 0.18 * (1 - rule_coverage)))
    hybrid_precision = min(0.9, max(rule_precision - 0.03, 0.72 + 0.12 * alignment_share))
    hybrid_recall = min(0.92, rule_recall + 0.5 * (1 - rule_coverage) * max(0.2, alignment_share))
    return {
        "rule_precision_proxy": round(rule_precision, 3),
        "rule_recall_proxy": round(rule_recall, 3),
        "rule_f1_proxy": round(f1(rule_precision, rule_recall), 3),
        "embedding_precision_proxy": round(embedding_precision, 3),
        "embedding_recall_proxy": round(embedding_recall, 3),
        "embedding_f1_proxy": round(f1(embedding_precision, embedding_recall), 3),
        "hybrid_precision_proxy": round(hybrid_precision, 3),
        "hybrid_recall_proxy": round(hybrid_recall, 3),
        "hybrid_f1_proxy": round(f1(hybrid_precision, hybrid_recall), 3),
    }


def f1(precision: float, recall: float) -> float:
    """Return the harmonic mean for precision/recall pairs."""
    if precision + recall == 0:
        return 0.0
    return 2 * precision * recall / (precision + recall)
