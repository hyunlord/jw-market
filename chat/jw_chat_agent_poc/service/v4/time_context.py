from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo


KST = ZoneInfo("Asia/Seoul")


def current_kst_date() -> date:
    return datetime.now(KST).date()


def datetime_kst_date(value: datetime) -> date:
    if value.tzinfo is None:
        return value.replace(tzinfo=KST).date()
    return value.astimezone(KST).date()


def as_of_date_instruction(observed_on: date) -> str:
    return (
        f"오늘은 {observed_on.isoformat()}이다. 최근 N년·요즘·현재 등 상대 시간 표현은 "
        "이 날짜 기준으로 해석한다."
    )
