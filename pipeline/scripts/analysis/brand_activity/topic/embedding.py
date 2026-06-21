from __future__ import annotations

from collections import defaultdict
import math

import numpy as np
from sklearn.cluster import Birch, KMeans
from sklearn.decomposition import TruncatedSVD
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics import silhouette_score
from sklearn.preprocessing import normalize

from .models import ClusterSummary, MessageRecord, TopicRule
from .text_tokens import token_counts


def choose_cluster_count(row_count: int, max_clusters: int = 8) -> int:
    """Choose a bounded k for market-level topic exploration."""
    if row_count < 4:
        return max(1, row_count)
    return max(2, min(max_clusters, round(math.sqrt(row_count / 2))))


def svd_embeddings(rows: list[MessageRecord], dimensions: int = 64) -> tuple[np.ndarray, list[str]]:
    """Build local TF-IDF plus SVD embeddings without external API calls."""
    texts = [row.message_text for row in rows]
    vectorizer = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 5), min_df=1, max_features=6000)
    matrix = vectorizer.fit_transform(texts)
    if min(matrix.shape) <= 2:
        dense = matrix.toarray()
    else:
        components = min(dimensions, matrix.shape[0] - 1, matrix.shape[1] - 1)
        dense = TruncatedSVD(n_components=max(2, components), random_state=42).fit_transform(matrix)
    return normalize(dense), list(vectorizer.get_feature_names_out())


def cluster_market(rows: list[MessageRecord], methods: tuple[str, ...] = ("kmeans", "birch")) -> dict[str, list[int]]:
    """Assign cluster labels with the available deterministic local methods."""
    if not rows:
        return {}
    if len(rows) == 1:
        return {method: [0] for method in methods}
    embeddings, _ = svd_embeddings(rows)
    k = choose_cluster_count(len(rows))
    weights = np.array([row.frequency for row in rows], dtype=float)
    labels_by_method: dict[str, list[int]] = {}
    if "kmeans" in methods:
        model = KMeans(n_clusters=k, random_state=42, n_init=10)
        labels_by_method["kmeans"] = list(model.fit_predict(embeddings, sample_weight=weights))
    if "birch" in methods:
        model = Birch(n_clusters=k, threshold=0.5)
        labels_by_method["birch"] = list(model.fit_predict(embeddings))
    return labels_by_method


def summarize_clusters(
    market: str,
    rows: list[MessageRecord],
    labels: list[int],
    method: str,
    rules: list[TopicRule],
    representatives: int = 3,
) -> list[ClusterSummary]:
    """Create interpretable cluster summaries with limited representative text."""
    grouped: defaultdict[int, list[MessageRecord]] = defaultdict(list)
    for row, label in zip(rows, labels, strict=True):
        grouped[int(label)].append(row)
    summaries: list[ClusterSummary] = []
    for label, members in sorted(grouped.items()):
        ordered = sorted(members, key=lambda item: (-item.frequency, item.message_hash))
        terms = tuple(term for term, _ in token_counts(member.message_text for member in members).most_common(8))
        suggested = suggest_cluster_label(terms, market, rules)
        summaries.append(
            ClusterSummary(
                market,
                method,
                f"{market}:{method}:{label}",
                len(members),
                sum(member.frequency for member in members),
                terms,
                tuple(member.message_id for member in ordered[:representatives]),
                tuple(member.message_text for member in ordered[:representatives]),
                suggested,
            )
        )
    return summaries


def suggest_cluster_label(terms: tuple[str, ...], market: str, rules: list[TopicRule]) -> str:
    """Suggest a business label using seed-rule term overlap before generic terms."""
    upper_terms = {term.upper() for term in terms}
    best_label = ""
    best_score = 0
    for rule in rules:
        if rule.market != market:
            continue
        score = sum(1 for keyword in rule.keywords if keyword.upper() in upper_terms)
        if score > best_score:
            best_label = rule.label
            best_score = score
    if best_label:
        return best_label
    return " / ".join(terms[:3]) if terms else "소형/기타 클러스터"


def cluster_silhouette(rows: list[MessageRecord], labels: list[int]) -> float | None:
    """Measure rough cluster separation when the label shape permits it."""
    if len(set(labels)) < 2 or len(set(labels)) >= len(rows):
        return None
    embeddings, _ = svd_embeddings(rows)
    return round(float(silhouette_score(embeddings, labels)), 4)


def cluster_member_weights(rows: list[MessageRecord], labels: list[int], market: str, method: str) -> dict[str, dict[str, int]]:
    """Return cluster membership weights keyed for Track A/B crosswalks."""
    clusters: defaultdict[str, dict[str, int]] = defaultdict(dict)
    for row, label in zip(rows, labels, strict=True):
        clusters[f"{market}:{method}:{label}"][row.message_id] = row.frequency
    return dict(clusters)
