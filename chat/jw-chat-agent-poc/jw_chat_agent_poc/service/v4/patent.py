from __future__ import annotations

from collections.abc import Mapping, Sequence
import re
from typing import Any, Literal

from jw_chat_agent_poc.service.v4.source_labels import patent_lane_label


PatentLaneName = Literal["kr_primary", "us_secondary", "news"]
_GENERIC_RELEVANCE_TOKENS = frozenset(
    {
        "calcium",
        "chemical",
        "company",
        "corporation",
        "hydrate",
        "hydrochloride",
        "limited",
        "pharma",
        "pharmaceutical",
        "sodium",
        "뉴스",
        "만료",
        "최근",
        "제약",
        "주식회사",
        "특허",
        "특허현황",
    }
)


def build_patent_lane_payload(
    *,
    kr_calls: Sequence[Mapping[str, Any]],
    us_calls: Sequence[Mapping[str, Any]],
    news_calls: Sequence[Mapping[str, Any]],
    entity_tokens: Sequence[str] = (),
    required_ingredients: Sequence[str] = (),
) -> dict[str, dict[str, Any]]:
    (
        kr_records,
        kr_received,
        kr_source_limit,
        kr_source_limit_reached,
        kr_identifier_exclusions,
        kr_product_patent_rows,
        kr_non_product_exclusions,
    ) = _patent_records(kr_calls, lane="kr_primary")
    (
        us_records,
        us_received,
        us_source_limit,
        us_source_limit_reached,
        us_identifier_exclusions,
        _,
        _,
    ) = _patent_records(us_calls, lane="us_secondary")
    us_relevance_exclusions = sum(
        not _contains_required_ingredients(record, required_ingredients)
        for record in us_records
    )
    us_records = [
        record
        for record in us_records
        if _contains_required_ingredients(record, required_ingredients)
    ]
    relevance_tokens, company_tokens = _relevance_tokens(
        entity_tokens,
        (*kr_records, *us_records),
    )
    news_records, news_received, relevance_decisions = _news_records(
        news_calls,
        relevance_tokens=relevance_tokens,
        company_tokens=company_tokens,
    )
    news_lane = _lane(
        scope="CONTEXT_ONLY",
        authority="Tavily web search",
        role="최근 보도 맥락이며 법적 특허 상태의 근거로 사용하지 않음",
        records=news_records,
        records_received=news_received,
    )
    news_lane["relevance_decisions"] = relevance_decisions
    us_lane = _lane(
        scope="US_REFERENCE_ONLY",
        authority="FDA Orange Book",
        role="미국 등재 특허의 보조 근거이며 국내 특허 상태와 혼합하지 않음",
        records=us_records,
        records_received=us_received,
        source_limit=us_source_limit,
        source_limit_reached=us_source_limit_reached,
        identifier_exclusions=us_identifier_exclusions,
    )
    us_lane["relevance_exclusions"] = us_relevance_exclusions
    kr_lane = _lane(
            scope="KR_PRIMARY",
            authority=patent_lane_label("kr_primary"),
            role="국내 특허 상태의 1차 근거",
            records=kr_records,
            records_received=kr_received,
            source_limit=kr_source_limit,
            source_limit_reached=kr_source_limit_reached,
            identifier_exclusions=kr_identifier_exclusions,
            product_patent_rows=kr_product_patent_rows,
            non_product_exclusions=kr_non_product_exclusions,
        )
    kr_lane["product_patent_edges"] = _product_patent_edges(kr_calls)
    kr_lane["pms_periods"] = _product_pms_periods(kr_calls)
    return {
        "kr_primary": kr_lane,
        "us_secondary": us_lane,
        "news": news_lane,
    }


def _contains_required_ingredients(
    record: Mapping[str, Any],
    required_ingredients: Sequence[str],
) -> bool:
    required = tuple(
        value.casefold()
        for value in (_text(ingredient) for ingredient in required_ingredients)
        if value
    )
    if len(required) < 2:
        return True
    ingredient = _text(record.get("ingredient")).casefold()
    return all(value in ingredient for value in required)


