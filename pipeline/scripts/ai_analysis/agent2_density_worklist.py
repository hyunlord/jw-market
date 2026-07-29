from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys
from typing import Any

from bundle_builder.agent2_density_router import (
    BrandedScoreRow,
    RouteDecision,
    route_worklist,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from pipeline.etl.io.mart.agent2_eligibility import (
    Agent2ScoreRow,
    is_agent2_eligible,
)
from pipeline.scripts.agent3.brand_identity import (
    canonical_brand_names_from_rows,
    latest_sales_by_brand_key_from_rows,
)
from pipeline.scripts.ai_analysis.agent2_brand_registry import (
    Agent2BrandRegistry,
    UnknownAgent2BrandAliasError,
    event_brand_source_names,
)


@dataclass(frozen=True, slots=True)
class Agent2BrandIdentity:
    brand_key: str
    canonical_brand_name: str
    latest_sales: float = 0.0


@dataclass(frozen=True, slots=True)
class CentralEvidenceBuildResult:
    score_rows: tuple[BrandedScoreRow, ...]
    excluded_registered: tuple[str, ...]
    unmatched_known: tuple[str, ...]
    unmatched_unknown: tuple[str, ...]
    registry_revision: str


@dataclass(frozen=True, slots=True)
class RoutedAgent2Brand:
    brand_key: str
    canonical_brand_name: str
    route: RouteDecision
    requested_ml_id: str | None = None
    requested_strategy_id: str | None = None

    @property
    def work_item_key(self) -> str:
        market_id = self.requested_strategy_id or self.requested_ml_id
        return f"{self.brand_key}::{market_id}" if market_id else self.brand_key


@dataclass(frozen=True, slots=True)
class DensityWorklist:
    routed: tuple[RoutedAgent2Brand, ...]
    evidence: CentralEvidenceBuildResult


class BrandNameKeyCollisionError(RuntimeError):
    def __init__(self, brand_name: str, first_key: str, second_key: str) -> None:
        super().__init__(
            f"brand_name maps to multiple brand_key values: "
            f"{brand_name}={first_key},{second_key}"
        )
        self.brand_name = brand_name
        self.first_key = first_key
        self.second_key = second_key


class UnknownEventBrandError(RuntimeError):
    def __init__(self, names: tuple[str, ...]) -> None:
        super().__init__(
            f"event_brand_scores contains unmapped brand names: {', '.join(names)}"
        )
        self.names = names


class MissingBrandMarketMembershipError(RuntimeError):
    def __init__(self, brand_keys: tuple[str, ...]) -> None:
        super().__init__(
            "strategic worklist brands have no active catalog membership: "
            + ", ".join(brand_keys)
        )
        self.brand_keys = brand_keys


def build_brand_identities(
    rows: list[dict[str, Any]],
) -> tuple[Agent2BrandIdentity, ...]:
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
    """Create the canonical event-brand name to brand_key map."""

    result: dict[str, str] = {}
    for row in rows:
        brand_key = _text(row.get("brand_key"))
        brand_name = _text(row.get("brand_name"))
        if not brand_key or not brand_name:
            continue
        existing = result.get(brand_name)
        if existing is not None and existing != brand_key:
            raise BrandNameKeyCollisionError(brand_name, existing, brand_key)
        result[brand_name] = brand_key
    return result


def build_central_evidence_from_rows(
    brand_rows: list[dict[str, Any]],
    score_rows: list[dict[str, Any]],
) -> CentralEvidenceBuildResult:
    """Resolve aliases and retain only central-eligible score rows."""

    name_to_key = build_name_to_key_map(brand_rows)
    registry = Agent2BrandRegistry.for_canonical_names(name_to_key)
    accepted: list[BrandedScoreRow] = []
    excluded: set[str] = set()
    unknown: set[str] = set()
    for row in score_rows:
        score = Agent2ScoreRow(
            news_id=_text(row.get("news_id")),
            source_processor=_optional_text(row.get("source_processor")),
            derivation=_optional_text(row.get("derivation")),
            tag=_optional_text(row.get("tag")),
            score=row.get("score") if row.get("score") is not None else 0,
            published_date=row.get("published_date"),
            news_exists=row.get("joined_news_id") is not None,
        )
        if not is_agent2_eligible(score):
            continue
        source_names = event_brand_source_names(row)
        if not source_names:
            continue
        target_keys: set[str] = set()
        for source_name in source_names:
            try:
                canonical_name = registry.resolve(source_name)
            except UnknownAgent2BrandAliasError:
                unknown.add(source_name)
                continue
            if canonical_name is None:
                excluded.add(source_name)
                continue
            target_keys.add(name_to_key[canonical_name])
        for brand_key in sorted(target_keys):
            accepted.append(
                BrandedScoreRow(
                    brand_key=brand_key,
                    score=score,
                )
            )
        if not target_keys:
            continue
    if unknown:
        raise UnknownEventBrandError(tuple(sorted(unknown)))
    exclusions = tuple(sorted(excluded))
    return CentralEvidenceBuildResult(
        score_rows=tuple(accepted),
        excluded_registered=exclusions,
        unmatched_known=exclusions,
        unmatched_unknown=(),
        registry_revision=registry.revision,
    )


def route_density_worklist(
    brand_rows: list[dict[str, Any]],
    score_rows: list[dict[str, Any]],
) -> DensityWorklist:
    """Build a central-eligible, brand_key-routed Agent2 worklist."""

    identities = build_brand_identities(brand_rows)
    evidence = build_central_evidence_from_rows(brand_rows, score_rows)
    routes = route_worklist(
        tuple(identity.brand_key for identity in identities),
        evidence.score_rows,
    )
    display_names = {
        identity.brand_key: identity.canonical_brand_name
        for identity in identities
    }
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


def expand_market_scoped_worklist(
    routed: tuple[RoutedAgent2Brand, ...],
    membership_rows: list[dict[str, Any]],
) -> tuple[RoutedAgent2Brand, ...]:
    """Expand routed brands by their authoritative strategic memberships."""

    memberships: dict[str, set[tuple[str, str]]] = {}
    for row in membership_rows:
        brand_key = _text(row.get("general_brand_key"))
        ml_id = _text(row.get("ml_id"))
        strategy_id = _text(row.get("strategy_id"))
        if not brand_key or not ml_id or not strategy_id:
            continue
        memberships.setdefault(brand_key, set()).add((ml_id, strategy_id))

    expanded: list[RoutedAgent2Brand] = []
    missing: list[str] = []
    for item in routed:
        scoped = sorted(memberships.get(item.brand_key, ()))
        if not scoped:
            missing.append(item.brand_key)
            continue
        expanded.extend(
            RoutedAgent2Brand(
                brand_key=item.brand_key,
                canonical_brand_name=item.canonical_brand_name,
                route=item.route,
                requested_ml_id=ml_id,
                requested_strategy_id=strategy_id,
            )
            for ml_id, strategy_id in scoped
        )
    if missing:
        raise MissingBrandMarketMembershipError(tuple(sorted(set(missing))))
    return tuple(expanded)


def load_density_worklist(db_conn: Any, *, market_scoped: bool = False) -> DensityWorklist:
    """Load the mart universe and joined score rows required by central policy."""

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
        SELECT s.news_id, s.brand_canonical, s.brand_name,
               s.mirrored_from_jw_brands, s.source_processor, s.derivation,
               s.tag, s.score, n.news_id AS joined_news_id, n.published_date
        FROM event_brand_scores s
        LEFT JOIN news_raw n ON s.news_id = n.news_id
        """,
    )
    worklist = route_density_worklist(brand_rows, score_rows)
    if not market_scoped:
        return worklist

    membership_rows = _fetch_all(
        db_conn,
        """
        SELECT general_brand_key, ml_id, strategy_id
        FROM catalog_strategic_brand
        WHERE COALESCE(is_excluded, 0) = 0
          AND general_brand_key IS NOT NULL AND general_brand_key <> ''
          AND ml_id IS NOT NULL AND ml_id <> ''
          AND strategy_id IS NOT NULL AND strategy_id <> ''
        ORDER BY general_brand_key, ml_id, strategy_id
        """,
    )
    return DensityWorklist(
        routed=expand_market_scoped_worklist(worklist.routed, membership_rows),
        evidence=worklist.evidence,
    )


def _fetch_all(db_conn: Any, sql: str) -> list[dict[str, Any]]:
    cursor = db_conn.cursor()
    cursor.execute(sql)
    return list(cursor.fetchall())


def _text(value: Any) -> str:
    return str(value or "").strip()


def _optional_text(value: Any) -> str | None:
    cleaned = _text(value)
    return cleaned or None
