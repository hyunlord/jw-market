from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import copy
import hashlib
import json
from typing import Any, Final
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from pipeline.scripts.gates.latency_matrix_cases import (
    LATENCY_MATRIX_PROVENANCE,
    build_latency_matrix_cases,
    normalize_brand_identity,
    resolved_brand_names,
)
from pipeline.scripts.gates.latency_matrix_required import REQUIRED_CD_BRANDS, REQUIRED_GROUP_SCOPES
from pipeline.scripts.gates.latency_matrix_types import MatrixCase


EDGE_BRANDS: Final[tuple[str, ...]] = ("아토젯", "마운자로")


@dataclass(frozen=True, slots=True)
class RawResponse:
    status: int
    body: bytes


Requester = Callable[[str, MatrixCase, float], RawResponse]


def request_case(base_url: str, case: MatrixCase, timeout_seconds: float) -> RawResponse:
    encoded = None
    if case.body is not None:
        encoded = json.dumps(case.body, ensure_ascii=False, separators=(",", ":")).encode()
    request = Request(
        base_url.rstrip("/") + case.path,
        data=encoded,
        method=case.method,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            return RawResponse(status=response.status, body=response.read())
    except HTTPError as exc:
        return RawResponse(status=exc.code, body=exc.read())
    except (TimeoutError, URLError, OSError) as exc:
        return RawResponse(status=0, body=str(exc).encode())


def _json(response: RawResponse, label: str) -> object:
    if response.status != 200:
        raise RuntimeError(f"{label} returned HTTP {response.status}")
    try:
        return json.loads(response.body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{label} returned invalid JSON") from exc


def _brand_names(payload: object) -> tuple[str, ...]:
    raw = payload.get("brands") if isinstance(payload, dict) else payload
    if not isinstance(raw, list):
        return ()
    names = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = str(item.get("brand") or item.get("name") or item.get("brand_name") or "").strip()
        if name:
            names.append(name)
    return tuple(dict.fromkeys(names))


def _normalized_body(case: MatrixCase, response: RawResponse) -> bytes:
    if not case.mask_generated_at:
        return response.body
    payload = copy.deepcopy(_json(response, case.identifier))
    if isinstance(payload, dict):
        payload.pop("generated_at", None)
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()


def _populated_topic_brands(case: MatrixCase, response: RawResponse) -> int | None:
    if not case.identifier.startswith("brand_activity_group:topics:") or response.status != 200:
        return None
    payload = _json(response, case.identifier)
    data = payload.get("data") if isinstance(payload, dict) else None
    brands = data.get("brands") if isinstance(data, dict) else None
    if not isinstance(brands, list):
        return 0
    return sum(
        1
        for brand in brands
        if isinstance(brand, dict)
        and (
            int(brand.get("event_count") or 0) > 0
            or bool(brand.get("topic_shares"))
            or bool(brand.get("brand_specific_topics"))
        )
    )


def _observation(case: MatrixCase, candidate: RawResponse, reference: RawResponse) -> dict[str, object]:
    candidate_body = _normalized_body(case, candidate) if candidate.status == 200 else candidate.body
    reference_body = _normalized_body(case, reference) if reference.status == 200 else reference.body
    observation: dict[str, object] = {
        "id": case.identifier,
        "candidate_status": candidate.status,
        "reference_status": reference.status,
        "candidate_sha256": hashlib.sha256(candidate_body).hexdigest(),
        "reference_sha256": hashlib.sha256(reference_body).hexdigest(),
        "parity": candidate.status == reference.status == 200 and candidate_body == reference_body,
    }
    candidate_populated = _populated_topic_brands(case, candidate)
    reference_populated = _populated_topic_brands(case, reference)
    if candidate_populated is not None:
        observation["candidate_populated_brands"] = candidate_populated
    if reference_populated is not None:
        observation["reference_populated_brands"] = reference_populated
    return observation


def _search_case(brand: str) -> MatrixCase:
    return MatrixCase(
        identifier=f"brand_search:{brand}",
        method="GET",
        path=f"/api/brands?{urlencode({'q': brand})}",
    )


def _reference_exclusion(case: MatrixCase, response: RawResponse) -> dict[str, object] | None:
    if not case.identifier.startswith("deep:") or response.status != 422:
        return None
    try:
        payload = json.loads(response.body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    detail = payload.get("detail") if isinstance(payload, dict) else None
    error = detail.get("error") if isinstance(detail, dict) else None
    if error != "source_not_available":
        return None
    return {
        "id": case.identifier,
        "reason": error,
        "reference_status": response.status,
    }


def collect_latency_matrix_evidence(
    candidate_url: str,
    reference_url: str,
    *,
    timeout_seconds: float,
    max_workers: int,
    requester: Requester = request_case,
    edge_brands: Sequence[str] = EDGE_BRANDS,
) -> dict[str, object]:
    if max_workers != 1:
        raise ValueError("latency matrix census requires max_workers=1")
    brands_case = MatrixCase(identifier="brands", method="GET", path="/api/brands")
    market_status_case = MatrixCase(identifier="market_status", method="GET", path="/api/market-status")
    reference_brands_response = requester(reference_url, brands_case, timeout_seconds)
    reference_brands = _json(reference_brands_response, "reference brands")
    default_brands = _brand_names(reference_brands)
    requested_brands = tuple(
        dict.fromkeys(
            (
                *default_brands,
                *edge_brands,
                *REQUIRED_CD_BRANDS,
                *(member for _option_id, member in REQUIRED_GROUP_SCOPES),
            )
        )
    )

    search_cases = tuple(_search_case(brand) for brand in requested_brands)
    search_payloads: dict[str, object] = {}
    search_observations: list[dict[str, object]] = []
    for brand, case in zip(requested_brands, search_cases, strict=True):
        reference = requester(reference_url, case, timeout_seconds)
        candidate = requester(candidate_url, case, timeout_seconds)
        search_observations.append(_observation(case, candidate, reference))
        search_payloads[brand] = _json(reference, f"reference brand search {brand}")

    context_resolved_brands = resolved_brand_names(search_payloads, requested_brands)
    default_identities = {normalize_brand_identity(brand) for brand in default_brands}
    resolved_brands = tuple(
        brand
        for brand in requested_brands
        if normalize_brand_identity(brand) in default_identities or brand in context_resolved_brands
    )
    default_only_brands = tuple(brand for brand in resolved_brands if brand not in context_resolved_brands)

    discovery_cases = build_latency_matrix_cases(
        reference_brands,
        search_payloads,
        requested_brands=requested_brands,
        required_cd_brands=REQUIRED_CD_BRANDS,
        group_scopes=REQUIRED_GROUP_SCOPES,
    )
    filter_option_payloads: dict[str, object] = {}
    prefetched: dict[str, tuple[RawResponse, RawResponse]] = {}
    for case in discovery_cases:
        if not case.identifier.startswith("filter_options:"):
            continue
        reference = requester(reference_url, case, timeout_seconds)
        candidate = requester(candidate_url, case, timeout_seconds)
        filter_option_payloads[case.identifier] = _json(reference, f"reference {case.identifier}")
        prefetched[case.identifier] = (candidate, reference)

    cases = build_latency_matrix_cases(
        reference_brands,
        search_payloads,
        requested_brands=requested_brands,
        required_cd_brands=REQUIRED_CD_BRANDS,
        group_scopes=REQUIRED_GROUP_SCOPES,
        filter_option_payloads=filter_option_payloads,
    )
    initial = [
        _observation(
            brands_case,
            requester(candidate_url, brands_case, timeout_seconds),
            reference_brands_response,
        ),
        *search_observations,
    ]
    reference_market_status = requester(reference_url, market_status_case, timeout_seconds)
    candidate_market_status = requester(candidate_url, market_status_case, timeout_seconds)
    initial.append(_observation(market_status_case, candidate_market_status, reference_market_status))

    observations: list[dict[str, object]] = []
    included_cases: list[MatrixCase] = []
    excluded_reference_cases: list[dict[str, object]] = []
    for case in cases:
        prefetched_response = prefetched.get(case.identifier)
        if prefetched_response is None:
            candidate = None
            reference = requester(reference_url, case, timeout_seconds)
        else:
            candidate, reference = prefetched_response
        exclusion = _reference_exclusion(case, reference)
        if exclusion is not None:
            excluded_reference_cases.append(exclusion)
            continue
        if candidate is None:
            candidate = requester(candidate_url, case, timeout_seconds)
        observations.append(_observation(case, candidate, reference))
        included_cases.append(case)
    observations.extend(initial)
    observations.sort(key=lambda item: str(item["id"]))
    expected_cases = sorted(
        {
            "brands",
            "market_status",
            *(case.identifier for case in search_cases),
            *(case.identifier for case in included_cases),
        }
    )
    return {
        "classification": "census",
        "provenance": LATENCY_MATRIX_PROVENANCE,
        "default_brands": list(default_brands),
        "requested_brands": list(requested_brands),
        "resolved_brands": list(resolved_brands),
        "context_resolved_brands": list(context_resolved_brands),
        "default_only_brands": list(default_only_brands),
        "excluded_reference_cases": excluded_reference_cases,
        "required_cd_brands": list(REQUIRED_CD_BRANDS),
        "required_group_scopes": [list(scope) for scope in REQUIRED_GROUP_SCOPES],
        "expected_cases": expected_cases,
        "observations": observations,
    }
