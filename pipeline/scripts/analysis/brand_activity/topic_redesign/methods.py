"""Candidate extraction methods and comparable scoring for topic redesign."""

from __future__ import annotations

from collections import Counter, defaultdict
import math

import numpy as np

from .models import MethodScore, MessageRow
from .text import contains_keyword, is_noisy_candidate, ngram_counts, pmi_collocations, redundancy_rate, token_counts, tokenize


def score_extraction_methods(rows: list[MessageRow], sample_markets: tuple[str, ...]) -> list[MethodScore]:
    """Run all comparison methods on sample markets and return measured scores."""
    scores: list[MethodScore] = []
    for market in sample_markets:
        market_rows = [row for row in rows if row.market == market]
        method_terms = {
            "빈출 토큰": _top_tokens(market_rows),
            "n-gram 빈출": _top_ngrams(market_rows),
            "PMI 연어": _top_collocations(market_rows),
            "TF-IDF/SVD 군집": _cluster_terms(market_rows),
            "c-TF-IDF 대체": _ctfidf_terms(rows, market),
        }
        for method, terms in method_terms.items():
            scores.append(_score_terms(market, method, market_rows, terms))
    return scores


def _top_tokens(rows: list[MessageRow]) -> tuple[str, ...]:
    """Extract high-frequency single-token candidates."""
    return tuple(term for term, _ in token_counts(row.text for row in rows).most_common(20) if not is_noisy_candidate(term))[:12]


def _top_ngrams(rows: list[MessageRow]) -> tuple[str, ...]:
    """Extract frequent bigram/trigram phrase candidates."""
    bigrams = ngram_counts((row.text for row in rows), 2)
    trigrams = ngram_counts((row.text for row in rows), 3)
    combined = Counter[str]()
    combined.update(bigrams)
    combined.update({term: count for term, count in trigrams.items() if count >= 2})
    return tuple(term for term, _ in combined.most_common(20) if not is_noisy_candidate(term))[:12]


def _top_collocations(rows: list[MessageRow]) -> tuple[str, ...]:
    """Extract PMI-ranked collocation candidates with support gating."""
    min_count = max(2, min(10, len(rows) // 100))
    return tuple(term for term, _, _ in pmi_collocations((row.text for row in rows), min_count)[:12])


def _ctfidf_terms(rows: list[MessageRow], market: str) -> tuple[str, ...]:
    """Extract market-specific terms using a c-TF-IDF style local statistic."""
    market_term_counts: defaultdict[str, Counter[str]] = defaultdict(Counter)
    term_markets: defaultdict[str, set[str]] = defaultdict(set)
    for row in rows:
        row_tokens = tokenize(row.text)
        market_term_counts[row.market].update(row_tokens)
        for token in set(row_tokens):
            term_markets[token].add(row.market)
    total_markets = len(market_term_counts) or 1
    scored: list[tuple[str, float]] = []
    for term, count in market_term_counts[market].items():
        if is_noisy_candidate(term):
            continue
        inverse_market_frequency = math.log((1 + total_markets) / (1 + len(term_markets[term]))) + 1
        scored.append((term, count * inverse_market_frequency))
    return tuple(term for term, _ in sorted(scored, key=lambda item: (-item[1], item[0]))[:12])


def _cluster_terms(rows: list[MessageRow]) -> tuple[str, ...]:
    """Extract cluster-representative terms with local TF-IDF/SVD/KMeans."""
    if len(rows) < 20:
        return _top_ngrams(rows)[:8]
    try:
        from sklearn.cluster import KMeans
        from sklearn.decomposition import TruncatedSVD
        from sklearn.feature_extraction.text import TfidfVectorizer
    except ImportError:
        return _top_ngrams(rows)[:8]
    texts = [row.text for row in rows]
    vectorizer = TfidfVectorizer(tokenizer=tokenize, token_pattern=None, lowercase=False, max_features=1600)
    matrix = vectorizer.fit_transform(texts)
    if matrix.shape[0] < 3 or matrix.shape[1] < 3:
        return _top_ngrams(rows)[:8]
    cluster_count = min(8, max(2, round(math.sqrt(len(rows)) / 2)), matrix.shape[0] - 1)
    component_count = min(40, matrix.shape[1] - 1, matrix.shape[0] - 1)
    reduced = TruncatedSVD(n_components=max(2, component_count), random_state=42).fit_transform(matrix)
    labels = KMeans(n_clusters=cluster_count, random_state=42, n_init=10).fit_predict(reduced)
    feature_names = vectorizer.get_feature_names_out()
    terms: list[str] = []
    for cluster_id in range(cluster_count):
        member_index = np.where(labels == cluster_id)[0]
        if len(member_index) == 0:
            continue
        weights = np.asarray(matrix[member_index].mean(axis=0)).ravel()
        for term_index in weights.argsort()[::-1][:3]:
            term = str(feature_names[term_index])
            if term not in terms and not is_noisy_candidate(term):
                terms.append(term)
    return tuple(terms[:12])


def _score_terms(market: str, method: str, rows: list[MessageRow], terms: tuple[str, ...]) -> MethodScore:
    """Convert extracted candidates into comparable coverage/noise/redundancy scores."""
    if not rows:
        return MethodScore(market, method, 0, 0.0, 1.0, 0.0, 0.0, (), "no rows")
    matched = sum(1 for row in rows if any(contains_keyword(row.text, term) for term in terms))
    noise = sum(1 for term in terms if is_noisy_candidate(term)) / len(terms) if terms else 1.0
    redundancy = redundancy_rate(terms)
    coverage = matched / len(rows)
    score = (coverage * 0.55) + ((1 - noise) * 0.25) + ((1 - redundancy) * 0.20)
    note = _method_note(method)
    return MethodScore(market, method, len(terms), coverage, noise, redundancy, round(score, 4), terms[:8], note)


def _method_note(method: str) -> str:
    """Explain the operational fit of each extraction method."""
    notes = {
        "빈출 토큰": "커버리지는 높지만 단일 토큰 노이즈가 많아 라벨명 확정에는 약함",
        "n-gram 빈출": "비즈니스 문구 후보가 선명하고 사람이 검토하기 쉬움",
        "PMI 연어": "빈도는 낮아도 결합 의미가 강한 누락 후보 발굴에 좋음",
        "TF-IDF/SVD 군집": "미분류 잔여를 묶어 신규 후보를 찾는 discovery 보조",
        "c-TF-IDF 대체": "시장 고유어를 잘 올리지만 구 단위 맥락은 약함",
    }
    return notes[method]

