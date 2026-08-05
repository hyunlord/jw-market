from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
import os

import requests


class DeepAnalysisBackendError(RuntimeError):
    """Raised when the read-only deep-analysis API cannot satisfy its contract."""


class DeepAnalysisBackend:
    """Read the existing formal deep-analysis endpoint without generating values."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        timeout: tuple[float, float] = (3.0, 15.0),
        session: requests.Session | None = None,
    ) -> None:
        self._base_url = (
            base_url
            or os.environ.get("DEEP_ANALYSIS_BACKEND_URL")
            or "http://jw-market-backend-api-service"
        ).rstrip("/")
        self._timeout = timeout
        self._session = session or requests.Session()

    def get_analysis(
        self,
        *,
        brand: str,
        view_kind: str,
        market_id: str,
        source: str,
    ) -> dict[str, object]:
        try:
            response = self._session.get(
                f"{self._base_url}/api/deep-analysis/{brand}",
                params={
                    "view_kind": view_kind,
                    "market_id": market_id,
                    "source": source,
                },
                timeout=self._timeout,
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise DeepAnalysisBackendError(
                f"deep-analysis backend unavailable: {type(exc).__name__}"
            ) from exc
        if not isinstance(payload, dict):
            raise DeepAnalysisBackendError("deep-analysis backend returned a non-object payload")
        return project_deep_analysis_response(
            payload,
            brand=brand,
            view_kind=view_kind,
            market_id=market_id,
            source=source,
        )


def project_deep_analysis_response(
    payload: Mapping[str, object],
    *,
    brand: str,
    view_kind: str,
    market_id: str,
    source: str,
) -> dict[str, object]:
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise DeepAnalysisBackendError("deep-analysis response has no data object")
    evidence = (
        *_forecast_evidence(data.get("forecast"), source),
        *_simulation_evidence(data.get("simulation"), source),
        *_profile_evidence(data.get("brand_factors"), source),
    )
    generated_at = payload.get("generated_at")
    evidence_periods = tuple(
        str(item["period"])
        for item in evidence
        if item.get("period") not in (None, "")
    )
    dashboard_tables = _deep_analysis_tables(evidence)
    insight = _insight_payload(
        data.get("ai_analysis"),
        brand=brand,
        market_id=market_id,
    )
    return {
        "source": source.upper(),
        "tool": "market.get_deep_analysis",
        "summary_text": f"{brand} 심층분석의 사전 계산 결과를 조회했습니다.",
        "evidence": evidence,
        "model_insight_status": (
            "available_model_generated" if insight is not None else "unavailable"
        ),
        "insight": insight,
        "limitations": (),
        "render_data": {
            "brand": brand,
            "metric": "deep_analysis",
            "period": evidence_periods[-1] if evidence_periods else None,
            "unit_label": "mixed",
            "view_type": view_kind,
            "market_id": market_id,
            "source_label": source.upper(),
            "forecast": data.get("forecast"),
            "simulation": data.get("simulation"),
            "brand_profile": data.get("brand_factors"),
            "dashboard_tables": dashboard_tables,
            "generated_at": generated_at,
        },
    }


def _insight_payload(
    value: object,
    *,
    brand: str,
    market_id: str,
) -> dict[str, object] | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return {
        "raw_text": value,
        "generated_by": "deep-analysis-api-llm",
        "fetched_at_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "target_market": market_id,
        "target_brand": brand,
        "api_response_location": "data.ai_analysis",
    }


def _forecast_evidence(value: object, source: str) -> tuple[dict[str, object], ...]:
    rows: list[dict[str, object]] = []
    for combo, section in _combo_sections(value, source):
        brands = section.get("brands")
        if not isinstance(brands, Sequence) or isinstance(brands, (str, bytes)):
            continue
        for brand in brands:
            if not isinstance(brand, Mapping):
                continue
            brand_name = str(brand.get("brand") or brand.get("brand_name") or "")
            for period, raw_value, share in _parallel_points(
                section.get("history_periods"),
                brand.get("history_values"),
                brand.get("history_ms_pct"),
            ):
                rows.append(
                    {
                        "value_kind": "observed",
                        "source": source.upper(),
                        "combo": combo,
                        "brand": brand_name,
                        "period": period,
                        "value": raw_value,
                        "market_share": share,
                    }
                )
            forecast_points = _parallel_points(
                section.get("forecast_periods"),
                brand.get("forecast_values"),
                brand.get("forecast_ms_pct"),
            )
            if not forecast_points:
                forecast_points = tuple(
                    (
                        str(point.get("period") or ""),
                        point.get("value"),
                        (
                            point.get("market_share")
                            if "market_share" in point
                            else point.get("ms")
                        ),
                    )
                    for point in _mapping_sequence(brand.get("forecast"))
                    if point.get("period") not in (None, "")
                )
            for period, raw_value, share in forecast_points:
                rows.append(
                    {
                        "value_kind": "system_forecast",
                        "source": source.upper(),
                        "combo": combo,
                        "brand": brand_name,
                        "period": period,
                        "value": raw_value,
                        "market_share": share,
                    }
                )
    return tuple(rows)


def _simulation_evidence(value: object, source: str) -> tuple[dict[str, object], ...]:
    rows: list[dict[str, object]] = []
    for combo, section in _combo_sections(value, source):
        by_brand = section.get("by_brand")
        if not isinstance(by_brand, Mapping):
            continue
        for brand_name, brand in by_brand.items():
            if not isinstance(brand, Mapping):
                continue
            scenarios = brand.get("scenarios")
            if not isinstance(scenarios, Mapping):
                scenarios = brand
            for scenario in ("base", "lower", "upper"):
                scenario_payload = scenarios.get(scenario)
                periods = brand.get("forecast_periods") or section.get("forecast_periods")
                values = (
                    scenario_payload.get("values")
                    if isinstance(scenario_payload, Mapping)
                    else None
                )
                points = _parallel_points(periods, values, None)
                if not points:
                    points = tuple(
                        (str(point.get("period") or ""), point.get("value"), None)
                        for point in _mapping_sequence(scenario_payload)
                        if point.get("period") not in (None, "")
                    )
                for period, raw_value, _share in points:
                    rows.append(
                        {
                            "value_kind": "system_simulation",
                            "source": source.upper(),
                            "combo": combo,
                            "brand": str(brand_name),
                            "scenario": scenario,
                            "period": period,
                            "value": raw_value,
                        }
                    )
    return tuple(rows)


def _profile_evidence(value: object, source: str) -> tuple[dict[str, object], ...]:
    if not isinstance(value, Mapping):
        return ()
    rows: list[dict[str, object]] = []
    for source_name, source_rows in value.items():
        if str(source_name).casefold() != source.casefold():
            continue
        for row in _mapping_sequence(source_rows):
            rows.append(
                {
                    **{str(key): item for key, item in row.items()},
                    "value_kind": "observed_profile",
                    "source": str(source_name or source).upper(),
                }
            )
    return tuple(rows)


def _combo_sections(
    value: object,
    source: str,
) -> tuple[tuple[str, Mapping[str, object]], ...]:
    if not isinstance(value, Mapping):
        return ()
    by_combo = value.get("by_combo")
    if not isinstance(by_combo, Mapping):
        return ()
    return tuple(
        (str(combo), section)
        for combo, section in by_combo.items()
        if isinstance(section, Mapping)
        and section.get("available") is not False
        and str(combo).partition(".")[0].casefold() == source.casefold()
    )


def _mapping_sequence(value: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _parallel_points(
    periods: object,
    values: object,
    shares: object,
) -> tuple[tuple[str, object, object], ...]:
    if not isinstance(periods, Sequence) or isinstance(periods, (str, bytes)):
        return ()
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return ()
    share_values = (
        shares
        if isinstance(shares, Sequence) and not isinstance(shares, (str, bytes))
        else ()
    )
    return tuple(
        (
            str(period),
            values[index],
            share_values[index] if index < len(share_values) else None,
        )
        for index, period in enumerate(periods)
        if index < len(values) and period not in (None, "") and values[index] is not None
    )


def _deep_analysis_tables(
    evidence: tuple[dict[str, object], ...],
) -> tuple[dict[str, object], ...]:
    tables: list[dict[str, object]] = []
    observed = tuple(
        (
            item.get("source"),
            item.get("combo"),
            item.get("brand"),
            item.get("period"),
            item.get("value"),
            item.get("market_share"),
        )
        for item in evidence
        if item.get("value_kind") == "observed"
    )
    if observed:
        tables.append(
            {
                "name": "심층분석 실적",
                "columns": ("소스", "지표", "브랜드", "기간", "값", "점유율(%)"),
                "rows": observed,
            }
        )
    forecast = tuple(
        (
            item.get("source"),
            item.get("combo"),
            item.get("brand"),
            item.get("period"),
            item.get("value"),
            item.get("market_share"),
        )
        for item in evidence
        if item.get("value_kind") == "system_forecast"
    )
    if forecast:
        tables.append(
            {
                "name": "시스템 예측",
                "columns": ("소스", "지표", "브랜드", "기간", "예측값", "예측 점유율(%)"),
                "rows": forecast,
            }
        )
    simulation = tuple(
        (
            item.get("source"),
            item.get("combo"),
            item.get("brand"),
            item.get("scenario"),
            item.get("period"),
            item.get("value"),
        )
        for item in evidence
        if item.get("value_kind") == "system_simulation"
    )
    if simulation:
        tables.append(
            {
                "name": "시스템 시뮬레이션",
                "columns": ("소스", "지표", "브랜드", "시나리오", "기간", "값"),
                "rows": simulation,
            }
        )
    profile = tuple(
        (
            item.get("source"),
            item.get("brand"),
            str(field),
            value,
        )
        for item in evidence
        if item.get("value_kind") == "observed_profile"
        for field, value in _profile_fields(item)
    )
    if profile:
        tables.append(
            {
                "name": "브랜드 프로파일링",
                "columns": ("소스", "브랜드", "항목", "값"),
                "rows": profile,
            }
        )
    return tuple(tables)


def _profile_fields(item: Mapping[str, object]) -> tuple[tuple[str, object], ...]:
    excluded = {"value_kind", "source", "brand", "brand_key"}
    fields: list[tuple[str, object]] = []
    for key, value in item.items():
        if key in excluded:
            continue
        if isinstance(value, Mapping):
            fields.extend((str(nested_key), nested_value) for nested_key, nested_value in value.items())
        else:
            fields.append((str(key), value))
    return tuple(fields)
