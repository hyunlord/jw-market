from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Final
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin
from urllib.request import Request, urlopen

import pymysql
from pymysql.cursors import DictCursor


SEGMENT_LEVELS: Final[tuple[str, ...]] = ("class", "molecule", "ox_gx")
SEGMENT_PROVENANCE: Final[str] = "actual=live_api:/api/cause/리바로;expected=mart_sql:mart_strategic_ml_market_metric"
MARKET_GROWTH_PROVENANCE: Final[str] = "actual=live_api:/api/dynamic-market;expected=mart_sql:mart_general_market_metric"
BRAND_SOURCE_PROVENANCE: Final[str] = "actual=live_api:/api/brands;expected=live_api:/api/deep-analysis/{brand}"
DEFAULT_DB_ENV: Final[Mapping[str, str]] = {
    "host": "DB_HOST",
    "port": "DB_PORT",
    "user": "DB_USER",
    "password": "DB_PASSWORD",
    "database": "DB_NAME",
}


@dataclass(frozen=True, slots=True)
class ReadOnlyDbConfig:
    host: str
    port: int
    user: str
    password: str
    database: str


def _db_config_from_env(env: Mapping[str, str]) -> ReadOnlyDbConfig:
    missing = [name for name in DEFAULT_DB_ENV.values() if not env.get(name)]
    if missing:
        raise RuntimeError(f"missing database environment: {','.join(missing)}")
    return ReadOnlyDbConfig(
        host=str(env["DB_HOST"]),
        port=int(env["DB_PORT"]),
        user=str(env["DB_USER"]),
        password=str(env["DB_PASSWORD"]),
        database=str(env["DB_NAME"]),
    )


def fetch_json(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    body: object | None = None,
    timeout_seconds: float = 30.0,
    accepted_statuses: Sequence[int] = (200,),
) -> Any:
    data = None
    headers: dict[str, str] = {}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
        headers["Content-Type"] = "application/json"
    request = Request(
        urljoin(f"{base_url.rstrip('/')}/", path.lstrip("/")),
        data=data,
        headers=headers,
        method=method,
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            status = response.status
            raw = response.read()
    except HTTPError as exc:
        status = exc.code
        raw = exc.read()
    except (TimeoutError, URLError, OSError) as exc:
        raise RuntimeError(f"live request failed for {path}: {exc}") from exc
    if status not in accepted_statuses:
        raise RuntimeError(f"live request failed for {path}: status={status}")
    if not raw.strip():
        raise RuntimeError(f"live request returned empty body for {path}")
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError(f"live request returned invalid JSON for {path}: {exc}") from exc
    if payload is None or payload == {} or payload == []:
        raise RuntimeError(f"live request returned empty payload for {path}")
    return payload


def fetch_read_only_rows(
    query: str,
    params: Mapping[str, object],
    *,
    env: Mapping[str, str],
) -> list[dict[str, Any]]:
    config = _db_config_from_env(env)
    try:
        with pymysql.connect(
            host=config.host,
            port=config.port,
            user=config.user,
            password=config.password,
            database=config.database,
            charset="utf8mb4",
            cursorclass=DictCursor,
            autocommit=False,
            read_timeout=30,
            write_timeout=30,
            connect_timeout=10,
        ) as connection:
            with connection.cursor() as cursor:
                cursor.execute("SET SESSION TRANSACTION READ ONLY")
                cursor.execute("START TRANSACTION READ ONLY")
                cursor.execute("SELECT @@tx_read_only AS read_only")
                state = cursor.fetchone()
                if not state or int(state["read_only"]) != 1:
                    raise RuntimeError("database transaction is not read-only")
                cursor.execute(query, params)
                rows = list(cursor.fetchall())
                connection.rollback()
    except pymysql.MySQLError as exc:
        raise RuntimeError(f"read-only SQL failed: {exc}") from exc
    return rows


def _tracked_contract(contract_id: str) -> dict[str, Any]:
    path = Path(__file__).resolve().parents[3] / "tests" / "api" / "api_golden_contracts.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    for contract in document["contracts"]:
        if contract["id"] == contract_id:
            return contract
    raise RuntimeError(f"tracked golden contract not found: {contract_id}")


def _tracked_request_path(contract_id: str) -> str:
    request = _tracked_contract(contract_id).get("request")
    path = request.get("path") if isinstance(request, dict) else None
    if not isinstance(path, str) or not path.startswith("/"):
        raise RuntimeError(f"tracked golden contract has no absolute request path: {contract_id}")
    return path


def _series_by_period(series: object) -> dict[str, float | None]:
    if isinstance(series, str):
        series = json.loads(series)
    if isinstance(series, dict):
        points = [(str(period), point) for period, point in sorted(series.items())]
    elif isinstance(series, list):
        points = []
        for point in series:
            if not isinstance(point, dict) or not point.get("period"):
                raise RuntimeError("market_size_series list entries require period")
            points.append((str(point["period"]), point))
    else:
        points = []
    if not points:
        raise RuntimeError("market_size_series is empty")
    values: dict[str, float | None] = {}
    for period, point in points:
        raw = point
        if isinstance(point, dict):
            raw = next(
                (point[key] for key in ("value", "raw_value", "market_size", "total") if point.get(key) is not None),
                None,
            )
        try:
            values[period] = None if raw is None else float(raw)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"invalid market_size_series value: {raw!r}") from exc
    return values


