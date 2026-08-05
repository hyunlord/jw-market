from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
import hashlib
import json
import re
from urllib.parse import urlparse

from jw_chat_agent_poc.orchestrator.source_grading import (
    grade_web_url,
    is_official_web_url,
    official_web_domains,
)
from jw_chat_agent_poc.tool_use.v3_execution_contracts import (
    MarketMetricFact,
    ToolExecutionRecord,
    V3EvidenceBundle,
    WebSourceFact,
)
from jw_chat_agent_poc.tool_use.v3_fusion_evidence import (
    fact_numeric_literals,
    web_source_numeric_literals,
)
from jw_chat_agent_poc.tool_use.v3_fusion_limitations import failure_reason_code


_REQUEST_ENDING = re.compile(r"(?:알려줘|보여줘|검색해줘|찾아줘|뭐\s*있어)\??$")
_WEB_CONTEXT = re.compile(r"뉴스|최근\s*이슈|웹\s*검색|검색해|동향")
_MAX_WEB_FACTS = 3
_BLOCKED_TOOL_PREFIXES = ("market.", "file.", "hira_disease")
_BLOCKED_FAILURE_CODES = frozenset(
    {
        "unknown_brand",
        "invalid_market_label",
        "unsupported_source",
        "ambiguous_family",
        "ambiguous_market",
        "no_anchor",
        "general_composite_unavailable",
        "general_metric_unavailable",
    }
)
_MARKET_METRIC_TERMS = {
    "sales": ("sales", "매출"),
    "share": ("share", "점유율"),
    "rank": ("rank", "순위"),
    "hhi": ("hhi", "집중도"),
    "market_size": ("market size", "시장 규모", "시장규모"),
}


@dataclass(frozen=True, slots=True)
class WebSearchResult:
    provider: str
    query: str
    items: tuple[Mapping[str, object], ...]
    latency_ms: float
    status: str = "ok"
    error: str | None = None


@dataclass(frozen=True, slots=True)
class WebSearchLogEntry:
    stage: str
    source_domain: str | None
    provider: str
    query: str
    items: tuple[Mapping[str, object], ...]
    latency_ms: float
    status: str
    error: str | None
    fetched_at_utc: str


@dataclass(frozen=True, slots=True)
class WebAugmentationEligibility:
    eligible: bool
    reason: str
    source_domain: str | None
    topic: str


@dataclass(frozen=True, slots=True)
class V3WebAugmentationResult:
    bundle: V3EvidenceBundle
    eligibility: WebAugmentationEligibility
    search_log: tuple[WebSearchLogEntry, ...]
    expanded_to_general: bool


WebSearch = Callable[..., WebSearchResult]


