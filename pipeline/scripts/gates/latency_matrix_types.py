from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class MatrixCase:
    identifier: str
    method: str
    path: str
    body: Mapping[str, Any] | None = None
    mask_generated_at: bool = False
