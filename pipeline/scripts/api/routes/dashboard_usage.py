from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, HTTPException, Query

from pipeline.scripts.api.dashboard_usage import (
    MAX_RANGE_DAYS,
    UsageFilters,
    UsageStatsService,
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
