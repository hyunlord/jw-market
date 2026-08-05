from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest


SCRIPT = Path(__file__).parents[1] / "scripts" / "chat_guard_final_analyze.py"
SPEC = importlib.util.spec_from_file_location("chat_guard_final_analyze", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def test_multiplier_does_not_invent_value_when_single_run_has_no_failures() -> None:
    assert MODULE._multiplier(1, 0) == "[확인 불가: single-run false positive=0]"


def test_percent_is_explicit_for_zero_denominator() -> None:
    assert MODULE._percent(0, 0) == "[확인 불가: denominator=0]"


def test_nearest_rank_percentile() -> None:
    assert MODULE._p([1.0, 2.0, 3.0, 4.0], 0.95) == 4.0


def test_cardinality_rejects_duplicate_result_keys() -> None:
    row = {
        "stage": "any_deny_live",
        "case": "one",
        "N": 1,
        "run": 1,
        "condition": "baseline",
    }
    with pytest.raises(ValueError, match="duplicate result key"):
        MODULE.validate_result_cardinality(
            [row, dict(row)],
            {"measured_corpus_windows": [5, 3], "unmeasured_corpus_windows": [7]},
        )
