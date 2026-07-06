from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import pymysql

for candidate in (Path("/app"), Path("/workspace")):
    if (candidate / "pipeline").exists():
        sys.path.insert(0, str(candidate))
        break

from pipeline.scripts.api.composers.cache_to_response import compose_cached_json


REPRESENTATIVE_BRANDS: Final[tuple[str, ...]] = ("리바로젯", "악템라", "헴리브라", "엔커버", "페린젝트", "가드렛")
REQUIRED_TOP_KEYS: Final[frozenset[str]] = frozenset(
    {"available_combos", "brand", "brand_name", "data", "generated_at", "market_id"}
)
REQUIRED_DATA_KEYS: Final[frozenset[str]] = frozenset({"ai_analysis", "events", "forecast", "simulation"})
REQUIRED_EVENT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "body_full",
        "category",
        "category_label",
        "date",
        "id",
        "impact_score",
        "on_chart",
        "on_list",
        "period_map",
        "related_coverage_count",
        "related_sources",
        "related_titles",
        "related_urls",
        "source",
        "source_url",
        "summary",
        "title",
        "url",
    }
)


class CacheRefreshValidationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ValidationSummary:
    live_rows: int
    staging_rows: int
    rows_with_events: int
    total_events: int
    non_events_diff_count: int
    forecast_diff_count: int
    contract_error_count: int
    aktemra_analysis_levels_equal: bool

    def to_json(self) -> dict[str, int | bool]:
        return {
            "live_rows": self.live_rows,
            "staging_rows": self.staging_rows,
            "rows_with_events": self.rows_with_events,
            "total_events": self.total_events,
            "non_events_diff_count": self.non_events_diff_count,
            "forecast_diff_count": self.forecast_diff_count,
            "contract_error_count": self.contract_error_count,
            "aktemra_analysis_levels_equal": self.aktemra_analysis_levels_equal,
        }


def quote_ident(name: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]+", name or ""):
        raise CacheRefreshValidationError(f"unsafe table name: {name!r}")
    return "`" + name.replace("`", "``") + "`"


def stable_json_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def decode_cache_payload(raw: str | None) -> dict[str, Any]:
    payload = compose_cached_json(raw)
    if not isinstance(payload, dict):
        raise CacheRefreshValidationError("cache payload must compose to object")
    return payload


def api_like_payload(payload: Mapping[str, Any], updated_at: Any, ai_analysis: Mapping[str, Any] | None) -> dict[str, Any]:
    out = copy.deepcopy(dict(payload))
    out["generated_at"] = str(updated_at)
    data = out.setdefault("data", {})
    if isinstance(data, dict):
        data["ai_analysis"] = dict(ai_analysis or {})
        data.pop("brand_strength", None)
    return out


