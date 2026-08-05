from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse

from pipeline.scripts.api.chat_usage_materialization import ChatMaterializationUnavailable
from pipeline.scripts.api.dashboard_usage import (
    DEFAULT_CACHE_TTL_SECONDS,
    ChatTurnDataContractError,
    DashboardQueryError,
    ChatTurnFilters,
    ChatTurnsRepository,
    InvalidChatTurnCursor,
    MAX_USAGE_LOG_PAGE_SIZE,
    MAX_USAGE_LOG_RANGE_DAYS,
    MAX_RANGE_DAYS,
    InvalidUsageLogCursor,
    UsageLogFilters,
    UsageHistoryRepository,
    UsageFilters,
    UsageStatsService,
    decode_chat_turn_cursor,
    decode_chat_turn_id,
    decode_usage_log_cursor,
)

LOGGER = logging.getLogger(__name__)


def _today() -> date:
    return datetime.now(UTC).date()


def _validate_range(date_from: date, date_to: date) -> None:
    if date_from > date_to:
        raise HTTPException(status_code=422, detail="시작일은 종료일보다 늦을 수 없습니다.")
    if (date_to - date_from).days + 1 > MAX_RANGE_DAYS:
        raise HTTPException(status_code=422, detail=f"조회 기간은 최대 {MAX_RANGE_DAYS}일입니다.")


def _materialization_error_detail(error: ChatMaterializationUnavailable) -> dict:
    messages = {
        "missing": "채팅 통계 준비 상태를 확인할 수 없습니다.",
        "status": "채팅 통계 갱신이 완료되지 않았습니다.",
        "stale": "채팅 통계가 최신 상태가 아닙니다. 잠시 후 다시 시도해 주세요.",
        "coverage": "요청한 기간의 채팅 통계가 아직 준비되지 않았습니다.",
    }
    return {
        "error": "chat_materialization_unavailable",
        "reason": error.reason,
        "message": messages[error.reason],
        "available_from": error.available_from.isoformat() if error.available_from else None,
        "available_to": error.available_to.isoformat() if error.available_to else None,
    }


def create_usage_dashboard_router(service: UsageStatsService) -> APIRouter:
    router = APIRouter(tags=["internal-dashboard"])

    @router.get("/api/dashboard/usage-stats", include_in_schema=False)
    def usage_stats(
        date_from: date | None = Query(default=None),
        date_to: date | None = Query(default=None),
        grain: Literal["day", "week"] = Query(default="day"),
        user_id: int | None = Query(default=None, ge=1),
        user_ids: list[int] | None = Query(default=None),
        excluded_user_ids: list[int] | None = Query(default=None),
        department: str | None = Query(default=None, min_length=1, max_length=100),
    ) -> dict:
        resolved_to = date_to or _today()
        resolved_from = date_from or (resolved_to - timedelta(days=29))
        _validate_range(resolved_from, resolved_to)

        resolved_user_ids = tuple(sorted(set(user_ids or ())))
        if len(resolved_user_ids) > 200 or any(user < 1 for user in resolved_user_ids):
            raise HTTPException(status_code=422, detail="사용자 목록 필터가 올바르지 않습니다.")
        resolved_excluded_user_ids = tuple(sorted(set(excluded_user_ids or ())))
        if len(resolved_excluded_user_ids) > 200 or any(
            user < 1 for user in resolved_excluded_user_ids
        ):
            raise HTTPException(status_code=422, detail="제외 사용자 목록이 올바르지 않습니다.")

        def fetch(resolved_date_from: date, resolved_date_to: date) -> dict:
            return service.get(
                UsageFilters(
                    date_from=resolved_date_from,
                    date_to=resolved_date_to,
                    grain=grain,
                    user_id=user_id,
                    user_ids=resolved_user_ids,
                    department=department.strip() if department else None,
                    excluded_user_ids=resolved_excluded_user_ids,
                )
            )

        try:
            return fetch(resolved_from, resolved_to)
        except ChatMaterializationUnavailable as error:
            if (
                error.reason == "coverage"
                and error.available_from is not None
                and error.available_to is not None
                and (date_from is None or date_to is None)
            ):
                clamped_from = (
                    max(resolved_from, error.available_from)
                    if date_from is None
                    else resolved_from
                )
                clamped_to = (
                    min(resolved_to, error.available_to) if date_to is None else resolved_to
                )
                if clamped_from <= clamped_to and (
                    clamped_from != resolved_from or clamped_to != resolved_to
                ):
                    try:
                        return fetch(clamped_from, clamped_to)
                    except ChatMaterializationUnavailable as retry_error:
                        error = retry_error
            raise HTTPException(
                status_code=503,
                detail=_materialization_error_detail(error),
            ) from error
        except DashboardQueryError as error:
            LOGGER.exception(
                "usage dashboard query failed",
                extra={"query_name": error.query_name},
            )
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "dashboard_query_unavailable",
                    "message": "사용 통계 데이터 소스를 조회할 수 없습니다. 잠시 후 다시 시도해 주세요.",
                },
            ) from error
    return router


