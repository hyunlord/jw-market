from __future__ import annotations

from collections.abc import Mapping
import os
from typing import Any, Final


SHADOW_TRANSPORT_ENV: Final = "CHAT_CLAIM_IR_SHADOW_TRANSPORT_ENABLED"
_TRUE_VALUES: Final = frozenset({"1", "true", "on", "enabled", "yes"})


def trace_for_transport(trace: Mapping[str, Any]) -> dict[str, Any]:
    """Return a shallow transport copy while retaining the server-owned trace."""
    projected = dict(trace)
    if os.getenv(SHADOW_TRANSPORT_ENV, "false").strip().casefold() not in _TRUE_VALUES:
        projected.pop("claim_ir_shadow", None)
    return projected


__all__ = ["SHADOW_TRANSPORT_ENV", "trace_for_transport"]
