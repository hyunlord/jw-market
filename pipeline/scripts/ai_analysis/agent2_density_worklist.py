from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
import sys
from typing import Any, Final

from bundle_builder.agent2_density_router import (
    EvidenceCount,
    RouteDecision,
    cutoff_for_tag,
    is_score_allowed_for_density,
    route_worklist,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pipeline.scripts.agent3.brand_identity import (
    canonical_brand_names_from_rows,
    latest_sales_by_brand_key_from_rows,
)
from pipeline.etl.io.mart.brand_alias_resolver import (
    MANUAL_BRAND_ALIASES,
    BrandAliasResolver,
)

KNOWN_UNMATCHED_EVENT_BRANDS: Final = frozenset(
    {"염화칼륨", "오메가", "트레시바", "하트만"}
)
MAX_UNKNOWN_EVENT_BRAND_RATIO: Final = 0.01
JW_BRANDS: Final = frozenset(
    {
        "가드렛", "가드메트", "뉴트로진", "라베칸", "라베칸듀오",
        "리바로", "리바로브이", "리바로젯", "리바로페노", "리바로하이",
        "모빌리아", "베노훼럼", "시그마트", "악템라", "엔커버", "위너프",
        "위너프A+", "제이다트", "제이클", "타발리스", "트루패스", "페린젝트",
        "플라주오피", "피나스타", "헴리브라",
    }
)
JW_IDENTITY_ALIASES: Final = frozenset({"위너프에이플러스"})
WEEKLY_EVENT_ALIASES: Final = {
    "종근당 자누비아": "종근당자누비아",
    "종근당자누비아": "종근당자누비아",
}
WEEKLY_ALIAS_CONTEXTS: Final = {
    "종근당자누비아": ("ml_003", "cd_003"),
}
WEEKLY_EXCLUDED_NON_JW_MARKET: Final = frozenset(
    {
        "노보믹스", "다파시타 엠", "디트루시톨", "발디핀 플러스",
        "아모잘탄 큐", "아모잘탄 플러스", "아주 세파드록실", "아주 세프라딘",
        "애피드라 주 솔로스타", "엑스원 플러스", "엔젤라 프리필드펜주",
        "유트로핀 에스 펜주", "유트로핀 펜주", "지노트로핀 고퀵 펜주",
        "콜린알포", "텔로핀 셋", "텔미누보 플러스", "텔미칸 에이 플러스",
    }
)


@dataclass(frozen=True, slots=True)
class Agent2BrandIdentity:
    brand_key: str
    canonical_brand_name: str
    latest_sales: float = 0.0


@dataclass(frozen=True, slots=True)
class DensityEvidenceBuildResult:
    counts: tuple[EvidenceCount, ...]
    unmatched_known: tuple[str, ...]
    unmatched_unknown: tuple[str, ...]
    aliases: tuple[tuple[str, str, str, str], ...] = ()
    excluded: tuple["ExcludedEventBrand", ...] = ()


@dataclass(frozen=True, slots=True)
class ExcludedEventBrand:
    brand: str
    reason: str
    source_event_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "brand": self.brand,
            "reason": self.reason,
            "source_event_count": self.source_event_count,
        }


@dataclass(frozen=True, slots=True)
class RoutedAgent2Brand:
    brand_key: str
    canonical_brand_name: str
    route: RouteDecision
    tier: int = 2
    cohort: str = "nonstrategic"
    latest_sales: float = 0.0


@dataclass(frozen=True, slots=True)
class DensityWorklist:
    routed: tuple[RoutedAgent2Brand, ...]
    evidence: DensityEvidenceBuildResult


class BrandNameKeyCollisionError(RuntimeError):
    def __init__(self, brand_name: str, first_key: str, second_key: str) -> None:
        super().__init__(f"brand_name maps to multiple brand_key values: {brand_name}={first_key},{second_key}")
        self.brand_name = brand_name
        self.first_key = first_key
        self.second_key = second_key


class UnknownEventBrandError(RuntimeError):
    def __init__(
        self,
        names: tuple[str, ...],
        *,
        total_brands: int,
        max_ratio: float = MAX_UNKNOWN_EVENT_BRAND_RATIO,
    ) -> None:
        ratio = len(names) / total_brands if total_brands else 1.0
        super().__init__(
            "event_brand_scores unmapped brand threshold exceeded: "
            f"{len(names)}/{total_brands} ({ratio:.4%}) > {max_ratio:.4%}; "
            f"names={', '.join(names)}"
        )
        self.names = names
        self.total_brands = total_brands
        self.ratio = ratio
        self.max_ratio = max_ratio


