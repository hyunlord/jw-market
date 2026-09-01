from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from jw_chat_agent_poc.service.v4.lossless_contracts import EvidenceRecord

_FULL_DATE_RE = re.compile(r"(?<!\d)(\d{4})[-./]?(\d{2})[-./]?(\d{2})(?!\d)")
_MONTH_DATE_RE = re.compile(r"(?<!\d)(\d{4})[-./](\d{1,2})(?![-./]?\d)")
_YEAR_DATE_RE = re.compile(r"(?<!\d)(\d{4})(?!\d)")
_COMPLETED_STATUSES = {"COMPLETED", "완료"}
_ACTIVE_STATUSES = {
    "RECRUITING",
    "ACTIVE_NOT_RECRUITING",
    "NOT_YET_RECRUITING",
    "ENROLLING_BY_INVITATION",
    "진행",
    "모집중",
}


def normalize_surface_dates(value: object) -> str:
    """Render full date tokens as ISO dates without mutating source values."""
    raw = str(value or "").strip()

    def replace(match: re.Match[str]) -> str:
        try:
            return date(*(int(part) for part in match.groups())).isoformat()
        except ValueError:
            return match.group(0)

    return _FULL_DATE_RE.sub(replace, raw)


def clinical_time_axis(
    records: Sequence[EvidenceRecord],
    observed_on: date,
) -> dict[str, Any]:
    completed: list[dict[str, Any]] = []
    active_progress: list[dict[str, Any]] = []
    future_milestones: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []
    imprecise_date_count = 0
    missing_date_count = 0
    tracked_fields = (
        "start_date",
        "primary_completion_date",
        "completion_date",
        "last_update_date",
    )

    for record in records:
        payload = record.payload
        parsed: dict[str, date | None] = {}
        for field in tracked_fields:
            value = _value(payload, field, _clinical_date_alias(field))
            parsed_date, precision = _parse_date(value)
            parsed[field] = parsed_date if precision == "day" else None
            if precision in {"month", "year", "invalid"}:
                imprecise_date_count += 1
            elif precision == "missing":
                missing_date_count += 1

        status = _text(payload, "overall_status", "status").upper()
        title = _text(payload, "brief_title", "title") or "시험명 원천 미제공"
        nct_id = _text(payload, "nct_id") or record.evidence_id
        last_update = parsed["last_update_date"]
        if last_update is not None:
            updates.append(
                {
                    "evidence_id": record.evidence_id,
                    "nct_id": nct_id,
                    "title": title,
                    "last_update_date": last_update.isoformat(),
                    "_date": last_update,
                }
            )
        completion_date = parsed["completion_date"] or parsed[
            "primary_completion_date"
        ]
        if status in _COMPLETED_STATUSES and completion_date is not None:
            completed.append(
                {
                    "evidence_id": record.evidence_id,
                    "nct_id": nct_id,
                    "title": title,
                    "completion_date": completion_date.isoformat(),
                    "_date": completion_date,
                }
            )

        primary_completion = parsed["primary_completion_date"]
        start = parsed["start_date"]
        if status in _ACTIVE_STATUSES and start and primary_completion:
            total_days = (primary_completion - start).days
            if total_days > 0:
                elapsed_days = (observed_on - start).days
                progress = round(
                    min(max(elapsed_days / total_days * 100, 0.0), 100.0),
                    1,
                )
                active_progress.append(
                    {
                        "evidence_id": record.evidence_id,
                        "nct_id": nct_id,
                        "title": title,
                        "status": status,
                        "start_date": start.isoformat(),
                        "primary_completion_date": primary_completion.isoformat(),
                        "progress_pct": progress,
                    }
                )
        if primary_completion and primary_completion > observed_on:
            future_milestones.append(
                {
                    "evidence_id": record.evidence_id,
                    "nct_id": nct_id,
                    "title": title,
                    "status": status,
                    "primary_completion_date": primary_completion.isoformat(),
                    "_date": primary_completion,
                }
            )

    recent_cutoff = _years_before(observed_on, 3)
    completed.sort(key=lambda item: (item["_date"], item["nct_id"]), reverse=True)
    active_progress.sort(
        key=lambda item: (item["primary_completion_date"], item["nct_id"])
    )
    future_milestones.sort(key=lambda item: (item["_date"], item["nct_id"]))
    updates.sort(key=lambda item: (item["_date"], item["nct_id"]), reverse=True)
    recent_count = sum(item["_date"] >= recent_cutoff for item in completed)
    completed_total = len(completed)
    return {
        "reference_date": observed_on.isoformat(),
        "completed_total": completed_total,
        "recent_completed_count": recent_count,
        "recent_completed_ratio_pct": (
            round(recent_count / completed_total * 100, 1) if completed_total else None
        ),
        "latest_completed": _without_private(completed[0]) if completed else {},
        "latest_update": _without_private(updates[0]) if updates else {},
        "active_progress": tuple(active_progress[:5]),
        "future_milestones": tuple(
            _without_private(item) for item in future_milestones[:5]
        ),
        "imprecise_date_count": imprecise_date_count,
        "missing_date_count": missing_date_count,
    }