def create_usage_logs_router(repository: UsageHistoryRepository) -> APIRouter:
    router = APIRouter(tags=["internal-dashboard"])

    @router.get("/api/dashboard/usage-logs", include_in_schema=False)
    def usage_logs(
        date_from: date = Query(),
        date_to: date = Query(),
        user_id: int | None = Query(default=None, ge=1),
        user_ids: list[int] | None = Query(default=None),
        excluded_user_ids: list[int] | None = Query(default=None),
        department: str | None = Query(default=None, min_length=1, max_length=100),
        endpoint: str | None = Query(default=None, min_length=1, max_length=255),
        http_status: int | None = Query(default=None, ge=100, le=599),
        page_size: int = Query(default=50, ge=1, le=MAX_USAGE_LOG_PAGE_SIZE),
        cursor: str | None = Query(default=None, min_length=1, max_length=512),
        page: int | None = Query(default=None, ge=1),
    ) -> dict:
        if date_from > date_to:
            raise HTTPException(status_code=422, detail="시작일은 종료일보다 늦을 수 없습니다.")
        if (date_to - date_from).days + 1 > MAX_USAGE_LOG_RANGE_DAYS:
            raise HTTPException(
                status_code=422,
                detail=f"조회 기간은 최대 {MAX_USAGE_LOG_RANGE_DAYS}일입니다.",
            )
        resolved_user_ids = tuple(sorted(set(user_ids or ())))
        if len(resolved_user_ids) > 200 or any(user < 1 for user in resolved_user_ids):
            raise HTTPException(status_code=422, detail="사용자 목록 필터가 올바르지 않습니다.")
        resolved_excluded_user_ids = tuple(sorted(set(excluded_user_ids or ())))
        if len(resolved_excluded_user_ids) > 200 or any(
            user < 1 for user in resolved_excluded_user_ids
        ):
            raise HTTPException(status_code=422, detail="제외 사용자 목록이 올바르지 않습니다.")
        if cursor is not None and page is not None:
            raise HTTPException(status_code=422, detail="page와 cursor는 함께 사용할 수 없습니다.")
        try:
            decoded_cursor = decode_usage_log_cursor(cursor) if cursor else None
        except InvalidUsageLogCursor as exc:
            raise HTTPException(status_code=400, detail="cursor가 올바르지 않습니다.") from exc
        page = repository.fetch_logs(
            UsageLogFilters(
                date_from=date_from,
                date_to=date_to,
                user_id=user_id,
                user_ids=resolved_user_ids,
                excluded_user_ids=resolved_excluded_user_ids,
                department=department.strip() if department else None,
                endpoint=endpoint.strip() if endpoint else None,
                http_status=http_status,
                page_size=page_size,
                cursor=decoded_cursor,
                page=page or 1,
            )
        )
        return {
            "items": list(page.items),
            "next_cursor": page.next_cursor,
            "has_more": page.has_more,
            "page": page.page,
            "total_count": page.total_count,
            "total_pages": page.total_pages,
            "page_size": page.page_size,
        }

    router.include_router(create_chat_turns_router(repository))
    return router


