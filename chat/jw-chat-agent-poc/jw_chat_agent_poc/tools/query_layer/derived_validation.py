from __future__ import annotations

from dataclasses import asdict
import json

from jw_chat_agent_poc.tools.query_layer.derived_models import DerivedParityReport
from jw_chat_agent_poc.tools.query_layer.derived_validation_live import LiveDerivedCensus
from jw_chat_agent_poc.tools.query_layer.store import MartSnapshot


def derived_parity_report(snapshot: MartSnapshot) -> DerivedParityReport:
    live = LiveDerivedCensus.build(snapshot)
    failures: list[str] = []
    checked = 0
    checked += _identity_set(
        failures,
        "market",
        set(live.market_points),
        set(snapshot.derived.market_points),
    )
    for key, expected in live.market_points.items():
        actual = snapshot.derived.market_points.get(key)
        if actual is None:
            continue
        checked += _compare(
            failures,
            f"market:{key}",
            expected,
            (actual.total_krw, actual.hhi, actual.cr5_pct, actual.denominator),
        )
    checked += _identity_set(
        failures,
        "brand",
        set(live.brand_points),
        set(snapshot.derived.brand_points),
    )
    for key, expected in live.brand_points.items():
        actual = snapshot.derived.brand_points.get(key)
        if actual is None:
            continue
        checked += _compare(
            failures,
            f"brand:{key}",
            expected,
            (actual.value_krw, actual.share_pct, actual.rank, actual.source_status),
        )
    insight_keys = {key[:-1] for key in live.brand_points}
    checked += _identity_set(
        failures,
        "insight",
        insight_keys,
        set(snapshot.derived.insights),
    )
    for key in insight_keys:
        actual = snapshot.derived.insights.get(key)
        if actual is None:
            continue
        expected = live.insight(key)
        actual_values = asdict(actual)
        checked += 1
        if _canonical(expected) != _canonical(actual_values):
            failures.append(f"insight:{key}:canonical byte mismatch")
        for field, expected_value in expected.items():
            checked += 1
            actual_value = actual_values[field]
            if expected_value != actual_value:
                failures.append(
                    f"insight:{key}:{field}: expected={expected_value!r} actual={actual_value!r}"
                )
    population = checked
    return DerivedParityReport(
        classification="census",
        checked=checked,
        population=population,
        failures=tuple(failures),
        exit_code=1 if population == 0 or failures else 0,
    )


def _identity_set(
    failures: list[str],
    label: str,
    expected: set[tuple[str, ...]],
    actual: set[tuple[str, ...]],
) -> int:
    for key in sorted(expected - actual):
        failures.append(f"{label}:missing:{key}")
    for key in sorted(actual - expected):
        failures.append(f"{label}:unexpected:{key}")
    return len(expected | actual)


def _compare(
    failures: list[str],
    label: str,
    expected: tuple[object, ...],
    actual: tuple[object, ...],
) -> int:
    if _canonical(expected) != _canonical(actual):
        failures.append(f"{label}:canonical byte mismatch")
    for index, (left, right) in enumerate(zip(expected, actual, strict=True)):
        if left != right:
            failures.append(f"{label}:{index}: expected={left!r} actual={right!r}")
    return len(expected) + 1


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
