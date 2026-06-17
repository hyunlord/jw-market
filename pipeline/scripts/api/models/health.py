from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    service: str
    layer3: dict[str, Any]
    layer4: dict[str, Any]
    generated_at: str