def _series_values(series: object) -> list[float | None]:
    return list(_series_by_period(series).values())


def _series_latest_total(series: object) -> float:
    latest = _series_values(series)[-1]
    if latest is None:
        raise RuntimeError("latest market_size_series value is missing")
    return latest


def _extract_segment_sum(payload: object, level: str) -> float:
    if not isinstance(payload, dict):
        raise RuntimeError("cause payload must be an object")
    data = payload.get("data")
    if not isinstance(data, dict):
        data = payload
    analysis_levels = data.get("analysis_levels")
    level_data = analysis_levels.get("data") if isinstance(analysis_levels, dict) else None
    if not isinstance(level_data, dict):
        raise RuntimeError("cause payload missing data.analysis_levels.data")
    public_name = {"class": "Class", "molecule": "Molecule", "ox_gx": "Ox/Gx"}[level]
    section = next(
        (value for name, value in level_data.items() if str(name).casefold() == public_name.casefold()),
        None,
    )
    if not isinstance(section, dict):
        raise RuntimeError(f"cause payload does not contain segment level {public_name}")
    segments = section.get("segments")
    if not isinstance(segments, list):
        by_channel = section.get("by_channel")
        segments = by_channel.get("전체") if isinstance(by_channel, dict) else None
    if not isinstance(segments, list) or not segments:
        raise RuntimeError(f"cause payload segment level {public_name} is empty")
    total = 0.0
    observed = 0
    for segment in segments:
        if not isinstance(segment, dict):
            raise RuntimeError(f"cause payload segment level {public_name} contains a non-object")
        label = str(segment.get("name") or segment.get("segment") or "").strip().casefold()
        if label in {"overall", "total", "전체"}:
            continue
        raw_value = next(
            (segment[key] for key in ("value", "sales", "raw_value") if segment.get(key) is not None),
            None,
        )
        if raw_value is None:
            raise RuntimeError(f"cause payload segment {public_name}/{label} is missing a value")
        total += float(raw_value)
        observed += 1
    if observed == 0:
        raise RuntimeError(f"cause payload segment level {public_name} has no non-total rows")
    return total


