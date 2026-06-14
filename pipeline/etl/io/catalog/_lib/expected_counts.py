from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[5]
DEFAULT_EXPECTED_ROW_COUNTS_PATH = PROJECT_ROOT / "pipeline" / "etl" / "config" / "expected_row_counts.yaml"


@lru_cache(maxsize=1)
def load_expected_row_counts(path: Path = DEFAULT_EXPECTED_ROW_COUNTS_PATH) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"expected row count config not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"expected row count config must be a mapping: {path}")
    return data


def expected_value(key: str) -> Any:
    value: Any = load_expected_row_counts()
    for part in key.split("."):
        if not isinstance(value, dict) or part not in value:
            raise KeyError(f"expected row count config key not found: {key}")
        value = value[part]
    return deepcopy(value)


def expected_int(key: str) -> int:
    value = expected_value(key)
    if not isinstance(value, int):
        raise TypeError(f"expected integer config value for {key}, got {type(value).__name__}")
    return value


def expected_mapping(key: str) -> dict[str, Any]:
    value = expected_value(key)
    if not isinstance(value, dict):
        raise TypeError(f"expected mapping config value for {key}, got {type(value).__name__}")
    return value
