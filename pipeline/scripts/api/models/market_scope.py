"""Pydantic request models for additive market-scope endpoints."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


ViewFamilyValue = Literal["strategy", "general"]
SourceValue = Literal["UBIST", "IQVIA", "ubist", "iqvia", "nsa", "iqvia_nsa"]


class MarketScopeResolveRequest(BaseModel):
    """Request body shared by scope resolution and scope-cause recomputation."""

    model_config = ConfigDict(extra="forbid")

    brand: str
    view_family: ViewFamilyValue = "strategy"
    source: SourceValue = "UBIST"
    measure: str = "sales"
    option_ids: list[str] = Field(min_length=1)


class MarketScopeCauseRequest(MarketScopeResolveRequest):
    """Request body for the cause-compatible market-scope endpoint."""

    view: str = "market_landscape"
