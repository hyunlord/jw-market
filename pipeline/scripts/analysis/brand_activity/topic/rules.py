"""Semi-automatic market dictionary and deterministic matching logic."""

from __future__ import annotations

from collections import defaultdict
import re

from .models import MarketRuleStats, MessageRecord, RuleAssignment, RuleMatchResult, TopicRule


def build_seed_rules() -> list[TopicRule]:
    """Return conservative market-specific seed rules for Track A."""
    seeds = {
        "C10C0": [
            ("당뇨 안전성/NODM", ("NODM", "당뇨", "혈당", "신규 당뇨")),
            ("LDL-C 강하", ("LDL-C", "LDL", "강하", "조절")),
            ("복합제 장점", ("복합제", "병용", "dual", "combination")),
            ("부작용/상호작용 감소", ("부작용", "근육", "간수치", "상호작용")),
            ("심혈관 예방", ("심혈관", "ASCVD", "CV", "예방")),
            ("TG 감소", ("TG", "중성지방", "triglyceride")),
            ("대사증후군", ("대사증후군", "metabolic")),
            ("임상근거", ("임상", "study", "data", "근거", "논문")),
        ],
        "C10A1": [
            ("LDL-C 강하", ("LDL-C", "LDL", "강하", "조절")),
            ("당뇨 안전성/NODM", ("NODM", "당뇨", "혈당")),
            ("부작용/내약성", ("부작용", "근육", "간수치", "내약성")),
            ("심혈관 예방", ("심혈관", "ASCVD", "CV", "예방")),
            ("임상근거", ("임상", "study", "data", "근거")),
        ],
        "A02B2": [
            ("야간 위산/증상 조절", ("야간", "위산", "산분비", "heartburn", "증상")),
            ("역류성식도염/GERD", ("역류성식도염", "GERD", "reflux", "미란")),
            ("P-CAB/PPI 비교", ("P-CAB", "PCAB", "PPI", "케이캡", "테고", "비교")),
            ("복용 편의/식전식후", ("식전", "식후", "복용", "편의", "on-demand")),
            ("안전성/상호작용", ("안전", "상호작용", "부작용", "간장애")),
            ("임상근거", ("임상", "study", "data", "근거")),
        ],
        "A10N1": [
            ("혈당/HbA1c 조절", ("혈당", "HbA1c", "당화혈색소", "조절")),
            ("저혈당 안전성", ("저혈당", "hypoglycemia", "안전")),
            ("신기능/용량", ("신기능", "신장", "CKD", "용량")),
            ("체중/대사 영향", ("체중", "대사", "비만")),
            ("복약/병용 편의", ("복약", "병용", "순응도", "편의")),
            ("임상근거", ("임상", "study", "data", "근거")),
        ],
        "G04C2": [
            ("배뇨증상/IPSS", ("배뇨", "IPSS", "잔뇨", "빈뇨", "야간뇨")),
            ("전립선/BPH", ("전립선", "BPH", "비대")),
            ("성기능/부작용", ("성기능", "사정", "부작용", "어지러움")),
            ("복용 편의", ("복용", "편의", "하루", "순응도")),
            ("임상근거", ("임상", "study", "data", "근거")),
        ],
    }
    rules: list[TopicRule] = []
    for market, labels in seeds.items():
        for label, keywords in labels:
            source = "xenon_seed" if market.startswith("C10") else "market_seed"
            rules.append(TopicRule(market, label, tuple(keywords), source))
    return rules


def keyword_hits(message: str, keywords: tuple[str, ...]) -> tuple[str, ...]:
    """Return keywords found in a message using case-insensitive boundaries."""
    found: list[str] = []
    haystack = message.upper()
    for keyword in keywords:
        needle = keyword.upper()
        if re.search(rf"(?<![A-Z0-9가-힣]){re.escape(needle)}(?![A-Z0-9가-힣])", haystack) or needle in haystack:
            found.append(keyword)
    return tuple(found)


def match_rules(rows: list[MessageRecord], rules: list[TopicRule]) -> RuleMatchResult:
    """Assign deterministic market-specific labels and summarize coverage."""
    by_market: defaultdict[str, list[TopicRule]] = defaultdict(list)
    for rule in rules:
        by_market[rule.market].append(rule)
    assignments: dict[str, RuleAssignment] = {}
    stats: dict[str, MarketRuleStats] = defaultdict(lambda: MarketRuleStats(""))
    label_counts: defaultdict[str, defaultdict[str, int]] = defaultdict(lambda: defaultdict(int))
    for row in rows:
        market_stats = stats.setdefault(row.market, MarketRuleStats(row.market))
        market_stats.total_rows += 1
        market_stats.total_weight += row.frequency
        labels: list[str] = []
        matched_keywords: list[str] = []
        for rule in by_market.get(row.market, []):
            hits = keyword_hits(row.message_text, rule.keywords)
            if hits:
                labels.append(rule.label)
                matched_keywords.extend(hits)
                label_counts[row.market][rule.label] += row.frequency
        if labels:
            market_stats.matched_rows += 1
            market_stats.matched_weight += row.frequency
        else:
            market_stats.unmatched_rows += 1
            market_stats.unmatched_weight += row.frequency
        if len(labels) > 1:
            market_stats.multilabel_rows += 1
        assignments[row.message_id] = RuleAssignment(
            row.message_id,
            row.market,
            tuple(labels),
            tuple(dict.fromkeys(matched_keywords)),
            row.frequency,
        )
    return RuleMatchResult(assignments, dict(stats), {m: dict(c) for m, c in label_counts.items()})
