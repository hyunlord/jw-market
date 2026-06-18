"""Strategy mart fact collection and raw-identity deduplication."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import json
from typing import Any

from pipeline.scripts.api.dynamic_market.types import quote_identifier
from pipeline.scripts.api.market_id import to_ml_id
from pipeline.scripts.api.market_scope.types import DEDUP_KEY_VERSION, DedupDiagnostics


class FactIdentityIncompleteError(Exception):
    """Raised when a union scope cannot prove raw fact identity."""

    def __init__(self, message: str) -> None:
        """Store a stable dedup-gate failure message."""

        self.message = message
        super().__init__(message)

    def __str__(self) -> str:
        """Return the stored dedup-gate failure message."""

        return self.message


@dataclass(frozen=True, slots=True)
class StrategyFact:
    """One raw-identifiable strategy fact used for union recomputation."""

    market_id: str
    raw_fact_id: str | None
    brand_key: str
    brand_name: str
    company: str
    source: str
    measure: str
    unit_label: str
    raw_value_history: dict[str, float]

    def dedup_key(self) -> tuple[str, str, str]:
        """Return the raw fact identity key, independent of market id."""

        if not self.raw_fact_id:
            raise FactIdentityIncompleteError(
                "strategy union requires raw fact identity; collapsed mart rows are not safe for overlap dedup"
            )
        return (self.source, self.measure, self.raw_fact_id)


def deduplicate_facts(facts: Sequence[StrategyFact]) -> tuple[tuple[StrategyFact, ...], DedupDiagnostics]:
    """Drop duplicate raw facts and return contract diagnostics.

    The key deliberately excludes ``market_id`` because group/source selections
    may include the same raw fact through more than one strategy market.
    """

    seen: set[tuple[str, str, str]] = set()
    deduped: list[StrategyFact] = []
    for fact in facts:
        key = fact.dedup_key()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(fact)
    diagnostics = DedupDiagnostics(
        dedup_strategy="raw_fact_identity_v1",
        dedup_key_version=DEDUP_KEY_VERSION,
        candidate_fact_count=len(facts),
        deduped_fact_count=len(deduped),
        dropped_duplicate_count=len(facts) - len(deduped),
    )
    return tuple(deduped), diagnostics


def collect_strategy_facts_from_rows(rows: Sequence[dict[str, Any]], *, market_id_field: str = "ml_id") -> tuple[StrategyFact, ...]:
    """Convert mart-like rows to facts.

    Current S5 strategic mart rows do not carry raw fact ids.  This converter
    preserves that absence instead of fabricating ids, so the dedup gate fails
    before unsafe group recomputation.
    """

    facts: list[StrategyFact] = []
    for row in rows:
        market_id = _strategy_market_id(str(row.get(market_id_field) or row.get("market_id") or ""))
        facts.append(
            StrategyFact(
                market_id=market_id,
                raw_fact_id=_raw_fact_id(row),
                brand_key=str(row.get("brand_key") or row.get("brand_id") or ""),
                brand_name=str(row.get("brand_name") or row.get("brand_key") or ""),
                company=_company(row),
                source=str(row.get("source") or ""),
                measure=str(row.get("measure") or ""),
                unit_label=str(row.get("unit_label") or ""),
                raw_value_history=_history(row.get("raw_value_history")),
            )
        )
    return tuple(facts)


def collect_strategy_facts_from_mart(
    fetch_all: Callable[[str, Sequence[Any]], list[dict[str, Any]]],
    *,
    mart_db: str,
    source_markets: tuple[str, ...],
    source: str,
    measure: str,
) -> tuple[StrategyFact, ...]:
    """Read candidate strategy rows with a caller-provided DB function.

    The collector is read-only.  It intentionally returns rows with missing
    raw ids when the mart lacks them; ``deduplicate_facts`` is the hard gate.
    """

    ml_ids = tuple(to_ml_id(market_id) for market_id in source_markets)
    if not ml_ids:
        return ()
    mart_source = _mart_source(source)
    mart_db_identifier = quote_identifier(mart_db)
    placeholders = ", ".join(["%s"] * len(ml_ids))
    sql = f"""
        SELECT ml_id, brand_id, brand_key, brand_name, source, measure, unit_label,
               raw_value_history, by_dimension, overlay_data, payload
        FROM {mart_db_identifier}.mart_strategic_ml_brand_metric
        WHERE ml_id IN ({placeholders}) AND source = %s AND measure = %s
        ORDER BY ml_id, brand_key, brand_id
    """
    return collect_strategy_facts_from_rows(fetch_all(sql, (*ml_ids, mart_source, measure)))


def _history(value: Any) -> dict[str, float]:
    """Normalize a history JSON object to ``period -> float``."""

    payload = json.loads(value) if isinstance(value, str) else (value or {})
    return {str(period): float(amount or 0.0) for period, amount in payload.items()}


def _raw_fact_id(row: dict[str, Any]) -> str | None:
    """Return a real raw fact id when one is present in a future mart."""

    for key in ("raw_fact_id", "source_fact_id", "fact_identity"):
        value = row.get(key)
        if value:
            return str(value)
    return None


def _company(row: dict[str, Any]) -> str:
    """Extract company/manufacturer metadata from dimension JSON columns."""

    dimension = row.get("by_dimension") or {}
    if isinstance(dimension, str):
        try:
            dimension = json.loads(dimension)
        except json.JSONDecodeError:
            dimension = {}
    return str(dimension.get("company") or dimension.get("manufacturer") or "Unknown")


def _strategy_market_id(value: str) -> str:
    """Convert mart ``ml_NNN`` ids to API strategy ids."""

    return f"strategy_{value[3:]}" if value.startswith("ml_") else value


def _mart_source(value: str) -> str:
    """Map contract source labels to mart source labels."""

    source = value.strip().upper()
    if source == "IQVIA":
        return "iqvia_nsa"
    if source == "UBIST":
        return "ubist"
    return value.strip().lower()
