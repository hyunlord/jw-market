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

KNOWN_UNMATCHED_EVENT_BRANDS: Final = frozenset(
    {"리조덱", "염화칼륨", "오메가", "위너프A+", "트레시바", "하트만"}
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


@dataclass(frozen=True, slots=True)
class RoutedAgent2Brand:
    brand_key: str
    canonical_brand_name: str
    route: RouteDecision


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
    def __init__(self, names: tuple[str, ...]) -> None:
        super().__init__(f"event_brand_scores contains unmapped brand names: {', '.join(names)}")
        self.names = names


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
) -> DensityEvidenceBuildResult:
    """Convert event score rows into brand_key evidence counts for density routing."""

    name_to_key = build_name_to_key_map(brand_rows)
    grouped: dict[tuple[str, str, str, str | None, int], int] = defaultdict(int)
    unmatched_known: set[str] = set()
    unmatched_unknown: set[str] = set()
    for row in score_rows:
        tag = _text(row.get("tag"))
        score = _number(row.get("score"))
        if not is_score_allowed_for_density(score, tag):
            continue
        brand_name = _text(row.get("brand_canonical"))
        brand_key = name_to_key.get(brand_name)
        if brand_key is None:
            if brand_name in KNOWN_UNMATCHED_EVENT_BRANDS:
                unmatched_known.add(brand_name)
            elif brand_name:
                unmatched_unknown.add(brand_name)
            continue
        source_processor = _text(row.get("source_processor"))
        derivation = _text(row.get("derivation"))
        cutoff = cutoff_for_tag(tag)
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
    )


def route_density_worklist(
    brand_rows: list[dict[str, Any]],
    score_rows: list[dict[str, Any]],
) -> DensityWorklist:
    """Build a brand_key-routed Agent2 worklist from mart and score rows."""

    identities = build_brand_identities(brand_rows)
    evidence = build_evidence_counts_from_rows(brand_rows, score_rows)
    if evidence.unmatched_unknown:
        raise UnknownEventBrandError(evidence.unmatched_unknown)
    routes = route_worklist(tuple(identity.brand_key for identity in identities), evidence.counts)
    display_names = {identity.brand_key: identity.canonical_brand_name for identity in identities}
    return DensityWorklist(
        routed=tuple(
            RoutedAgent2Brand(
                brand_key=route.brand,
                canonical_brand_name=display_names[route.brand],
                route=route,
            )
            for route in routes
        ),
        evidence=evidence,
    )


def load_density_worklist(db_conn: Any) -> DensityWorklist:
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
    return route_density_worklist(brand_rows, score_rows)


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