def build_brand_identities(rows: list[dict[str, Any]]) -> tuple[Agent2BrandIdentity, ...]:
    """Build brand_key identities using the Agent3 canonical-name rule."""

    canonical_names = canonical_brand_names_from_rows(rows)
    latest_sales = latest_sales_by_brand_key_from_rows(rows)
    return tuple(
        Agent2BrandIdentity(
            brand_key=brand_key,
            canonical_brand_name=canonical_names[brand_key],
            latest_sales=latest_sales.get(brand_key, 0.0),
        )
        for brand_key in sorted(canonical_names)
    )


def build_name_to_key_map(rows: list[dict[str, Any]]) -> dict[str, str]:
    """Create the event brand name to brand_key map and hard-stop on collisions."""

    result: dict[str, str] = {}
    for row in rows:
        brand_key = str(row.get("brand_key") or "").strip()
        brand_name = str(row.get("brand_name") or "").strip()
        if not brand_key or not brand_name:
            continue
        existing = result.get(brand_name)
        if existing is not None and existing != brand_key:
            raise BrandNameKeyCollisionError(brand_name, existing, brand_key)
        result[brand_name] = brand_key
    return result


def build_evidence_counts_from_rows(
    brand_rows: list[dict[str, Any]],
    score_rows: list[dict[str, Any]],
    *,
    accept_canonical_brand_keys: bool = False,
    weekly_global: bool = False,
) -> DensityEvidenceBuildResult:
    """Convert event score rows into brand_key evidence counts for density routing."""

    name_to_key = build_name_to_key_map(brand_rows)
    canonical_keys = frozenset(
        brand_key
        for row in brand_rows
        if (brand_key := _text(row.get("brand_key")))
    )
    alias_resolver = BrandAliasResolver.from_static(
        MANUAL_BRAND_ALIASES.items(),
        canonical_keys=name_to_key,
    )
    grouped: dict[tuple[str, str, str, str | None, int], int] = defaultdict(int)
    unmatched_known: set[str] = set()
    unmatched_unknown: set[str] = set()
    excluded_counts: dict[str, int] = defaultdict(int)
    aliases: set[tuple[str, str, str, str]] = set()
    for row in score_rows:
        tag = _text(row.get("tag"))
        score = _number(row.get("score"))
        source_processor = _text(row.get("source_processor"))
        if not is_score_allowed_for_density(score, tag, source_processor):
            continue
        event_brand_name = _text(row.get("brand_canonical"))
        brand_name = alias_resolver.resolve_alias(event_brand_name)
        brand_key = name_to_key.get(brand_name)
        if brand_key is None and weekly_global:
            alias_key = WEEKLY_EVENT_ALIASES.get(event_brand_name)
            if alias_key in canonical_keys:
                brand_key = alias_key
                aliases.add((event_brand_name, alias_key, "ml_003", "cd_003"))
        if (
            brand_key is None
            and accept_canonical_brand_keys
            and event_brand_name not in KNOWN_UNMATCHED_EVENT_BRANDS
            and event_brand_name in canonical_keys
        ):
            brand_key = event_brand_name
        if brand_key is None:
            if brand_name in KNOWN_UNMATCHED_EVENT_BRANDS:
                unmatched_known.add(brand_name)
            elif weekly_global and event_brand_name in WEEKLY_EXCLUDED_NON_JW_MARKET:
                excluded_counts[event_brand_name] += 1
            elif brand_name:
                unmatched_unknown.add(brand_name)
            continue
        derivation = _text(row.get("derivation"))
        cutoff = cutoff_for_tag(tag, source_processor)
        if cutoff is None:
            continue
        grouped[(brand_key, source_processor, derivation, tag, cutoff)] += 1
    counts = tuple(
        EvidenceCount(
            brand=brand_key,
            source_processor=source_processor,
            derivation=derivation,
            count=count,
            score_cutoff=cutoff,
            tag=tag,
        )
        for (brand_key, source_processor, derivation, tag, cutoff), count in sorted(grouped.items())
    )
    return DensityEvidenceBuildResult(
        counts=counts,
        unmatched_known=tuple(sorted(unmatched_known)),
        unmatched_unknown=tuple(sorted(unmatched_unknown)),
        aliases=tuple(sorted(aliases)),
        excluded=tuple(
            ExcludedEventBrand(
                brand=brand,
                reason="excluded_non_jw_market",
                source_event_count=count,
            )
            for brand, count in sorted(excluded_counts.items())
        ),
    )


