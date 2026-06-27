from __future__ import annotations

from dataclasses import asdict
from typing import TypeAlias


RouterDiagnosticValue: TypeAlias = str | bool | float | None


def router_diagnostics(router) -> dict[str, RouterDiagnosticValue]:
    diagnostics = getattr(router, "last_diagnostics", None)
    if diagnostics is None:
        return {"mode": "keyword", "fallback_used": False, "reason": "legacy_router"}
    return asdict(diagnostics)
