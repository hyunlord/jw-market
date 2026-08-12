from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal


PatentLaneName = Literal["kr_primary", "us_secondary", "news"]


def build_patent_lane_payload(
    *,
    kr_calls: Sequence[Mapping[str, Any]],
    us_calls: Sequence[Mapping[str, Any]],
    news_calls: Sequence[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    kr_records, kr_received = _patent_records(kr_calls, lane="kr_primary")
    us_records, us_received = _patent_records(us_calls, lane="us_secondary")
    news_records, news_received = _news_records(news_calls)
    return {
        "kr_primary": _lane(
            scope="KR_PRIMARY",
            authority="식품의약품안전처 의약품 특허목록",
            role="국내 특허 상태의 1차 근거",
            records=kr_records,
            records_received=kr_received,
        ),
        "us_secondary": _lane(
            scope="US_REFERENCE_ONLY",
            authority="FDA Orange Book",
            role="미국 등재 특허의 보조 근거이며 국내 특허 상태와 혼합하지 않음",
            records=us_records,
            records_received=us_received,
        ),
        "news": _lane(
            scope="CONTEXT_ONLY",
            authority="Tavily web search",
            role="최근 보도 맥락이며 법적 특허 상태의 근거로 사용하지 않음",
            records=news_records,
            records_received=news_received,
        ),
    }


def _patent_records(
    calls: Sequence[Mapping[str, Any]],
    *,
    lane: Literal["kr_primary", "us_secondary"],
) -> tuple[list[dict[str, Any]], int]:
    received: list[dict[str, Any]] = []
    for call in calls:
        for item in _items(call):
            received.append(_patent_record(call, item, lane=lane))
    return (
        _deduplicate(received, keys=("patent_no", "product", "expiration_date", "owner")),
        len(received),
    )


def _patent_record(
    call: Mapping[str, Any],
    item: Mapping[str, Any],
    *,
    lane: Literal["kr_primary", "us_secondary"],
) -> dict[str, Any]:
    if lane == "kr_primary":
        product = item.get("ITEM_NAME")
        ingredient = item.get("INGR_ENG_NAME") or item.get("INGR_NAME")
        patent_no = item.get("DOMESTIC_PATENT_NO")
        invention_title = item.get("INVENTION_TITLE") or item.get("PATENT_TITLE")
        status = item.get("DOMESTIC_PATENT_STATUS")
        expiration_date = item.get("DOMESTIC_END_DATE")
        owner = item.get("PATENTEE")
    else:
        product = item.get("PRT_NAME")
        ingredient = item.get("INGR_NAME")
        patent_no = item.get("KOR_PAT_NO")
        invention_title = (
            item.get("KOR_INVENTION_TITLE")
            or item.get("INVENTION_TITLE")
            or item.get("PATENT_TITLE")
        )
        status = item.get("KOR_STATUS")
        expiration_date = item.get("KOR_EXP_DATE")
        owner = item.get("KOR_APPLICANT")
    return {
        "lane": lane,
        "jurisdiction": "KR" if lane == "kr_primary" else "US",
        "source": str(call.get("source") or call.get("tool") or ""),
        "tool": str(call.get("tool") or ""),
        "product": _text(product),
        "ingredient": _text(ingredient),
        "patent_no": _text(patent_no),
        "invention_title": _text(invention_title),
        "listed_status": _text(status),
        "status": _text(status),
        "expiration_date": _text(expiration_date),
        "owner": _text(owner),
        "url": _text(call.get("safe_url")),
        "source_record": dict(item),
    }


def _news_records(
    calls: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    received: list[dict[str, Any]] = []
    for call in calls:
        for item in _items(call):
            received.append(
                {
                    "lane": "news",
                    "source": str(call.get("source") or call.get("tool") or ""),
                    "tool": str(call.get("tool") or ""),
                    "title": _text(item.get("title")),
                    "url": _text(item.get("url") or call.get("safe_url")),
                    "snippet": _text(item.get("snippet") or item.get("content")),
                    "event_date": _text(
                        item.get("event_date") or item.get("eventDate")
                    ),
                    "published_at": _text(
                        item.get("published_at")
                        or item.get("published_date")
                        or item.get("publishedAt")
                    ),
                    "source_record": dict(item),
                }
            )
    return _deduplicate(received, keys=("url", "title", "snippet")), len(received)


def _items(call: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    render_data = call.get("render_data")
    if not isinstance(render_data, Mapping):
        return []
    for key in ("items", "results"):
        values = render_data.get(key)
        if isinstance(values, list):
            return [item for item in values if isinstance(item, Mapping)]
    payload = render_data.get("payload")
    if isinstance(payload, Mapping):
        for key in ("items", "results"):
            values = payload.get(key)
            if isinstance(values, list):
                return [item for item in values if isinstance(item, Mapping)]
    return []


def _lane(
    *,
    scope: str,
    authority: str,
    role: str,
    records: list[dict[str, Any]],
    records_received: int,
) -> dict[str, Any]:
    return {
        "scope": scope,
        "authority": authority,
        "role": role,
        "records_received": records_received,
        "records_unique": len(records),
        "records": records,
    }


def _deduplicate(
    records: Sequence[dict[str, Any]],
    *,
    keys: Sequence[str],
) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()
    for record in records:
        identity = tuple(_text(record.get(key)).casefold() for key in keys)
        if identity in seen:
            continue
        seen.add(identity)
        unique.append(record)
    return unique


def _text(value: Any) -> str:
    return str(value).strip() if value not in (None, "") else ""
