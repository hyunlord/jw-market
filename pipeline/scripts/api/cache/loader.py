from __future__ import annotations

import argparse
import json
import logging
import time
from dataclasses import asdict, dataclass
from typing import Any

from pipeline.scripts.api.cache.keys import (
    cache_key_brands,
    cache_key_cause,
    cache_key_deep_analysis,
    cache_key_market_status,
)
from pipeline.scripts.api.cache.store import set_cache, truncate_cache
from pipeline.scripts.api.catalog import DISPLAY_BRANDS, IQVIA_MEASURES, UBIST_MEASURES, VIEWS
from pipeline.scripts.api.services import (
    build_brands_response,
    build_cause_response,
    build_deep_analysis_response,
    build_market_status_response,
    latest_period_for_brand,
    resolve_brand,
)
from pipeline.scripts.api.utils import json_dumps


LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class CauseVariant:
    brand_name: str
    view: str
    source: str
    measure: str


def compute_168_variants() -> list[CauseVariant]:
    variants: list[CauseVariant] = []
    for brand in DISPLAY_BRANDS:
        for source in brand.sources:
            measures = UBIST_MEASURES if source == "UBIST" else IQVIA_MEASURES
            for measure in measures:
                for view in VIEWS:
                    variants.append(
                        CauseVariant(
                            brand_name=brand.brand_name,
                            view=view,
                            source=source,
                            measure=measure,
                        )
                    )
    if len(variants) != 168:
        raise RuntimeError(f"Expected 168 cause variants, got {len(variants)}")
    return variants


def _timed_build(builder, *args, **kwargs) -> tuple[Any, int]:
    start = time.perf_counter()
    response = builder(*args, **kwargs)
    return response, int((time.perf_counter() - start) * 1000)


def rebuild_brands_cache() -> dict[str, Any]:
    stats: dict[str, Any] = {
        "keys_rebuilt": 0,
        "errors": [],
        "details": [],
    }

    response, elapsed = _timed_build(build_brands_response)
    set_cache(cache_key_brands(), "brands", response, computation_ms=elapsed)
    stats["keys_rebuilt"] += 1
    stats["details"].append({"endpoint": "brands", "cache_key": cache_key_brands(), "ms": elapsed})
    return stats


def rebuild_all_cache(*, clear_existing: bool = True) -> dict[str, Any]:
    if clear_existing:
        truncate_cache()

    stats: dict[str, Any] = rebuild_brands_cache()
    stats["variants"] = len(compute_168_variants())

    response, elapsed = _timed_build(build_market_status_response, "latest")
    set_cache(cache_key_market_status("latest"), "market_status", response, period_yyyymm="latest", computation_ms=elapsed)
    stats["keys_rebuilt"] += 1
    stats["details"].append(
        {"endpoint": "market_status", "cache_key": cache_key_market_status("latest"), "ms": elapsed}
    )

    for brand in DISPLAY_BRANDS:
        try:
            resolved = resolve_brand(brand.brand_name)
            period = latest_period_for_brand(resolved.brand_id)
            response, elapsed = _timed_build(build_deep_analysis_response, brand.brand_name, period)
            key = cache_key_deep_analysis(brand.brand_name, period)
            set_cache(
                key,
                "deep_analysis",
                response,
                brand_name=brand.brand_name,
                period_yyyymm=period,
                computation_ms=elapsed,
            )
            stats["keys_rebuilt"] += 1
            stats["details"].append({"endpoint": "deep_analysis", "cache_key": key, "ms": elapsed})
        except Exception as exc:  # noqa: BLE001 - audit should retain per-brand failures
            stats["errors"].append({"endpoint": "deep_analysis", "brand": brand.brand_name, "error": str(exc)})

    for variant in compute_168_variants():
        try:
            resolved = resolve_brand(variant.brand_name)
            period = latest_period_for_brand(resolved.brand_id)
            response, elapsed = _timed_build(
                build_cause_response,
                variant.brand_name,
                view=variant.view,
                source=variant.source,
                measure=variant.measure,
                period=period,
            )
            key = cache_key_cause(variant.brand_name, variant.view, variant.source, variant.measure, period)
            set_cache(
                key,
                "cause",
                response,
                brand_name=variant.brand_name,
                period_yyyymm=period,
                view=variant.view,
                source=variant.source,
                measure=variant.measure,
                computation_ms=elapsed,
            )
            stats["keys_rebuilt"] += 1
            stats["details"].append({"endpoint": "cause", "cache_key": key, "ms": elapsed, "variant": asdict(variant)})
        except Exception as exc:  # noqa: BLE001 - audit should retain per-variant failures
            stats["errors"].append({"endpoint": "cause", "variant": asdict(variant), "error": str(exc)})

    return stats


def main() -> int:
    parser = argparse.ArgumentParser(description="Rebuild Layer 4 response_store cache")
    parser.add_argument("--keep-existing", action="store_true", help="Do not truncate response_store first")
    parser.add_argument("--rebuild-brands", action="store_true", help="Only rebuild the /api/brands cache key")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    stats = rebuild_brands_cache() if args.rebuild_brands else rebuild_all_cache(clear_existing=not args.keep_existing)
    print(json.dumps(json.loads(json_dumps(stats)), ensure_ascii=False, indent=2))
    return 1 if stats["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
