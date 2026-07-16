from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
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


def _request_pair(
    candidate_url: str,
    reference_url: str,
    case: MatrixCase,
    timeout_seconds: float,
    requester: Requester,
) -> dict[str, object]:
    candidate = requester(candidate_url, case, timeout_seconds)
    reference = requester(reference_url, case, timeout_seconds)
    return _observation(case, candidate, reference)


def _search_case(brand: str) -> MatrixCase:
    return MatrixCase(
        identifier=f"brand_search:{brand}",
        method="GET",
        path=f"/api/brands?{urlencode({'q': brand})}",
    )


def collect_latency_matrix_evidence(
    candidate_url: str,
    reference_url: str,
    *,
    timeout_seconds: float,
    max_workers: int,
    requester: Requester = request_case,
    edge_brands: Sequence[str] = EDGE_BRANDS,
) -> dict[str, object]:
    brands_case = MatrixCase(identifier="brands", method="GET", path="/api/brands")
    market_status_case = MatrixCase(identifier="market_status", method="GET", path="/api/market-status")
    reference_brands_response = requester(reference_url, brands_case, timeout_seconds)
    reference_brands = _json(reference_brands_response, "reference brands")
    requested_brands = tuple(dict.fromkeys((*_brand_names(reference_brands), *edge_brands, *REQUIRED_CD_BRANDS)))

    search_cases = tuple(_search_case(brand) for brand in requested_brands)
    search_payloads: dict[str, object] = {}
    search_observations: list[dict[str, object]] = []
    for brand, case in zip(requested_brands, search_cases, strict=True):
        candidate = requester(candidate_url, case, timeout_seconds)
        reference = requester(reference_url, case, timeout_seconds)
        search_observations.append(_observation(case, candidate, reference))
        search_payloads[brand] = _json(reference, f"reference brand search {brand}")

    cases = build_latency_matrix_cases(
        reference_brands,
        search_payloads,
        requested_brands=requested_brands,
        required_cd_brands=REQUIRED_CD_BRANDS,
        group_scopes=REQUIRED_GROUP_SCOPES,
    )
    initial = [
        _observation(
            brands_case,
            requester(candidate_url, brands_case, timeout_seconds),
            reference_brands_response,
        ),
        _request_pair(candidate_url, reference_url, market_status_case, timeout_seconds, requester),
        *search_observations,
    ]
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(
                _request_pair,
                candidate_url,
                reference_url,
                case,
                timeout_seconds,
                requester,
            )
            for case in cases
        ]
        observations = [future.result() for future in as_completed(futures)]
    observations.extend(initial)
    observations.sort(key=lambda item: str(item["id"]))
    expected_cases = sorted({"brands", "market_status", *(case.identifier for case in search_cases), *(case.identifier for case in cases)})
    return {
        "classification": "census",
        "provenance": LATENCY_MATRIX_PROVENANCE,
        "requested_brands": list(requested_brands),
        "resolved_brands": list(resolved_brand_names(search_payloads, requested_brands)),
        "required_cd_brands": list(REQUIRED_CD_BRANDS),
        "required_group_scopes": [list(scope) for scope in REQUIRED_GROUP_SCOPES],
        "expected_cases": expected_cases,
        "observations": observations,
    }