def strip_events(payload: Mapping[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(dict(payload))
    data = out.get("data")
    if isinstance(data, dict):
        data.pop("events", None)
    return out


def analysis_levels(payload: Mapping[str, Any]) -> Any:
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return None
    return data.get("analysis_levels")


def event_contract_errors(event: Any, label: str, index: int) -> list[str]:
    if not isinstance(event, Mapping):
        return [f"{label}:event[{index}] is not object"]
    missing = sorted(REQUIRED_EVENT_KEYS.difference(str(key) for key in event.keys()))
    errors = [f"{label}:event[{index}] missing {key}" for key in missing]
    if "on_list" in event and not isinstance(event["on_list"], bool):
        errors.append(f"{label}:event[{index}].on_list is not bool")
    if "on_chart" in event and not isinstance(event["on_chart"], bool):
        errors.append(f"{label}:event[{index}].on_chart is not bool")
    if "period_map" in event and not isinstance(event["period_map"], Mapping):
        errors.append(f"{label}:event[{index}].period_map is not object")
    return errors


def payload_contract_errors(payload: Mapping[str, Any], label: str) -> list[str]:
    missing_top = sorted(REQUIRED_TOP_KEYS.difference(payload.keys()))
    errors = [f"{label}:missing top key {key}" for key in missing_top]
    data = payload.get("data")
    if not isinstance(data, Mapping):
        return [*errors, f"{label}:data is not object"]
    missing_data = sorted(REQUIRED_DATA_KEYS.difference(str(key) for key in data.keys()))
    errors.extend(f"{label}:missing data key {key}" for key in missing_data)
    events = data.get("events")
    if not isinstance(events, list):
        return [*errors, f"{label}:events is not list"]
    for index, event in enumerate(events):
        errors.extend(event_contract_errors(event, label, index))
    return errors


def validate_tables(conn: Any, live_table: str, staging_table: str) -> ValidationSummary:
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) AS c FROM {quote_ident(live_table)}")
        live_rows = int(cur.fetchone()["c"])
        cur.execute(f"SELECT COUNT(*) AS c FROM {quote_ident(staging_table)}")
        staging_rows = int(cur.fetchone()["c"])
        if live_rows <= 0:
            raise CacheRefreshValidationError("live cache_deep_analysis is empty")
        if staging_rows != live_rows:
            raise CacheRefreshValidationError(f"staging row count {staging_rows} != live row count {live_rows}")

        cur.execute(
            """
            SELECT brand, ai_analysis_json
            FROM cache_deep_analysis_ai_analysis
            WHERE brand IN %s
            """,
            (REPRESENTATIVE_BRANDS,),
        )
        ai_by_brand = {row["brand"]: json.loads(row.get("ai_analysis_json") or "{}") for row in cur.fetchall()}

        cur.execute(
            f"""
            SELECT s.brand, s.market_id, s.updated_at, s.response_json AS staging_json,
                   l.response_json AS live_json
            FROM {quote_ident(staging_table)} s
            JOIN {quote_ident(live_table)} l USING (brand, market_id)
            ORDER BY s.brand, s.market_id
            """
        )
        rows = cur.fetchall()

    rows_with_events = 0
    total_events = 0
    non_events_diff: list[str] = []
    forecast_diff: list[str] = []
    contract_errors: list[str] = []
    aktemra_live_levels: list[Any] = []
    aktemra_staging_levels: list[Any] = []

    for row in rows:
        label = f"{row['brand']}::{row['market_id']}"
        live_payload = decode_cache_payload(row.get("live_json"))
        staging_payload = decode_cache_payload(row.get("staging_json"))
        api_payload = api_like_payload(staging_payload, row.get("updated_at"), ai_by_brand.get(row["brand"]))
        contract_errors.extend(payload_contract_errors(api_payload, label))
        events = ((api_payload.get("data") or {}).get("events") or []) if isinstance(api_payload.get("data"), Mapping) else []
        if events:
            rows_with_events += 1
            total_events += len(events)
        if stable_json_hash(strip_events(live_payload)) != stable_json_hash(strip_events(staging_payload)):
            non_events_diff.append(label)
        live_forecast = (live_payload.get("data") or {}).get("forecast") if isinstance(live_payload.get("data"), Mapping) else None
        staging_forecast = (
            (staging_payload.get("data") or {}).get("forecast") if isinstance(staging_payload.get("data"), Mapping) else None
        )
        if stable_json_hash(live_forecast) != stable_json_hash(staging_forecast):
            forecast_diff.append(label)
        if row["brand"] == "악템라":
            aktemra_live_levels.append(analysis_levels(live_payload))
            aktemra_staging_levels.append(analysis_levels(staging_payload))

    if contract_errors:
        raise CacheRefreshValidationError("contract errors: " + json.dumps(contract_errors[:20], ensure_ascii=False))
    if total_events <= 0:
        raise CacheRefreshValidationError("staging cache has zero events")
    if non_events_diff:
        raise CacheRefreshValidationError("non-events changed: " + json.dumps(non_events_diff[:20], ensure_ascii=False))
    if forecast_diff:
        raise CacheRefreshValidationError("forecast changed: " + json.dumps(forecast_diff[:20], ensure_ascii=False))
    if stable_json_hash(aktemra_live_levels) != stable_json_hash(aktemra_staging_levels):
        raise CacheRefreshValidationError("aktemra analysis_levels changed during events-only refresh")

    return ValidationSummary(
        live_rows=live_rows,
        staging_rows=staging_rows,
        rows_with_events=rows_with_events,
        total_events=total_events,
        non_events_diff_count=len(non_events_diff),
        forecast_diff_count=len(forecast_diff),
        contract_error_count=len(contract_errors),
        aktemra_analysis_levels_equal=True,
    )


def connect_db() -> Any:
    return pymysql.connect(
        host=os.environ["MARIADB_HOST"],
        port=int(os.environ.get("MARIADB_PORT", "3306")),
        user=os.environ["MARIADB_USER"],
        password=os.environ["MARIADB_PASSWORD"],
        database=os.environ.get("MARIADB_DATABASE", "jw_mart"),
        charset="utf8mb4",
        autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate cache_deep_analysis events-only staging without modifying live.")
    parser.add_argument("--live-table", default=os.environ.get("LIVE_TABLE", "cache_deep_analysis"))
    parser.add_argument("--staging-table", default=os.environ.get("STAGING_TABLE"))
    args = parser.parse_args()
    if not args.staging_table:
        raise CacheRefreshValidationError("--staging-table or STAGING_TABLE is required")
    return args


def main() -> None:
    args = parse_args()
    conn = connect_db()
    try:
        summary = validate_tables(conn, args.live_table, args.staging_table)
        print(
            "CACHE_REFRESH_VALIDATE_JSON="
            + json.dumps({**summary.to_json(), "modified_live": False}, ensure_ascii=False)
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