def patent_time_axis(
    records: Sequence[EvidenceRecord],
    observed_on: date,
) -> dict[str, Any]:
    expirations: list[dict[str, Any]] = []
    imprecise_date_count = 0
    for record in records:
        raw = _value(
            record.payload,
            "expiration_date",
            "expiry_date",
            "EXPRY_DATE",
        )
        expires, precision = _parse_date(raw)
        if precision in {"month", "year", "invalid"}:
            imprecise_date_count += 1
        if precision != "day" or expires is None:
            continue
        status = _text(
            record.payload,
            "status",
            "listed_status",
            "PATENT_STATUS",
        )
        remaining_months = _whole_months(observed_on, expires)
        is_expired = expires < observed_on
        expirations.append(
            {
                "evidence_id": record.evidence_id,
                "patent_no": _text(
                    record.payload,
                    "patent_no",
                    "DOMESTIC_PATENT_NO",
                ),
                "patent_type": _text(record.payload, "patent_type")
                or "원천 미제공",
                "status": status or "원천 미제공",
                "expiration_date": expires.isoformat(),
                "remaining_months": max(remaining_months, 0),
                "elapsed_months": abs(remaining_months) if is_expired else 0,
                "is_expired": is_expired,
                "_date": expires,
            }
        )
    expirations.sort(key=lambda item: (item["_date"], item["patent_no"]), reverse=True)
    active_count = sum(
        item["status"] == "등록" and item["_date"] >= observed_on
        for item in expirations
    )
    material_expirations = [
        item
        for item in expirations
        if "물질" in str(item["patent_type"])
        or "substance" in str(item["patent_type"]).casefold()
    ]
    return {
        "reference_date": observed_on.isoformat(),
        "active_count": active_count,
        "expired_count": sum(item["_date"] < observed_on for item in expirations),
        "longest_expiration": _without_private(expirations[0]) if expirations else {},
        "material_expiration": (
            _without_private(material_expirations[0])
            if material_expirations
            else {}
        ),
        "expirations": tuple(_without_private(item) for item in expirations),
        "imprecise_date_count": imprecise_date_count,
    }


