from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, HTTPException, Query

from pipeline.scripts.api.dashboard_usage import (
    MAX_USAGE_LOG_PAGE_SIZE,
    MAX_USAGE_LOG_RANGE_DAYS,
    MAX_RANGE_DAYS,
    InvalidUsageLogCursor,
    UsageLogFilters,
    UsageLogsRepository,
    UsageFilters,
    UsageStatsService,
    decode_usage_log_cursor,
)


def _today() -> date:
    return datetime.now(UTC).date()


def create_usage_dashboard_router(service: UsageStatsService) -> APIRouter:
    router = APIRouter(tags=["internal-dashboard"])

    @router.get("/api/dashboard/usage-stats", include_in_schema=False)
    def usage_stats(
        date_from: date | None = Query(default=None),
        date_to: date | None = Query(default=None),
        grain: Literal["day", "week"] = Query(default="day"),
        user_id: int | None = Query(default=None, ge=1),
        department: str | None = Query(default=None, min_length=1, max_length=100),
    ) -> dict:
        resolved_to = date_to or _today()
        resolved_from = date_from or (resolved_to - timedelta(days=29))
        if resolved_from > resolved_to:
            raise HTTPException(status_code=422, detail="시작일은 종료일보다 늦을 수 없습니다.")
        if (resolved_to - resolved_from).days + 1 > MAX_RANGE_DAYS:
            raise HTTPException(status_code=422, detail=f"조회 기간은 최대 {MAX_RANGE_DAYS}일입니다.")
        return service.get(
            UsageFilters(
                date_from=resolved_from,
                date_to=resolved_to,
                grain=grain,
                user_id=user_id,
                department=department.strip() if department else None,
            )
        )

    return router


def create_usage_logs_router(repository: UsageLogsRepository) -> APIRouter:
    router = APIRouter(tags=["internal-dashboard"])

    @router.get("/api/dashboard/usage-logs", include_in_schema=False)
    def usage_logs(
        date_from: date = Query(),
        date_to: date = Query(),
        user_id: int | None = Query(default=None, ge=1),
        department: str | None = Query(default=None, min_length=1, max_length=100),
        endpoint: str | None = Query(default=None, min_length=1, max_length=255),
        http_status: int | None = Query(default=None, ge=100, le=599),
        page_size: int = Query(default=50, ge=1, le=MAX_USAGE_LOG_PAGE_SIZE),
        cursor: str | None = Query(default=None, min_length=1, max_length=512),
    ) -> dict:
        if date_from > date_to:
            raise HTTPException(status_code=422, detail="시작일은 종료일보다 늦을 수 없습니다.")
        if (date_to - date_from).days + 1 > MAX_USAGE_LOG_RANGE_DAYS:
            raise HTTPException(
                status_code=422,
                detail=f"조회 기간은 최대 {MAX_USAGE_LOG_RANGE_DAYS}일입니다.",
            )
        try:
            decoded_cursor = decode_usage_log_cursor(cursor) if cursor else None
        except InvalidUsageLogCursor as exc:
            raise HTTPException(status_code=400, detail="cursor가 올바르지 않습니다.") from exc
        page = repository.fetch_logs(
            UsageLogFilters(
                date_from=date_from,
                date_to=date_to,
                user_id=user_id,
                department=department.strip() if department else None,
                endpoint=endpoint.strip() if endpoint else None,
                http_status=http_status,
                page_size=page_size,
                cursor=decoded_cursor,
            )
        )
        return {
            "items": list(page.items),
            "next_cursor": page.next_cursor,
            "has_more": page.has_more,
        }

    return router