def collect_segment_sum_evidence(
    base_url: str,
    *,
    timeout_seconds: float,
    env: Mapping[str, str],
    fetcher: Callable[..., Any] = fetch_json,
    sql_fetcher: Callable[..., list[dict[str, Any]]] = fetch_read_only_rows,
) -> dict[str, object]:
    contract = _tracked_contract("cause_livalo")
    request = contract["request"]
    payload = fetcher(base_url, request["path"], method=request["method"], body=request.get("body"), timeout_seconds=timeout_seconds)
    rows = sql_fetcher(
        """
        SELECT market_size_series
        FROM mart_strategic_ml_market_metric
        WHERE ml_id = %(ml_id)s
          AND source = %(source)s
          AND measure = %(measure)s
        LIMIT 1
        """,
        {"ml_id": "ml_006", "source": "ubist", "measure": "sales"},
        env=env,
    )
    if len(rows) != 1:
        raise RuntimeError(f"expected one strategic market metric row, got {len(rows)}")
    total = _series_latest_total(rows[0]["market_size_series"])
    observations = [
        {
            "level": level,
            "segment_sum": _extract_segment_sum(payload, level),
            "market_total": total,
        }
        for level in SEGMENT_LEVELS
    ]
    return {
        "classification": "census",
        "provenance": SEGMENT_PROVENANCE,
        "identity": {"market": "ml_006", "source": "ubist", "measure": "sales"},
        "observations": observations,
    }


def _growth_rate(
    start: float | None,
    end: float | None,
    elapsed_periods: int,
    periods_per_year: int,
) -> float | None:
    if start is None or end is None or start <= 0.0 or end < 0.0 or elapsed_periods <= 0:
        return None
    return ((end / start) ** (periods_per_year / elapsed_periods) - 1.0) * 100.0


def _five_year_prior_period(period: str) -> str:
    try:
        year, suffix = period.split("-", 1)
        return f"{int(year) - 5}-{suffix}"
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"invalid market period: {period!r}") from exc


def _series_growth_inputs(
    series: object,
) -> tuple[float | None, float | None, str | None, str | None]:
    values = _series_by_period(series)
    numeric_periods = [period for period, value in values.items() if value is not None]
    if not numeric_periods:
        return None, None, None, None
    end_period = max(numeric_periods)
    end = values[end_period]
    exact_baseline_period = _five_year_prior_period(end_period)
    if values.get(exact_baseline_period) is not None:
        baseline_period = exact_baseline_period
    else:
        prior_periods = [
            period
            for period in numeric_periods
            if period < end_period
        ]
        baseline_period = min(prior_periods) if prior_periods else None
    start = values.get(baseline_period) if baseline_period is not None else None
    return start, end, baseline_period, end_period


def _period_ordinal(period: str, periods_per_year: int) -> int:
    year, suffix = period.split("-", 1)
    offset = int(suffix.removeprefix("Q")) - 1
    return int(year) * periods_per_year + offset


def _series_growth_expectations(series: object, source: str) -> tuple[dict[str, float | None], str | None]:
    values = _series_by_period(series)
    start, _, baseline_period, _ = _series_growth_inputs(series)
    if baseline_period is None:
        return {}, None
    periods_per_year = 4 if source == "iqvia_nsa" else 12
    baseline_ordinal = _period_ordinal(baseline_period, periods_per_year)
    expected = {
        period: _growth_rate(
            start,
            value,
            _period_ordinal(period, periods_per_year) - baseline_ordinal,
            periods_per_year,
        )
        for period, value in values.items()
    }
    return expected, baseline_period


def _actual_growth_from_payload(
    payload: object,
    expected_end_period: str | None,
) -> tuple[float | None, str | None]:
    if not isinstance(payload, dict):
        raise RuntimeError("dynamic payload must be an object")
    series = payload
    for key in ("result", "data"):
        if isinstance(series, dict) and isinstance(series.get(key), dict):
            series = series[key]
    points = series.get("market_size_series") if isinstance(series, dict) else None
    if not isinstance(points, list) or not points:
        raise RuntimeError("dynamic payload missing market_size_series")
    if not all(isinstance(point, dict) for point in points):
        raise RuntimeError("dynamic market_size_series entry is not an object")
    latest = points[-1]
    if expected_end_period is not None:
        matches = [point for point in points if str(point.get("period") or "") == expected_end_period]
        if not matches:
            raise RuntimeError(f"dynamic payload missing expected growth endpoint {expected_end_period}")
        latest = matches[-1]
    value = latest.get("mom_growth_pct")
    actual_period = str(latest.get("period") or "") or None
    return (None if value is None else float(value), actual_period)


