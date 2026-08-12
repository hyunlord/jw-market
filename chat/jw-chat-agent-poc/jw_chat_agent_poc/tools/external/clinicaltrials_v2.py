from __future__ import annotations

from dataclasses import dataclass
from time import monotonic
from typing import Any, Callable, Mapping, Protocol

import requests

from jw_chat_agent_poc.service.v4.clinical import (
    CompiledClinicalQuery,
    normalize_clinical_study,
)


CLINICALTRIALS_V2_STUDIES_URL = "https://clinicaltrials.gov/api/v2/studies"


class _Response(Protocol):
    def raise_for_status(self) -> None: ...

    def json(self) -> object: ...


@dataclass(frozen=True, slots=True)
class ClinicalSearchResult:
    records: tuple[dict[str, Any], ...]
    total_reported: int | None
    records_received: int
    records_unique: int
    page_count: int
    pagination_complete: bool
    partial_reason: str | None
    query_manifest: dict[str, Any]
    elapsed_ms: float


class ClinicalTrialsV2Client:
    def __init__(
        self,
        *,
        get: Callable[..., _Response] = requests.get,
        timeout_s: float = 12.0,
        record_cap: int = 1_000,
    ) -> None:
        if record_cap < 1:
            raise ValueError("record_cap must be positive")
        self._get = get
        self._timeout_s = timeout_s
        self._record_cap = record_cap

    def search(self, compiled: CompiledClinicalQuery) -> ClinicalSearchResult:
        started = monotonic()
        page_token: str | None = None
        seen_tokens: set[str] = set()
        received: list[Mapping[str, Any]] = []
        total_reported: int | None = None
        page_count = 0
        partial_reason: str | None = None

        while len(received) < self._record_cap:
            parameters = dict(compiled.parameters)
            parameters["pageSize"] = 100
            if page_token:
                parameters["pageToken"] = page_token
            response = self._get(
                CLINICALTRIALS_V2_STUDIES_URL,
                params=parameters,
                timeout=self._timeout_s,
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, Mapping):
                raise ValueError("ClinicalTrials API v2 response is not an object")
            page_count += 1
            if total_reported is None:
                total_reported = _optional_int(payload.get("totalCount"))
            studies = payload.get("studies")
            if not isinstance(studies, list):
                raise ValueError("ClinicalTrials API v2 response has no studies array")
            remaining = self._record_cap - len(received)
            received.extend(item for item in studies[:remaining] if isinstance(item, Mapping))

            next_token = str(payload.get("nextPageToken") or "").strip()
            if len(received) >= self._record_cap and next_token:
                partial_reason = _cap_reason(total_reported, self._record_cap)
                page_token = next_token
                break
            if not next_token:
                page_token = None
                break
            if next_token in seen_tokens:
                partial_reason = "ClinicalTrials API가 동일한 nextPageToken을 반복해 페이지 수집을 중단했습니다"
                page_token = next_token
                break
            seen_tokens.add(next_token)
            page_token = next_token

        matched_queries = compiled.concept.source_queries or (compiled.expression,)
        normalized = [
            normalize_clinical_study(study, matched_queries=matched_queries)
            for study in received
        ]
        unique_by_nct: dict[str, dict[str, Any]] = {}
        for record in normalized:
            nct_id = str(record.get("nct_id") or "").upper()
            if not nct_id:
                continue
            if nct_id not in unique_by_nct:
                unique_by_nct[nct_id] = record
                continue
            current = unique_by_nct[nct_id]
            current["matched_query"] = list(
                dict.fromkeys(
                    [
                        *list(current.get("matched_query") or []),
                        *list(record.get("matched_query") or []),
                    ]
                )
            )

        pagination_complete = page_token is None and (
            total_reported is None or len(received) >= total_reported
        )
        if not pagination_complete and partial_reason is None:
            partial_reason = (
                f"원천 검색 {total_reported}건 중 {len(received)}건만 수신되어 전체 현황으로 볼 수 없습니다"
                if total_reported is not None
                else "페이지 수집이 완료되지 않아 전체 현황으로 볼 수 없습니다"
            )

        manifest = {
            "query_id": compiled.query_id,
            "query_type": compiled.query_type,
            "compiled_expression": compiled.expression,
            "parameters": dict(compiled.parameters),
            "source_queries": list(matched_queries),
            "total_reported": total_reported,
            "records_received": len(received),
            "records_unique": len(unique_by_nct),
            "page_count": page_count,
            "pagination_complete": pagination_complete,
            "partial_reason": partial_reason,
        }
        return ClinicalSearchResult(
            records=tuple(unique_by_nct.values()),
            total_reported=total_reported,
            records_received=len(received),
            records_unique=len(unique_by_nct),
            page_count=page_count,
            pagination_complete=pagination_complete,
            partial_reason=partial_reason,
            query_manifest=manifest,
            elapsed_ms=round((monotonic() - started) * 1000, 1),
        )


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _cap_reason(total_reported: int | None, cap: int) -> str:
    total = total_reported if total_reported is not None else f"{cap}+"
    return f"원천 검색 {total}건 중 안전 상한 {cap}건만 수신한 부분 결과"