def nedrug_time_axis(
    records: Sequence[EvidenceRecord],
    observed_on: date,
) -> dict[str, Any]:
    approvals: list[dict[str, Any]] = []
    reexaminations: list[dict[str, Any]] = []
    changes: list[dict[str, Any]] = []
    imprecise_date_count = 0
    for record in records:
        payload = record.payload
        item_name = _text(payload, "item_name", "ITEM_NAME") or record.evidence_id
        approval, approval_precision = _parse_date(
            _value(payload, "approval_date", "ITEM_PERMIT_DATE")
        )
        if approval_precision in {"month", "year", "invalid"}:
            imprecise_date_count += 1
        if approval is not None and approval_precision == "day":
            approvals.append(
                {
                    "evidence_id": record.evidence_id,
                    "item_name": item_name,
                    "approval_date": approval.isoformat(),
                    "elapsed_years": _elapsed_years(approval, observed_on),
                    "_date": approval,
                }
            )

        reexam_raw = _value(
            payload,
            "reexam_date",
            "REEXAM_DATE",
            "pms_period_raw",
        )
        reexam_end, reexam_precision = _parse_last_date(reexam_raw)
        if reexam_precision in {"month", "year", "invalid"}:
            imprecise_date_count += 1
        if reexam_end is not None and reexam_precision == "day":
            remaining_months = _whole_months(observed_on, reexam_end)
            is_expired = reexam_end < observed_on
            reexaminations.append(
                {
                    "evidence_id": record.evidence_id,
                    "item_name": item_name,
                    "reexam_end_date": reexam_end.isoformat(),
                    "remaining_months": max(remaining_months, 0),
                    "elapsed_months": abs(remaining_months) if is_expired else 0,
                    "is_expired": is_expired,
                    "_date": reexam_end,
                }
            )

        change, change_precision = _parse_date(
            _value(
                payload,
                "change_date",
                "CHANGE_DATE",
                "CHNG_DATE",
                "last_change_date",
            )
        )
        if change_precision in {"month", "year", "invalid"}:
            imprecise_date_count += 1
        if change is not None and change_precision == "day":
            changes.append(
                {
                    "evidence_id": record.evidence_id,
                    "item_name": item_name,
                    "change_date": change.isoformat(),
                    "_date": change,
                }
            )

    approvals.sort(key=lambda item: (item["_date"], item["item_name"]), reverse=True)
    reexaminations.sort(
        key=lambda item: (item["_date"], item["item_name"]), reverse=True
    )
    changes.sort(key=lambda item: (item["_date"], item["item_name"]), reverse=True)
    approvals = _deduplicate_temporal_items(
        approvals,
        key_fields=("item_name", "approval_date"),
    )
    reexaminations = _deduplicate_temporal_items(
        reexaminations,
        key_fields=("item_name", "reexam_end_date"),
    )
    changes = _deduplicate_temporal_items(
        changes,
        key_fields=("item_name", "change_date"),
    )
    return {
        "reference_date": observed_on.isoformat(),
        "approvals": tuple(_without_private(item) for item in approvals),
        "reexaminations": tuple(
            _without_private(item) for item in reexaminations
        ),
        "latest_changes": tuple(_without_private(item) for item in changes[:2]),
        "imprecise_date_count": imprecise_date_count,
    }


def _deduplicate_temporal_items(
    items: Sequence[dict[str, Any]],
    *,
    key_fields: tuple[str, ...],
) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    for item in items:
        key = tuple(str(item.get(field) or "") for field in key_fields)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _clinical_date_alias(field: str) -> str:
    return {
        "start_date": "study_start_date",
        "primary_completion_date": "primary_completion",
        "completion_date": "study_completion_date",
        "last_update_date": "last_update_post_date",
    }[field]


def _parse_last_date(value: object) -> tuple[date | None, str]:
    raw = str(value or "").strip()
    matches = tuple(_FULL_DATE_RE.finditer(raw))
    if matches:
        match = matches[-1]
        try:
            return date(*(int(part) for part in match.groups())), "day"
        except ValueError:
            return None, "invalid"
    return _parse_date(raw)


def _parse_date(value: object) -> tuple[date | None, str]:
    raw = str(value or "").strip()
    if not raw:
        return None, "missing"
    match = _FULL_DATE_RE.search(raw)
    if match:
        try:
            return date(*(int(part) for part in match.groups())), "day"
        except ValueError:
            return None, "invalid"
    match = _MONTH_DATE_RE.search(raw)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2)), 1), "month"
        except ValueError:
            return None, "invalid"
    match = _YEAR_DATE_RE.search(raw)
    if match:
        return date(int(match.group(1)), 1, 1), "year"
    return None, "invalid"


def _years_before(value: date, years: int) -> date:
    try:
        return value.replace(year=value.year - years)
    except ValueError:
        return value.replace(year=value.year - years, day=28)


def _elapsed_years(start: date, end: date) -> int:
    return max(
        0,
        end.year
        - start.year
        - ((end.month, end.day) < (start.month, start.day)),
    )


def _whole_months(start: date, end: date) -> int:
    months = (end.year - start.year) * 12 + end.month - start.month
    if end.day < start.day:
        months -= 1
    return months


def _value(payload: Mapping[str, Any], *keys: str) -> object:
    return next((payload[key] for key in keys if payload.get(key) not in (None, "")), "")


def _text(payload: Mapping[str, Any], *keys: str) -> str:
    return str(_value(payload, *keys)).strip()


def _without_private(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if not key.startswith("_")}
