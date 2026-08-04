from __future__ import annotations

import math
import re
from copy import deepcopy
from typing import Any

from .general_window import calculation_period_scope, rolling_period_scope

_PERIOD_KEY = re.compile(r"^\d{4}-(?:\d{2}|Q[1-4])$")
_MISSING = object()
_LATEST_SNAPSHOT_FIELDS = frozenset(
    {
        "ei_ms_matrix",
        "growth_contribution_ms_matrix",
        "target_customer_competition",
    }
)


def _is_period(value: object) -> bool:
    return isinstance(value, str) and _PERIOD_KEY.fullmatch(value) is not None


def _periods_in(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if _is_period(key):
                found.add(key)
            found.update(_periods_in(item))
    elif isinstance(value, list):
        for item in value:
            found.update(_periods_in(item))
            if isinstance(item, dict) and _is_period(item.get("period")):
                found.add(str(item["period"]))
    return found


def _product_key(product: dict[str, Any]) -> tuple[str, str]:
    return (
        str(product.get("product_code") or ""),
        str(product.get("product_name") or ""),
    )


def _merge_products(
    existing: Any,
    candidate: Any,
    *,
    target_periods: frozenset[str],
    display_periods: tuple[str, ...],
    calculation_periods: tuple[str, ...],
    baseline_exists: bool,
    path: tuple[str, ...],
) -> list[dict[str, Any]]:
    old_products = {
        _product_key(item): item
        for item in existing
        if isinstance(item, dict)
    } if isinstance(existing, list) else {}
    new_products = {
        _product_key(item): item
        for item in candidate
        if isinstance(item, dict)
    } if isinstance(candidate, list) else {}
    merged: list[dict[str, Any]] = []
    for key in sorted(set(old_products) | set(new_products)):
        old = old_products.get(key, _MISSING)
        new = new_products.get(key, _MISSING)
        item = _merge_value(
            old,
            new,
            target_periods=target_periods,
            display_periods=display_periods,
            calculation_periods=calculation_periods,
            baseline_exists=baseline_exists and old is not _MISSING,
            path=(*path, key[0] or key[1]),
        )
        if not isinstance(item, dict):
            continue
        history = item.get("raw_value_history")
        if isinstance(history, dict):
            item["raw_value_total"] = float(
                math.fsum(float(value or 0.0) for value in history.values())
            )
        merged.append(item)
    return sorted(
        merged,
        key=lambda item: (
            -float(item.get("raw_value_total") or 0.0),
            _product_key(item),
        ),
    )


def _merge_period_map(
    existing: dict[str, Any],
    candidate: dict[str, Any],
    *,
    target_periods: frozenset[str],
    allowed_periods: tuple[str, ...],
    baseline_exists: bool,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for period in allowed_periods:
        if period in target_periods:
            if period in candidate:
                result[period] = deepcopy(candidate[period])
            continue
        if baseline_exists:
            if period in existing:
                result[period] = deepcopy(existing[period])
    return result


def _merge_value(
    existing: Any,
    candidate: Any,
    *,
    target_periods: frozenset[str],
    display_periods: tuple[str, ...],
    calculation_periods: tuple[str, ...],
    baseline_exists: bool,
    path: tuple[str, ...],
) -> Any:
    if path and path[-1] == "products":
        return _merge_products(
            existing,
            candidate,
            target_periods=target_periods,
            display_periods=display_periods,
            calculation_periods=calculation_periods,
            baseline_exists=baseline_exists,
            path=path,
        )

    latest = display_periods[-1] if display_periods else None
    if path == ("target_customer_competition", "latest"):
        if latest in target_periods and candidate is not _MISSING:
            return deepcopy(candidate)
        if baseline_exists and existing is not _MISSING:
            return deepcopy(existing)
        return deepcopy(candidate) if candidate is not _MISSING else None

    if isinstance(existing, dict) or isinstance(candidate, dict):
        old = existing if isinstance(existing, dict) else {}
        new = candidate if isinstance(candidate, dict) else {}
        if any(_is_period(key) for key in (*old.keys(), *new.keys())):
            allowed = (
                calculation_periods
                if path == ("raw_value_history",)
                else display_periods
            )
            return _merge_period_map(
                old,
                new,
                target_periods=target_periods,
                allowed_periods=allowed,
                baseline_exists=baseline_exists,
            )
        result: dict[str, Any] = {}
        ordered_keys = (*old.keys(), *(key for key in new if key not in old))
        for key in ordered_keys:
            old_item = old.get(key, _MISSING)
            new_item = new.get(key, _MISSING)
            result[key] = _merge_value(
                old_item,
                new_item,
                target_periods=target_periods,
                display_periods=display_periods,
                calculation_periods=calculation_periods,
                baseline_exists=baseline_exists and old_item is not _MISSING,
                path=(*path, str(key)),
            )
        return result

    if isinstance(existing, list) or isinstance(candidate, list):
        old_list = existing if isinstance(existing, list) else []
        new_list = candidate if isinstance(candidate, list) else []
        if path and path[0] in _LATEST_SNAPSHOT_FIELDS:
            if not baseline_exists or latest in target_periods:
                return deepcopy(new_list)
            return deepcopy(old_list)
        return deepcopy(old_list if baseline_exists else new_list)

    if baseline_exists and existing is not _MISSING:
        return deepcopy(existing)
    if candidate is not _MISSING:
        return deepcopy(candidate)
    return None


def merge_scoped_row(
    existing: dict[str, Any] | None,
    candidate: dict[str, Any] | None,
    *,
    period_scope: tuple[str, ...],
    source_periods: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """Replace only requested period cells while retaining the serving JSON schema."""

    targets = frozenset(str(period).strip() for period in period_scope if str(period).strip())
    if not targets:
        raise ValueError("period-scoped merge requires at least one period")
    if existing is None and candidate is None:
        raise ValueError("period-scoped merge requires an existing or candidate row")
    source_row = candidate or existing or {}
    source = str(source_row.get("source") or "").strip()
    if source not in {"ubist", "iqvia_nsa"}:
        raise ValueError(f"unsupported period-scoped source: {source!r}")

    periods = set(source_periods or ())
    if not periods:
        periods = _periods_in(existing) | _periods_in(candidate)
    periods.update(targets)
    display = rolling_period_scope(periods, source=source)
    calculation = calculation_period_scope(periods, source=source)
    merged = _merge_value(
        existing if existing is not None else _MISSING,
        candidate if candidate is not None else _MISSING,
        target_periods=targets,
        display_periods=display,
        calculation_periods=calculation,
        baseline_exists=existing is not None,
        path=(),
    )
    assert isinstance(merged, dict)
    payload = merged.get("payload")
    if isinstance(payload, dict) and "metric_history" in merged:
        payload["period_count"] = len(merged.get("metric_history") or {})
        payload["calculation_period_count"] = len(
            merged.get("raw_value_history") or {}
        )
    return merged
