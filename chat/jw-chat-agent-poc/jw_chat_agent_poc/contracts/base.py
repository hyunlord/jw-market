from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ContractModel(BaseModel):
    """Strict immutable base for cross-phase contracts."""

    model_config = ConfigDict(extra="forbid", frozen=True)