def _actual_growth_series(payload: object) -> dict[str, float | None]:
    if not isinstance(payload, dict):
        raise RuntimeError("dynamic payload must be an object")
    series = payload
    for key in ("result", "data"):
        if isinstance(series, dict) and isinstance(series.get(key), dict):
            series = series[key]
    points = series.get("market_size_series") if isinstance(series, dict) else None
    if not isinstance(points, list) or not all(isinstance(point, dict) for point in points):
        raise RuntimeError("dynamic payload missing market_size_series")
    return {
        str(point.get("period") or ""): None if point.get("mom_growth_pct") is None else float(point["mom_growth_pct"])
        for point in points
        if point.get("period")
    }


def collect_market_growth_evidence(
    base_url: str,
    *,
    timeout_seconds: float,
    max_workers: int,
    env: Mapping[str, str],
    fetcher: Callable[..., Any] = fetch_json,
    sql_fetcher: Callable[..., list[dict[str, Any]]] = fetch_read_only_rows,
) -> dict[str, object]:
    dynamic_path = _tracked_request_path("dynamic_general_c10a1_livalo")
    rows = sql_fetcher(
        """
        SELECT source, atc4_code, market_size_series
        FROM mart_general_market_metric
        WHERE measure = 'sales'
          AND source IN ('ubist', 'iqvia_nsa')
        ORDER BY source, atc4_code
        """,
        {},
        env=env,
    )
    if not rows:
        raise RuntimeError("empty market growth SQL census")

    def probe(row: Mapping[str, Any]) -> dict[str, object]:
        source = str(row["source"])
        market = str(row["atc4_code"])
        start, end, baseline_period, end_period = _series_growth_inputs(row["market_size_series"])
        periods_per_year = 4 if source == "iqvia_nsa" else 12
        elapsed_periods = (
            _period_ordinal(end_period, periods_per_year) - _period_ordinal(baseline_period, periods_per_year)
            if end_period is not None and baseline_period is not None
            else 0
        )
        expected = _growth_rate(start, end, elapsed_periods, periods_per_year)
        expected_series, _ = _series_growth_expectations(row["market_size_series"], source)
        api_source = "iqvia" if source == "iqvia_nsa" else source
        body = {
            "view": "general",
            "source": api_source,
            "measure": "sales",
            "filters": {"atc4": [market]},
        }
        try:
            payload = fetcher(base_url, dynamic_path, method="POST", body=body, timeout_seconds=timeout_seconds)
            actual, actual_end_period = _actual_growth_from_payload(payload, end_period)
            actual_series = _actual_growth_series(payload)
            error = None
        except RuntimeError as exc:
            actual = None
            actual_end_period = None
            actual_series = {}
            error = str(exc)
        return {
            "market": market,
            "source": source,
            "periods_per_year": periods_per_year,
            "elapsed_periods": elapsed_periods,
            "expected": expected,
            "expected_baseline_period": baseline_period,
            "expected_end_period": end_period,
            "actual": actual,
            "actual_end_period": actual_end_period,
            "point_checks": [
                {"period": period, "expected": point_expected, "actual": actual_series.get(period)}
                for period, point_expected in sorted(expected_series.items())
            ],
            "error": error,
        }

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(probe, row) for row in rows]
        observations = [future.result() for future in as_completed(futures)]
    observations.sort(key=lambda item: (str(item["source"]), str(item["market"])))
    return {
        "classification": "census",
        "provenance": MARKET_GROWTH_PROVENANCE,
        "observations": observations,
    }


def _source_list(brand: Mapping[str, Any], view: str) -> set[str]:
    field = "general_sources" if view == "general" else "strategic_sources"
    value = brand.get(field)
    if not isinstance(value, list):
        return set()
    return {str(item).upper() for item in value}