def route_density_worklist(
    brand_rows: list[dict[str, Any]],
    score_rows: list[dict[str, Any]],
    *,
    accept_canonical_brand_keys: bool = False,
    weekly_global: bool = False,
    strategic_rows: list[dict[str, Any]] | None = None,
) -> DensityWorklist:
    """Build a brand_key-routed Agent2 worklist from mart and score rows."""

    effective_brand_rows = list(brand_rows)
    if weekly_global:
        existing_keys = {_text(row.get("brand_key")) for row in effective_brand_rows}
        for row in strategic_rows or []:
            brand_key = _text(row.get("general_brand_key"))
            expected_context = WEEKLY_ALIAS_CONTEXTS.get(brand_key)
            actual_context = (_text(row.get("ml_id")), _text(row.get("cd_id")))
            if expected_context != actual_context or brand_key in existing_keys:
                continue
            effective_brand_rows.append(
                {
                    "brand_key": brand_key,
                    "brand_name": _text(row.get("canonical_name")) or brand_key,
                    "raw_value_history": {},
                }
            )
            existing_keys.add(brand_key)

    identities = build_brand_identities(effective_brand_rows)
    evidence = build_evidence_counts_from_rows(
        effective_brand_rows,
        score_rows,
        accept_canonical_brand_keys=accept_canonical_brand_keys,
        weekly_global=weekly_global,
    )
    unknown_ratio = (
        len(evidence.unmatched_unknown) / len(identities)
        if identities
        else float(bool(evidence.unmatched_unknown))
    )
    if unknown_ratio > MAX_UNKNOWN_EVENT_BRAND_RATIO:
        raise UnknownEventBrandError(
            evidence.unmatched_unknown,
            total_brands=len(identities),
        )
    routes = route_worklist(tuple(identity.brand_key for identity in identities), evidence.counts)
    display_names = {identity.brand_key: identity.canonical_brand_name for identity in identities}
    identity_by_key = {identity.brand_key: identity for identity in identities}
    strategic_names = {
        _identity_token(row.get("canonical_name"))
        for row in strategic_rows or []
        if not bool(row.get("is_excluded")) and not bool(row.get("is_class_excluded"))
    }
    items = [
        RoutedAgent2Brand(
            brand_key=route.brand,
            canonical_brand_name=display_names[route.brand],
            route=route,
            tier=_tier_for_identity(identity_by_key[route.brand], strategic_names),
            cohort=_cohort_for_tier(_tier_for_identity(identity_by_key[route.brand], strategic_names)),
            latest_sales=identity_by_key[route.brand].latest_sales,
        )
        for route in routes
    ]
    if weekly_global:
        items.sort(key=lambda item: (item.tier, -item.latest_sales, item.brand_key))
    return DensityWorklist(
        routed=tuple(items),
        evidence=evidence,
    )


def load_density_worklist(
    db_conn: Any,
    *,
    accept_canonical_brand_keys: bool = False,
    weekly_global: bool = False,
) -> DensityWorklist:
    """Load the d2 mart universe and score rows, then route by brand_key."""

    brand_rows = _fetch_all(
        db_conn,
        """
        SELECT brand_key, brand_name, raw_value_history
        FROM mart_general_brand_metric
        WHERE brand_key IS NOT NULL AND brand_key <> ''
          AND brand_name IS NOT NULL AND brand_name <> ''
        """,
    )
    score_rows = _fetch_all(
        db_conn,
        """
        SELECT brand_canonical, source_processor, derivation, tag, score
        FROM event_brand_scores
        WHERE brand_canonical IS NOT NULL AND brand_canonical <> ''
        """,
    )
    strategic_rows = (
        _fetch_all(
            db_conn,
            """
            SELECT brand_id, canonical_name, is_jw, is_target,
                   is_excluded, is_class_excluded, general_brand_key, ml_id, cd_id
            FROM catalog_strategic_brand
            ORDER BY brand_id
            """,
        )
        if weekly_global
        else []
    )
    return route_density_worklist(
        brand_rows,
        score_rows,
        accept_canonical_brand_keys=accept_canonical_brand_keys,
        weekly_global=weekly_global,
        strategic_rows=strategic_rows,
    )


def _identity_token(value: Any) -> str:
    return "".join(_text(value).casefold().split())


def _tier_for_identity(
    identity: Agent2BrandIdentity,
    strategic_names: set[str],
) -> int:
    tokens = {
        _identity_token(identity.brand_key),
        _identity_token(identity.canonical_brand_name),
    }
    jw_tokens = {_identity_token(name) for name in (*JW_BRANDS, *JW_IDENTITY_ALIASES)}
    if any(token in jw_tokens for token in tokens):
        return 0
    if any(token in strategic_names for token in tokens):
        return 1
    return 2


def _cohort_for_tier(tier: int) -> str:
    return {0: "jw", 1: "strategic", 2: "nonstrategic"}[tier]


def _fetch_all(db_conn: Any, sql: str) -> list[dict[str, Any]]:
    cursor = db_conn.cursor()
    cursor.execute(sql)
    return list(cursor.fetchall())


def _text(value: Any) -> str:
    return str(value or "").strip()


def _number(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
