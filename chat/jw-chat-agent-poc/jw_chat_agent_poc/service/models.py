from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ChatRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    question: str = Field(min_length=1)
    document_paths: tuple[str, ...] = ()
    external_mode: str = "live"
    conversation_id: str | None = None


class ChatAccepted(BaseModel):
    model_config = ConfigDict(frozen=True)

    session_id: str
    conversation_id: str | None = None
    sources: tuple[str, ...]


class ChatAnswer(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str
    charts: list[dict[str, Any]]
    trace: dict[str, Any]
    sources: tuple[str, ...]
    conversation_id: str | None = None


class HealthResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: str
