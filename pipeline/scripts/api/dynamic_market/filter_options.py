"""Filter option list helpers for dynamic market UIs."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from pipeline.scripts.api import db
from pipeline.scripts.api.dynamic_market.resolvers import normalize_source
from pipeline.scripts.api.dynamic_market.types import DynamicMarketInputError, quote_identifier


GENERAL_DIMENSION_TABLE = "mart_general_filter_dimension_metric"
STRATEGIC_DIMENSION_TABLE = "mart_strategic_filter_dimension_metric"
SELECTABLE_ATC_LEVELS = ("atc3", "atc4")
DIMENSION_LABELS: dict[str, str] = {
    "seller": "판매사",
    "molecule_strength": "성분용량",
    "form": "제형",
    "route": "투여경로",
    "reimbursement": "급여구분",
    "mfr": "MFR NAME KOR",
    "molecule_type": "MOLECULE TYPE",
    "strength": "STRENGTH",
    "nhi": "NHI TYPE",
}
DIMENSION_ORDER_BY_SOURCE: dict[str, tuple[str, ...]] = {
    "ubist": ("seller", "molecule_strength", "form", "route", "reimbursement"),
    "iqvia_nsa": ("mfr", "molecule_type", "strength", "nhi"),
}


@dataclass(frozen=True, slots=True)
class DimensionOptionRow:
    dimension_type: str
    dimension_value: str
    dimension_value_norm: str
    row_count: int


def build_filter_options(
    *,
    mart_db: str,
    view: str,
    source: str,
    market_id: str | None = None,
    general_dimension_db: str | None = None,
    strategic_dimension_db: str | None = None,
) -> dict[str, object]:
    normalized_view = normalize_view(view)
    normalized_source = normalize_source(source)
    dimension_db = (general_dimension_db if normalized_view == "general" else strategic_dimension_db) or mart_db
    dimensions = _load_dimension_options(
        dimension_db=dimension_db,
        view=normalized_view,
        source=normalized_source,
        market_id=market_id,
    )
    atc_rows = _load_atc_rows(mart_db=mart_db, view=normalized_view, source=normalized_source, market_id=market_id)
    return build_filter_option_payload(
        view=normalized_view,
        source=normalized_source,
        market_id=market_id,
        dimensions=dimensions,
        atc_rows=atc_rows,
    )


def build_filter_option_payload(
    *,
    view: str,
    source: str,
    market_id: str | None,
    dimensions: Sequence[DimensionOptionRow],
    atc_rows: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    grouped: dict[str, list[DimensionOptionRow]] = defaultdict(list)
    for row in dimensions:
        grouped[row.dimension_type].append(row)
    ordered_dimensions: list[dict[str, object]] = []
    for dimension_type in DIMENSION_ORDER_BY_SOURCE.get(source, ()):
        rows = sorted(grouped.get(dimension_type, ()), key=lambda item: item.dimension_value)
        ordered_dimensions.append(
            {
                "dimension_type": dimension_type,
                "label": DIMENSION_LABELS.get(dimension_type, dimension_type),
                "values": [
                    {"key": item.dimension_value_norm, "value": item.dimension_value, "row_count": item.row_count}
                    for item in rows
                ],
            }
        )
    return {
        "view": view,
        "source": source,
        "market_id": market_id,
        "dimensions": ordered_dimensions,
        "atc": build_atc_hierarchy(atc_rows),
    }


def build_atc_hierarchy(rows: Iterable[Mapping[str, object]]) -> dict[str, object]:
    buckets: dict[str, dict[str, str]] = {"atc1": {}, "atc2": {}, "atc3": {}, "atc4": {}}
    for row in rows:
        code = str(row.get("atc4_code") or "").strip().upper()
        if not code:
            continue
        desc = str(row.get("atc4_desc") or "").strip()
        prefixes = {"atc1": code[:1], "atc2": code[:3], "atc3": code[:4], "atc4": code}
        for level, value in prefixes.items():
            if value:
                buckets[level].setdefault(value, desc if level == "atc4" else "")
    return {
        **{
            level: [
                {"key": value, "value": value, "label": f"{value} {label}".strip()}
                for value, label in sorted(values.items())
            ]
            for level, values in buckets.items()
        },
        "selectable_levels": list(SELECTABLE_ATC_LEVELS),
    }


def normalize_view(value: str) -> str:
    normalized = value.strip().lower()
    if normalized not in {"general", "strategic"}:
        raise DynamicMarketInputError(f"unsupported filter option view: {value}")
    return normalized


def _load_dimension_options(*, dimension_db: str, view: str, source: str, market_id: str | None) -> tuple[DimensionOptionRow, ...]:
    table = GENERAL_DIMENSION_TABLE if view == "general" else STRATEGIC_DIMENSION_TABLE
    where = ["source = %s"]
    params: list[str] = [source]
    if view == "general":
        if atc_prefix := _general_atc_prefix(market_id):
            where.append("atc4_code LIKE %s")
            params.append(atc_prefix)
    elif market_id:
        market_kind, normalized_market_id = _strategic_market_filter(market_id)
        where.extend(["market_kind = %s", "market_id = %s"])
        params.extend([market_kind, normalized_market_id])
    rows = db.fetch_all(
        f"""
        SELECT dimension_type, dimension_value, dimension_value_norm, COUNT(*) AS row_count
        FROM {quote_identifier(dimension_db)}.{table}
        WHERE {" AND ".join(where)}
        GROUP BY dimension_type, dimension_value, dimension_value_norm
        ORDER BY dimension_type, dimension_value
        """,
        params,
    )
    return tuple(
        DimensionOptionRow(
            dimension_type=str(row["dimension_type"]),
            dimension_value=str(row["dimension_value"]),
            dimension_value_norm=str(row["dimension_value_norm"]),
            row_count=int(row["row_count"]),
        )
        for row in rows
    )


def _load_atc_rows(*, mart_db: str, view: str, source: str, market_id: str | None) -> tuple[dict[str, object], ...]:
    if view == "strategic":
        table, id_column = _strategic_atc_table(market_id)
        where = ["source = %s"]
        params: list[str] = [source]
        if market_id:
            _, normalized_market_id = _strategic_market_filter(market_id)
            where.append(f"{id_column} = %s")
            params.append(normalized_market_id)
        sql = f"""
            SELECT DISTINCT JSON_UNQUOTE(JSON_EXTRACT(by_dimension, '$.atc4_code')) AS atc4_code,
                   JSON_UNQUOTE(JSON_EXTRACT(by_dimension, '$.atc4_desc')) AS atc4_desc
            FROM {quote_identifier(mart_db)}.{table}
            WHERE {" AND ".join(where)}
        """
        return tuple(db.fetch_all(sql, params))
    where = ["source = %s"]
    params: list[str] = [source]
    if atc_prefix := _general_atc_prefix(market_id):
        where.append("atc4_code LIKE %s")
        params.append(atc_prefix)
    rows = db.fetch_all(
        f"""
        SELECT DISTINCT atc4_code, atc4_desc
        FROM {quote_identifier(mart_db)}.mart_general_brand_metric
        WHERE {" AND ".join(where)}
        ORDER BY atc4_code
        """,
        params,
    )
    return tuple(rows)


def _general_atc_prefix(market_id: str | None) -> str | None:
    if not market_id:
        return None
    normalized = market_id.strip().upper()
    if not normalized:
        return None
    return f"{normalized}%"


def _strategic_market_filter(market_id: str) -> tuple[str, str]:
    normalized = market_id.strip()
    if normalized.startswith("ml_"):
        return "ml", normalized
    if normalized.startswith("cd_"):
        return "cd", normalized
    if normalized.startswith("strategy_"):
        return "ml", f"ml_{normalized.removeprefix('strategy_')}"
    raise DynamicMarketInputError(f"unsupported strategic market id: {market_id}")


def _strategic_atc_table(market_id: str | None) -> tuple[str, str]:
    if market_id and market_id.strip().startswith("cd_"):
        return "mart_strategic_cd_brand_metric", "cd_market_id"
    return "mart_strategic_ml_brand_metric", "ml_id"