def _patent_records(
    calls: Sequence[Mapping[str, Any]],
    *,
    lane: Literal["kr_primary", "us_secondary"],
) -> tuple[list[dict[str, Any]], int, int | None, bool, int, int, int]:
    received: list[dict[str, Any]] = []
    source_limits: list[int] = []
    source_limit_reached = False
    for call in calls:
        items = _items(call)
        render_data = call.get("render_data")
        if isinstance(render_data, Mapping):
            source_limit = _integer(
                render_data.get("request_limit")
                or mapping(render_data.get("request")).get("limit")
            )
            if source_limit is not None:
                source_limits.append(source_limit)
                source_limit_reached = source_limit_reached or (
                    bool(render_data.get("source_limit_reached"))
                    or (lane == "kr_primary" and len(items) >= source_limit)
                )
        for item in items:
            received.append(_patent_record(call, item, lane=lane))
    if lane == "kr_primary":
        product_patents = [
            record
            for record in received
            if _text(record.get("page_group")) == "제품특허"
        ]
        identified = [
            record for record in product_patents if _text(record.get("patent_no"))
        ]
        unique = _deduplicate_patent_numbers(identified)
        identifier_exclusions = len(product_patents) - len(identified)
        product_patent_rows = len(product_patents)
        non_product_exclusions = len(received) - product_patent_rows
    else:
        unique = _deduplicate(
            received,
            keys=("patent_no", "product", "expiration_date", "owner"),
        )
        identifier_exclusions = 0
        product_patent_rows = 0
        non_product_exclusions = 0
    return (
        unique,
        len(received),
        max(source_limits) if source_limits else None,
        source_limit_reached,
        identifier_exclusions,
        product_patent_rows,
        non_product_exclusions,
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
        invention_title = (
            item.get("DOMESTIC_INVN_NM")
            or item.get("INVENTION_TITLE")
            or item.get("PATENT_TITLE")
        )
        patent_type = item.get("PATENT_GB_CODE")
        page_group = item.get("PAGE_GB_NM")
        status = item.get("DOMESTIC_PATENT_STATUS")
        expiration_date = item.get("DOMESTIC_END_DATE")
        owner = item.get("PATENTEE")
        product_item_seq = item.get("ITEM_SEQ")
        product_item_name = item.get("ITEM_NAME")
        pms_period_start, pms_period_end = _pms_period(item.get("PMS_END_DATE"))
        authority = "KR_LISTED_PATENT"
    else:
        product = item.get("PRT_NAME")
        ingredient = item.get("INGR_NAME")
        patent_no = item.get("KOR_PAT_NO")
        invention_title = (
            item.get("KOR_INVENTION_TITLE")
            or item.get("KOR_NAME_OF_INVENTION")
            or item.get("INVENTION_TITLE")
            or item.get("PATENT_TITLE")
        )
        status = item.get("KOR_STATUS")
        expiration_date = item.get("KOR_EXP_DATE")
        owner = item.get("KOR_APPLICANT")
        patent_type = item.get("PATENT_GB_CODE")
        page_group = ""
        product_item_seq = ""
        product_item_name = product
        pms_period_start = ""
        pms_period_end = ""
        authority = "US_ORANGE_BOOK"
    extinction_reason = _extinction_reason(status)
    return {
        "lane": lane,
        "jurisdiction": "KR" if lane == "kr_primary" else "US",
        "authority": authority,
        "source": str(call.get("source") or call.get("tool") or ""),
        "tool": str(call.get("tool") or ""),
        "product": _text(product),
        "ingredient": _text(ingredient),
        "patent_no": _text(patent_no),
        "invention_title": _text(invention_title),
        "patent_type": _text(patent_type),
        "page_group": _text(page_group),
        "listed_status": _text(status),
        "status": _text(status),
        "extinction_reason": extinction_reason,
        "event_type": _patent_event_type(extinction_reason),
        "listed_end_date": _text(expiration_date),
        "expiration_date": _text(expiration_date),
        "product_item_seq": _text(product_item_seq),
        "product_item_name": _text(product_item_name),
        "pms_period_start": pms_period_start,
        "pms_period_end": pms_period_end,
        "owner": _text(owner),
        "url": _text(call.get("safe_url")),
        "source_record": dict(item),
    }


def _extinction_reason(value: Any) -> str:
    match = re.search(r"소멸\s*\(([^)]+)\)", _text(value))
    return match.group(1).strip() if match else ""


def _patent_event_type(reason: str) -> str:
    normalized = "".join(reason.split())
    if normalized == "존속기간만료":
        return "PATENT_TERM_EXPIRED"
    if normalized == "무효":
        return "PATENT_INVALIDATED"
    if normalized == "등록료불납":
        return "PATENT_LAPSED_NONPAYMENT"
    return "UNKNOWN"


def _pms_period(value: Any) -> tuple[str, str]:
    match = re.fullmatch(
        r"\s*(\d{4}-\d{2}-\d{2})\s*[~～]\s*(\d{4}-\d{2}-\d{2})\s*",
        _text(value),
    )
    return match.groups() if match else ("", "")


def _product_patent_edges(
    calls: Sequence[Mapping[str, Any]],
) -> list[dict[str, str]]:
    edges: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for call in calls:
        for item in _items(call):
            if _text(item.get("PAGE_GB_NM")) != "제품특허":
                continue
            key = (_text(item.get("ITEM_SEQ")), _text(item.get("DOMESTIC_PATENT_NO")))
            if not all(key) or key in seen:
                continue
            seen.add(key)
            edges.append({"product_item_seq": key[0], "patent_no": key[1]})
    return edges


def _product_pms_periods(
    calls: Sequence[Mapping[str, Any]],
) -> list[dict[str, str]]:
    periods: list[dict[str, str]] = []
    seen: set[str] = set()
    for call in calls:
        for item in _items(call):
            item_seq = _text(item.get("ITEM_SEQ"))
            start, end = _pms_period(item.get("PMS_END_DATE"))
            if not item_seq or not start or not end or item_seq in seen:
                continue
            seen.add(item_seq)
            periods.append(
                {
                    "product_item_seq": item_seq,
                    "pms_period_start": start,
                    "pms_period_end": end,
                }
            )
    return periods


def _news_records(
    calls: Sequence[Mapping[str, Any]],
    *,
    relevance_tokens: Sequence[str],
    company_tokens: Sequence[str],
) -> tuple[list[dict[str, Any]], int, list[dict[str, Any]]]:
    received: list[dict[str, Any]] = []
    for call in calls:
        for item in _items(call):
            received.append(
                {
                    "lane": "news",
                    "authority": "NEWS",
                    "jurisdiction": "N/A",
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
    relevant: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    for record_index, record in enumerate(received):
        decision = _news_relevance_decision(
            record,
            record_index=record_index,
            relevance_tokens=relevance_tokens,
            company_tokens=company_tokens,
        )
        decisions.append(decision)
        if decision["decision"] == "keep":
            relevant.append(record)
    return (
        _deduplicate(relevant, keys=("url", "title", "snippet")),
        len(received),
        decisions,
    )


def _relevance_tokens(
    explicit: Sequence[str],
    patent_records: Sequence[Mapping[str, Any]],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    primary_candidates = [*explicit]
    company_candidates: list[str] = []
    for record in patent_records:
        primary_candidates.extend(
            _text(record.get(field)) for field in ("product", "ingredient")
        )
        company_candidates.append(_text(record.get("owner")))
    company_tokens = _normalized_relevance_tokens(company_candidates)
    primary_tokens = tuple(
        token
        for token in _normalized_relevance_tokens(primary_candidates)
        if token not in company_tokens
    )
    return primary_tokens, company_tokens


def _normalized_relevance_tokens(candidates: Sequence[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    for candidate in candidates:
        value = " ".join(_text(candidate).casefold().split())
        if (
            len(value) >= 2
            and value not in _GENERIC_RELEVANCE_TOKENS
            and value not in normalized
        ):
            normalized.append(value)
        for token in value.replace("/", " ").replace("+", " ").split():
            if (
                len(token) >= 3
                and token not in _GENERIC_RELEVANCE_TOKENS
                and token not in normalized
            ):
                normalized.append(token)
    return tuple(normalized)


def _news_relevance_decision(
    record: Mapping[str, Any],
    *,
    record_index: int,
    relevance_tokens: Sequence[str],
    company_tokens: Sequence[str],
) -> dict[str, Any]:
    surface = " ".join(
        (_text(record.get("title")), _text(record.get("snippet")))
    ).casefold()
    matched_primary = [token for token in relevance_tokens if token in surface]
    matched_company = [token for token in company_tokens if token in surface]
    keep = bool(matched_primary)
    reason = (
        "brand_or_ingredient_token"
        if keep
        else "company_token_only"
        if matched_company
        else "no_brand_or_ingredient_token"
    )
    return {
        "record_index": record_index,
        "decision": "keep" if keep else "discard",
        "reason": reason,
        "matched_brand_or_ingredient_tokens": matched_primary,
        "matched_company_tokens": matched_company,
    }


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
    source_limit: int | None = None,
    source_limit_reached: bool = False,
    identifier_exclusions: int = 0,
    product_patent_rows: int = 0,
    non_product_exclusions: int = 0,
) -> dict[str, Any]:
    return {
        "scope": scope,
        "authority": authority,
        "role": role,
        "records_received": records_received,
        "records_unique": len(records),
        "records": records,
        "source_limit": source_limit,
        "source_limit_reached": source_limit_reached,
        "identifier_exclusions": identifier_exclusions,
        "product_patent_rows": product_patent_rows,
        "non_product_exclusions": non_product_exclusions,
    }


def patent_record_sort_key(record: Mapping[str, Any]) -> tuple[Any, ...]:
    status = _text(record.get("status") or record.get("listed_status"))
    expiration = "".join(
        character
        for character in _text(record.get("expiration_date"))
        if character.isdigit()
    )
    completeness = sum(
        bool(_text(record.get(field)))
        for field in ("invention_title", "patent_type", "owner", "expiration_date")
    )
    return (
        0 if status == "등록" else 1,
        -int(expiration or "0"),
        -completeness,
        _text(record.get("patent_no")),
        _text(record.get("product")),
    )


def _deduplicate_patent_numbers(
    records: Sequence[dict[str, Any]],
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        key = _text(record.get("patent_no")).casefold()
        grouped.setdefault(key, []).append(record)
    unique: list[dict[str, Any]] = []
    for candidates in grouped.values():
        winner = dict(sorted(candidates, key=_patent_duplicate_sort_key)[0])
        variants = list(
            dict.fromkeys(
                _text(candidate.get("status"))
                for candidate in sorted(candidates, key=_patent_duplicate_sort_key)
                if _text(candidate.get("status"))
            )
        )
        winner["status_variants"] = variants
        winner["source_row_count"] = len(candidates)
        unique.append(winner)
    return unique


def _patent_duplicate_sort_key(record: Mapping[str, Any]) -> tuple[Any, ...]:
    status = _text(record.get("status") or record.get("listed_status"))
    status_rank = (
        0
        if status == "등록"
        else 1
        if "(" in status and ")" in status
        else 2
    )
    return (status_rank, *patent_record_sort_key(record))


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


def _integer(value: Any) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}