def create_chat_turns_router(repository: ChatTurnsRepository) -> APIRouter:
    router = APIRouter(tags=["internal-dashboard"])

    @router.get("/api/dashboard/chat-turns", include_in_schema=False)
    def chat_turns(
        date_from: date = Query(),
        date_to: date = Query(),
        user_id: int | None = Query(default=None, ge=1),
        user_ids: list[int] | None = Query(default=None),
        excluded_user_ids: list[int] | None = Query(default=None),
        department: str | None = Query(default=None, min_length=1, max_length=100),
        page_size: int = Query(default=50, ge=1, le=MAX_USAGE_LOG_PAGE_SIZE),
        cursor: str | None = Query(default=None, min_length=1, max_length=512),
        page: int | None = Query(default=None, ge=1),
    ) -> dict:
        if date_from > date_to:
            raise HTTPException(status_code=422, detail="시작일은 종료일보다 늦을 수 없습니다.")
        if (date_to - date_from).days + 1 > MAX_USAGE_LOG_RANGE_DAYS:
            raise HTTPException(
                status_code=422,
                detail=f"조회 기간은 최대 {MAX_USAGE_LOG_RANGE_DAYS}일입니다.",
            )
        resolved_user_ids = tuple(sorted(set(user_ids or ())))
        if len(resolved_user_ids) > 200 or any(user < 1 for user in resolved_user_ids):
            raise HTTPException(status_code=422, detail="사용자 목록 필터가 올바르지 않습니다.")
        resolved_excluded_user_ids = tuple(sorted(set(excluded_user_ids or ())))
        if len(resolved_excluded_user_ids) > 200 or any(
            user < 1 for user in resolved_excluded_user_ids
        ):
            raise HTTPException(status_code=422, detail="제외 사용자 목록이 올바르지 않습니다.")
        if cursor is not None and page is not None:
            raise HTTPException(status_code=422, detail="page와 cursor는 함께 사용할 수 없습니다.")
        try:
            decoded_cursor = decode_chat_turn_cursor(cursor) if cursor else None
        except InvalidChatTurnCursor as exc:
            raise HTTPException(status_code=400, detail="cursor가 올바르지 않습니다.") from exc
        try:
            page = repository.fetch_chat_turns(
                ChatTurnFilters(
                    date_from=date_from,
                    date_to=date_to,
                    user_id=user_id,
                    user_ids=resolved_user_ids,
                    excluded_user_ids=resolved_excluded_user_ids,
                    department=department.strip() if department else None,
                    page_size=page_size,
                    cursor=decoded_cursor,
                    page=page or 1,
                )
            )
        except ChatMaterializationUnavailable as error:
            return _chat_turns_unavailable(error)
        except DashboardQueryError as error:
            LOGGER.exception(
                "chat turn history query failed",
                extra={"query_name": error.query_name},
            )
            raise HTTPException(
                status_code=503,
                detail={
                    "error": "dashboard_query_unavailable",
                    "message": "채팅 이력을 조회할 수 없습니다. 잠시 후 다시 시도해 주세요.",
                },
            ) from error
        except ChatTurnDataContractError as error:
            LOGGER.exception("chat turn history data contract failed")
            raise HTTPException(
                status_code=500,
                detail={
                    "error": "chat_turn_data_contract_error",
                    "message": "채팅 이력 응답을 구성할 수 없습니다.",
                },
            ) from error
        except Exception as error:
            LOGGER.exception("chat turn history request failed unexpectedly")
            raise HTTPException(
                status_code=500,
                detail={
                    "error": "chat_turns_internal_error",
                    "message": "채팅 이력 요청을 처리할 수 없습니다.",
                },
            ) from error
        return {
            "items": list(page.items),
            "next_cursor": page.next_cursor,
            "has_more": page.has_more,
            "page": page.page,
            "total_count": page.total_count,
            "total_pages": page.total_pages,
            "page_size": page.page_size,
        }

    @router.get("/api/dashboard/chat-turns/{turn_id}", include_in_schema=False)
    def chat_turn_detail(turn_id: str) -> dict:
        try:
            detail_key = decode_chat_turn_id(turn_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="대화 식별자가 올바르지 않습니다.") from exc
        result = repository.fetch_chat_turn(detail_key)
        if result is None:
            raise HTTPException(status_code=404, detail="대화 이력을 찾을 수 없습니다.")
        return result.item

    return router


def _chat_turns_unavailable(error: ChatMaterializationUnavailable) -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "detail": _materialization_error_detail(error),
            "limits": {
                "max_days": MAX_USAGE_LOG_RANGE_DAYS,
                "cache_ttl_seconds": DEFAULT_CACHE_TTL_SECONDS,
            },
        },
    )