class V3WebAugmenter:
    """Add read-only web evidence to a V3 bundle without serving it."""

    def __init__(
        self,
        *,
        search: WebSearch,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._search = search
        self._now = now

    def augment(self, question: str, bundle: V3EvidenceBundle) -> V3WebAugmentationResult:
        eligibility = web_augmentation_eligibility(question, bundle)
        if not eligibility.eligible:
            return V3WebAugmentationResult(bundle, eligibility, (), False)

        fetched_at = _utc_text(self._now())
        logs: list[WebSearchLogEntry] = []
        results: tuple[Mapping[str, object], ...] = ()
        expanded = False
        domains = (
            official_web_domains(eligibility.source_domain)
            if eligibility.source_domain
            else ()
        )
        if domains:
            official_query = rewrite_web_query(
                question,
                source_domain=eligibility.source_domain,
                official=True,
            )
            official = self._search(official_query, topic=eligibility.topic)
            logs.append(_log_entry("official", eligibility.source_domain, official, fetched_at))
            results = tuple(
                item
                for item in official.items
                if is_official_web_url(
                    str(item.get("url") or ""),
                    source_domain=eligibility.source_domain,
                )
            )

        if not results:
            expanded = bool(domains)
            general_query = rewrite_web_query(
                question,
                source_domain=eligibility.source_domain,
                official=False,
            )
            general = self._search(general_query, topic=eligibility.topic)
            logs.append(_log_entry("general", eligibility.source_domain, general, fetched_at))
            results = tuple(general.items)

        projected_facts = tuple(
            _web_fact(
                item,
                rank=rank,
                query=logs[-1].query,
                stage=logs[-1].stage,
                fetched_at_utc=fetched_at,
            )
            for rank, item in enumerate(results[:_MAX_WEB_FACTS], start=1)
            if _usable_item(item)
        )
        facts = tuple(
            replace(
                fact,
                conflicts_with_evidence_ids=_conflicting_internal_ids(
                    fact,
                    bundle,
                ),
            )
            for fact in projected_facts
        )
        executions = tuple(
            ToolExecutionRecord(
                tool_name="web_search",
                arguments={"query": entry.query, "topic": eligibility.topic},
                raw_result={
                    "provider": entry.provider,
                    "query": entry.query,
                    "items": [dict(item) for item in entry.items],
                    "status": entry.status,
                    "error": entry.error,
                    "fetched_at_utc": entry.fetched_at_utc,
                    "search_stage": entry.stage,
                },
                latency_ms=entry.latency_ms,
            )
            for entry in logs
        )
        augmented = replace(
            bundle,
            status=("partial" if bundle.failures else "complete") if facts else bundle.status,
            facts=(*bundle.facts, *facts),
            executions=(*bundle.executions, *executions),
            original_call_count=bundle.original_call_count + len(logs),
            executed_call_count=bundle.executed_call_count + len(logs),
        )
        return V3WebAugmentationResult(augmented, eligibility, tuple(logs), expanded)


def web_augmentation_eligibility(
    question: str,
    bundle: V3EvidenceBundle,
) -> WebAugmentationEligibility:
    for failure in bundle.failures:
        code = failure_reason_code(failure)
        if code in _BLOCKED_FAILURE_CODES:
            return WebAugmentationEligibility(False, f"blocked_{code}", None, "general")
        if failure.tool_name.startswith(_BLOCKED_TOOL_PREFIXES):
            return WebAugmentationEligibility(
                False,
                f"blocked_tool_{failure.tool_name}",
                None,
                "general",
            )

    tools = {failure.tool_name for failure in bundle.failures}
    if "hira_reimbursement_criteria" in tools:
        return WebAugmentationEligibility(True, "hira_evidence_gap", "hira", "general")
    if any(tool.startswith("mfds_") for tool in tools):
        return WebAugmentationEligibility(
            True,
            "regulatory_evidence_gap",
            "regulatory",
            "general",
        )
    if any(tool.startswith("clinicaltrials_") for tool in tools):
        return WebAugmentationEligibility(
            True,
            "clinical_evidence_gap",
            "clinical_trials",
            "general",
        )
    if "web_search" in tools or _WEB_CONTEXT.search(question):
        topic = "news" if re.search(r"뉴스|최근\s*이슈", question) else "general"
        return WebAugmentationEligibility(True, "explicit_web_context", None, topic)
    return WebAugmentationEligibility(False, "not_web_resolvable", None, "general")


def rewrite_web_query(
    question: str,
    *,
    source_domain: str | None,
    official: bool,
) -> str:
    normalized = " ".join(question.split())
    normalized = _REQUEST_ENDING.sub("", normalized).strip()
    if re.search(r"급여|보험", normalized):
        normalized = re.sub(r"급여\s*기준", "보험급여 인정기준 고시", normalized)
    elif re.search(r"허가", normalized):
        normalized = f"{normalized} 의약품 허가정보"
    elif re.search(r"뉴스|이슈", normalized):
        normalized = f"{normalized} 제약 뉴스"
    if official and source_domain:
        domains = official_web_domains(source_domain)
        clause = " OR ".join(f"site:{domain}" for domain in domains)
        if clause:
            return f"{normalized} ({clause})"
    return normalized


def _web_fact(
    item: Mapping[str, object],
    *,
    rank: int,
    query: str,
    stage: str,
    fetched_at_utc: str,
) -> WebSourceFact:
    url = str(item.get("url") or "").strip()
    title = str(item.get("title") or "").strip()
    excerpt = str(item.get("snippet") or item.get("content") or "").strip()
    identity = json.dumps(
        {"query": query, "rank": rank, "url": url, "title": title, "excerpt": excerpt},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    evidence_id = f"v3-shadow:web_search:{hashlib.sha256(identity.encode()).hexdigest()[:16]}"
    domain = (urlparse(url).hostname or "").lower().rstrip(".")
    return WebSourceFact(
        evidence_id=evidence_id,
        tool_name="web_search",
        arguments={"query": query},
        raw_result=dict(item),
        missing_required_fields=(),
        url=url,
        title=title,
        excerpt=excerpt,
        fetched_at_utc=fetched_at_utc,
        domain=domain,
        search_query=query,
        result_rank=rank,
        source_grade=grade_web_url(url).value,
        search_stage=stage,
    )


def _usable_item(item: Mapping[str, object]) -> bool:
    url = str(item.get("url") or "").strip()
    title = str(item.get("title") or "").strip()
    excerpt = str(item.get("snippet") or item.get("content") or "").strip()
    return url.startswith("https://") and bool(title) and bool(excerpt)


def _conflicting_internal_ids(
    web_fact: WebSourceFact,
    bundle: V3EvidenceBundle,
) -> tuple[str, ...]:
    web_text = f"{web_fact.title} {web_fact.excerpt}".casefold()
    web_numbers = web_source_numeric_literals(web_fact)
    conflicts: list[str] = []
    for fact in bundle.facts:
        if not isinstance(fact, MarketMetricFact):
            continue
        entity = (fact.entity or "").strip().casefold()
        metric = (fact.metric or "").strip().casefold()
        if not metric:
            continue
        terms = _MARKET_METRIC_TERMS.get(
            metric,
            (metric,),
        )
        if not entity or entity not in web_text:
            continue
        if not any(term and term in web_text for term in terms):
            continue
        internal_numbers = fact_numeric_literals(fact)
        if (
            internal_numbers
            and web_numbers
            and web_numbers.difference(internal_numbers)
            and internal_numbers.difference(web_numbers)
        ):
            conflicts.append(fact.evidence_id)
    return tuple(conflicts)


def _log_entry(
    stage: str,
    source_domain: str | None,
    result: WebSearchResult,
    fetched_at_utc: str,
) -> WebSearchLogEntry:
    return WebSearchLogEntry(
        stage=stage,
        source_domain=source_domain,
        provider=result.provider,
        query=result.query,
        items=result.items,
        latency_ms=result.latency_ms,
        status=result.status,
        error=result.error,
        fetched_at_utc=fetched_at_utc,
    )


def _utc_text(value: datetime) -> str:
    normalized = value.astimezone(UTC)
    return normalized.isoformat(timespec="seconds").replace("+00:00", "Z")


__all__ = [
    "V3WebAugmentationResult",
    "V3WebAugmenter",
    "WebAugmentationEligibility",
    "WebSearchLogEntry",
    "WebSearchResult",
    "rewrite_web_query",
    "web_augmentation_eligibility",
]
