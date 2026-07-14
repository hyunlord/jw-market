from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, is_dataclass
import json
import math

from pipeline.scripts.forecast import forecast_runner


def _canonical(value: object) -> bytes:
    def encode(item: object) -> object:
        if is_dataclass(item) and not isinstance(item, type):
            return asdict(item)
        raise TypeError(type(item).__name__)

    return json.dumps(
        value,
        default=encode,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _grain(index: int) -> tuple[list[str], list[float], tuple[str, ...]]:
    periods = [f"2024-{month:02d}" for month in range(1, 13)] + [
        f"2025-{month:02d}" for month in range(1, 13)
    ]
    values = [
        100.0 + index + (month * 1.7) + (8.0 * math.sin(month * math.pi / 6.0))
        for month in range(24)
    ]
    identity = ("brand", f"brand-{index}", "A10N3", "UBIST", "sales")
    return periods, values, identity


def _run(index: int) -> bytes:
    periods, values, identity = _grain(index)
    result = forecast_runner.build_forecast_result(
        periods,
        values,
        "UBIST",
        12,
        rng_identity=identity,
    )
    return _canonical(result)


def test_stable_seed_is_identity_bound_and_repeatable() -> None:
    identity = ("brand", "brand-1", "A10N3", "UBIST", "sales")

    first = forecast_runner._stable_forecast_seed(identity)
    second = forecast_runner._stable_forecast_seed(identity)
    other = forecast_runner._stable_forecast_seed((*identity[:-1], "volume"))

    assert first == second
    assert first != other


def test_holtwinters_is_byte_deterministic_serial_and_parallel() -> None:
    indexes = list(range(8))

    serial_a = [_run(index) for index in indexes]
    serial_b = [_run(index) for index in indexes]
    with ThreadPoolExecutor(max_workers=4) as executor:
        parallel_a = list(executor.map(_run, indexes))
    with ThreadPoolExecutor(max_workers=4) as executor:
        parallel_b = list(executor.map(_run, indexes))

    assert serial_a == serial_b
    assert parallel_a == parallel_b
    assert serial_a == parallel_a
