from __future__ import annotations

from typing import Any

from pipeline.scripts.api.composers.number_format import deep_format_numbers
from pipeline.scripts.api.utils import loads_json_maybe


def compose_cached_json(raw: Any) -> Any:
    return deep_format_numbers(loads_json_maybe(raw))