def _brand_contexts(payload: object, expected_brand: str) -> list[dict[str, str]]:
    items = payload
    if isinstance(payload, dict):
        items = payload.get("items") or payload.get("brands") or payload.get("data")
    if not isinstance(items, list) or not items:
        raise RuntimeError("brand search returned no contexts")
    contexts: list[dict[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        brand = str(item.get("name") or item.get("brand") or item.get("brand_name") or "")
        if brand != expected_brand:
            continue
        raw_contexts = item.get("contexts")
        if not isinstance(raw_contexts, list):
            continue
        for context in raw_contexts:
            if not isinstance(context, dict):
                continue
            view_kind = str(context.get("view_kind") or "")
            market_id = str(context.get("market_id") or "")
            if view_kind and market_id:
                contexts.append({"brand": brand, "view_kind": view_kind, "market_id": market_id})
    return contexts


def _has_deep_data(payload: object) -> bool:
    if not isinstance(payload, dict):
        raise RuntimeError("deep-analysis payload must be an object")
    detail = payload.get("detail")
    if isinstance(detail, dict):
        if detail.get("error") == "source_not_available":
            return False
        raise RuntimeError(f"deep-analysis probe failed: {detail.get('error') or detail}")
    market_meta = payload.get("market_meta")
    if not isinstance(market_meta, dict) or not isinstance(market_meta.get("has_market_data"), bool):
        raise RuntimeError("deep-analysis payload missing boolean market_meta.has_market_data")
    return bool(market_meta["has_market_data"])


def collect_brand_source_evidence(
    base_url: str,
    *,
    timeout_seconds: float,
    max_workers: int,
    fetcher: Callable[..., Any] = fetch_json,
) -> dict[str, object]:
    brands_path = _tracked_request_path("brands")
    prefix, marker, suffix = brands_path.rpartition("/api/brands")
    if not marker or suffix:
        raise RuntimeError(f"tracked brands path is not canonical: {brands_path}")
    deep_analysis_path = f"{prefix}/api/deep-analysis"
    brands_payload = fetcher(base_url, brands_path, timeout_seconds=timeout_seconds)
    brands = brands_payload
    if isinstance(brands_payload, dict):
        brands = brands_payload.get("brands")
    if not isinstance(brands, list) or not brands:
        raise RuntimeError("default /api/brands returned empty brand population")

    def observe(brand: Mapping[str, Any], view: str, source: str) -> dict[str, object]:
        name = str(brand.get("name") or brand.get("brand") or brand.get("brand_name") or "")
        listed = source in _source_list(brand, view)
        try:
            search = fetcher(
                base_url,
                f"{brands_path}?{urlencode({'q': name})}",
                timeout_seconds=timeout_seconds,
            )
            contexts = _brand_contexts(search, name)
            has_data = False
            for context in contexts:
                context_view = context["view_kind"]
                if view == "general" and context_view != "general":
                    continue
                if view == "strategic" and context_view not in {"strategic_ml", "strategic_cd"}:
                    continue
                path = f"{deep_analysis_path}/{quote(context['brand'])}"
                payload = fetcher(
                    base_url,
                    f"{path}?{urlencode({'view_kind': context_view, 'market_id': context['market_id'], 'source': source.lower()})}",
                    timeout_seconds=timeout_seconds,
                    accepted_statuses=(200, 422),
                )
                has_data = has_data or _has_deep_data(payload)
            error = None
        except RuntimeError as exc:
            has_data = False
            error = str(exc)
        return {"brand": name, "view": view, "source": source, "listed": listed, "has_data": has_data, "error": error}

    jobs = [
        (brand, view, source)
        for brand in brands
        if isinstance(brand, dict)
        for view in ("general", "strategic")
        for source in ("UBIST", "IQVIA")
    ]
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(observe, brand, view, source) for brand, view, source in jobs]
        observations = [future.result() for future in as_completed(futures)]
    observations.sort(key=lambda item: (str(item["brand"]), str(item["view"]), str(item["source"])))
    return {
        "classification": "census",
        "provenance": BRAND_SOURCE_PROVENANCE,
        "observations": observations,
    }


def write_evidence(path: Path | None, evidence: Mapping[str, object]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
