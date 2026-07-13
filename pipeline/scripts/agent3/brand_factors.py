from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pipeline.scripts.ai_analysis.bundle_builder.competitor_resolver import resolve_market_top5_competitors

from .db import DbConfig, connect
from .source_loader import Agent3Source, Agent3SourceLoader
from .source_processing import source_db_value


@dataclass(frozen=True, slots=True)
class BrandFactor:
    brand_key: str
    brand: str


def source_competitor_top5(
    *,
    brand_name: str,
    source: Agent3Source,
    config: DbConfig | None = None,
) -> list[dict[str, Any]]:
    target = _target_market_row(brand_name=brand_name, source=source, config=config)
    if target is None:
        return []
    with connect(config) as conn:
        resolved = resolve_market_top5_competitors(
            brand_name=brand_name,
            ml_id=str(target["ml_id"]),
            cd_id=None,
            source=source.upper(),
            db_conn=conn,
        )
    competitors = resolved.get("top_competitors")
    return competitors if isinstance(competitors, list) else []


def assemble_brand_factors(
    *,
    selected_brand_key: str,
    selected_brand_name: str,
    config: DbConfig | None = None,
) -> list[dict[str, Any]]:
    factor_brands = _factor_brands(
        selected_brand_key=selected_brand_key,
        selected_brand_name=selected_brand_name,
        config=config,
    )
    loader = Agent3SourceLoader(config)
    sections = loader.load_factor_sections([item.brand_key for item in factor_brands])
    return [
        {
            "brand": item.brand,
            "brand_key": item.brand_key,
            "iqvia": sections.get((item.brand_key, "iqvia"), {}),
            "ubist": sections.get((item.brand_key, "ubist"), {}),
        }
        for item in factor_brands
    ]


def _factor_brands(*, selected_brand_key: str, selected_brand_name: str, config: DbConfig | None) -> list[BrandFactor]:
    seen = {selected_brand_key}
    result = [BrandFactor(brand_key=selected_brand_key, brand=selected_brand_name)]
    for source in ("iqvia", "ubist"):
        for item in source_competitor_top5(brand_name=selected_brand_name, source=source, config=config):
            brand_key = str(item.get("brand_key") or item.get("brand_name") or "")
            brand_name = str(item.get("brand_name") or brand_key)
            if not brand_key or brand_key in seen:
                continue
            seen.add(brand_key)
            result.append(BrandFactor(brand_key=brand_key, brand=brand_name))
            if len(result) >= 6:
                return result
    return result


def _target_market_row(*, brand_name: str, source: Agent3Source, config: DbConfig | None) -> dict[str, Any] | None:
    with connect(config) as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                """
                SELECT ml_id, brand_key, brand_name, source, metric_history
                FROM mart_strategic_ml_brand_metric
                WHERE (brand_key=%s OR brand_name=%s)
                  AND source=%s
                  AND measure='sales'
                ORDER BY ml_id, brand_key
                LIMIT 1
                """,
                (brand_name, brand_name, source_db_value(source)),
            )
            row = cursor.fetchone()
    return dict(row) if row else None
