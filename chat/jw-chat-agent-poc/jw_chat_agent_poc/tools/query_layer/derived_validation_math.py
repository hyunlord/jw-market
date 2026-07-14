from __future__ import annotations

from typing import Any


def ends(values: tuple[Any, ...]) -> tuple[Any, Any]:
    return (values[0], values[-1]) if values else (None, None)


def growth(start: float | None, end: float | None) -> float | None:
    if start in {None, 0} or end is None:
        return None
    return (float(end) / float(start) - 1) * 100


def delta(start: float | None, end: float | None) -> float | None:
    if start is None or end is None:
        return None
    return float(end) - float(start)


def compound(
    start: float | None,
    end: float | None,
    elapsed: int | None,
    target: int,
) -> float | None:
    if start in {None, 0} or end is None or not elapsed:
        return None
    return ((float(end) / float(start)) ** (target / elapsed) - 1) * 100


def elapsed_months(start: str, end: str) -> int | None:
    if len(start) == len(end) == 7 and start[4] == end[4] == "-":
        return (int(end[:4]) - int(start[:4])) * 12 + int(end[5:7]) - int(start[5:7])
    if len(start) == len(end) == 7 and start[4:6] == end[4:6] == "-Q":
        return ((int(end[:4]) - int(start[:4])) * 4 + int(end[-1]) - int(start[-1])) * 3
    return None


def shift_month(period: str) -> str:
    if len(period) != 7 or period[4] != "-":
        return ""
    year, month = int(period[:4]), int(period[5:7])
    return f"{year - 1}-12" if month == 1 else f"{year:04d}-{month - 1:02d}"


def shift_year(period: str) -> str:
    return f"{int(period[:4]) - 1}{period[4:]}" if len(period) == 7 else ""


def terminal_streak(values: tuple[float, ...]) -> tuple[str | None, int]:
    if len(values) < 2:
        return None, 0
    direction = "up" if values[-1] > values[-2] else "down" if values[-1] < values[-2] else None
    if direction is None:
        return None, 0
    count = 1
    for left, right in zip(reversed(values[:-1]), reversed(values[1:]), strict=True):
        if (direction == "up" and right > left) or (direction == "down" and right < left):
            count += 1
        else:
            break
    return direction, count


def turning_point(values: tuple[tuple[str, float], ...]) -> tuple[str | None, str | None]:
    for index in range(1, len(values) - 1):
        previous = values[index - 1][1]
        current = values[index][1]
        following = values[index + 1][1]
        if current < previous and current < following:
            return values[index][0], "low"
        if current > previous and current > following:
            return values[index][0], "high"
    return None, None
