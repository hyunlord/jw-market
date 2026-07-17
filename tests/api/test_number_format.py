from __future__ import annotations

import math
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
import sys

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from pipeline.scripts.api.composers import number_format


@pytest.mark.parametrize(
    "value",
    [
        0.0,
        -0.0,
        1.23459,
        -1.23459,
        999_999_999_999.9999,
        -999_999_999_999.9999,
        0.00001,
        -0.00001,
        1e-5,
        -1e-5,
        1e20,
        -1e20,
        math.pi,
        -math.pi,
    ],
)
def test_float_format_matches_decimal_round_down_contract(value: float) -> None:
    expected = float(
        Decimal(str(value)).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
    )

    actual = number_format.format_number(value)

    assert actual == expected
    assert math.copysign(1.0, actual) == math.copysign(1.0, expected)


def test_finite_float_fast_path_does_not_construct_decimal(monkeypatch) -> None:
    def fail_decimal(*_args, **_kwargs):
        raise AssertionError("finite float formatting must not construct Decimal")

    monkeypatch.setattr(number_format, "Decimal", fail_decimal)

    assert number_format.format_number(123.456789) == 123.4567


def test_float_fast_path_matches_decimal_at_truncation_boundaries() -> None:
    values: list[float] = []
    for numerator in range(-2_000, 2_001):
        boundary = numerator / 10_000
        values.extend(
            (
                math.nextafter(boundary, -math.inf),
                boundary,
                math.nextafter(boundary, math.inf),
            )
        )
        values.extend(
            boundary * scale
            for scale in (1_000_003.0, 99_999_983.0, 499_999_993.0)
        )

    for value in values:
        expected = float(
            Decimal(str(value)).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
        )
        actual = number_format.format_number(value)
        assert actual == expected, value
        assert math.copysign(1.0, actual) == math.copysign(1.0, expected), value


def test_decimal_input_keeps_decimal_quantize_contract() -> None:
    assert number_format.format_number(Decimal("123.456789")) == 123.4567


def test_numeric_subclasses_keep_formatting_contract() -> None:
    class FloatValue(float):
        pass

    class DecimalValue(Decimal):
        pass

    assert number_format.format_number(FloatValue(123.456789)) == 123.4567
    assert number_format.format_number(DecimalValue("123.456789")) == 123.4567


@pytest.mark.parametrize("value", [None, True, False, 0, 42, "리바로", object()])
def test_non_float_values_pass_through_unchanged(value: object) -> None:
    assert number_format.format_number(value) is value
