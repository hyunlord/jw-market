from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ChatRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    question: str = Field(
        min_length=0,
        description="사용자 질문 텍스트입니다. 파일 전용 업로드 확인 시나리오에서는 빈 문자열도 허용합니다.",
    )
    document_paths: tuple[str, ...] = Field(
        default=(),
        description="세션에 첨부된 파일 경로 목록입니다. 일반적인 235 bridge 경로에서는 시스템이 파일 검색 컨텍스트를 자동 위임합니다.",
    )
    file_context: str | None = Field(
        default=None,
        description="파일 검색 결과 컨텍스트 문자열입니다. 선택 필드이며 보통 235 bridge 검색 결과로 시스템이 자동 채웁니다.",
    )
    external_mode: str = Field(
        default="live",
        description='외부 API 호출 모드입니다. 기본값은 "live"이며, 테스트/격리 검증에는 fixture 응답을 쓰는 "fixture"를 사용할 수 있습니다.',
    )
    conversation_id: str | None = Field(
        default=None,
        description="세션/대화 식별자입니다. 제공하면 이전 대화 컨텍스트와 pending clarification 상태를 이어서 사용합니다.",
    )


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
    file_sources: list[dict[str, Any]] = []


class HealthResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: str
