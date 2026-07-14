from __future__ import annotations

import argparse
from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
from typing import Any, Callable, Sequence
from urllib.error import URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.scripts.gates.release_evidence import (
    BRAND_SOURCE_PROVENANCE,
    MARKET_GROWTH_PROVENANCE,
    SEGMENT_LEVELS,
    SEGMENT_PROVENANCE,
    collect_brand_source_evidence,
    collect_market_growth_evidence,
    collect_segment_sum_evidence,
    write_evidence,
)


STRICT_LOG_PATTERN = re.compile(r"Traceback|(?:^|\s)ERROR(?:\s|:|$)|(?:^|\s)5[0-9]{2}(?:\s|$)")
PERIOD_TOKEN = re.compile(r"^\d{4}(?:-(?:0[1-9]|1[0-2]|Q[1-4]))?$")
TRACKED_GOLDEN_CONTRACTS = ROOT / "tests" / "api" / "api_golden_contracts.json"


@dataclass(frozen=True)
class GateResult:
    gate: str
    classification: str
    checked: int
    population: int
    failures: int
    tolerance: str
    environment: str
    details: tuple[str, ...] = ()

    @property
    def exit_code(self) -> int:
        return 1 if self.failures else 0

    def render(self) -> str:
        fields = (
            f"gate={self.gate}",
            f"classification={self.classification}",
            f"checked={self.checked}",
            f"population={self.population}",
            "missing=fail",
            f"tolerance={self.tolerance}",
            f"failures={self.failures}",
            f"exit_code={self.exit_code}",
            f"environment={self.environment}",
        )
        return "\n".join((*self.details, *fields))


def _load_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def _canonical_sha(payload: object) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return hashlib.sha256(raw).hexdigest()


def _unique_by_id(items: object, *, label: str) -> dict[str, dict[str, Any]]:
    if not isinstance(items, list):
        raise ValueError(f"{label} must be a JSON array")
    indexed: dict[str, dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise ValueError(f"{label} entries require a string id")
        identifier = item["id"]
        if identifier in indexed:
            raise ValueError(f"duplicate {label} identity: {identifier}")
        indexed[identifier] = item
    return indexed


def _fetch_live_payload(contract: dict[str, Any], base_url: str, timeout_seconds: float) -> Any:
    request_spec = contract.get("request")
    if not isinstance(request_spec, dict):
        raise ValueError("contract request must be a JSON object")
    method = str(request_spec.get("method", "")).upper()
    path = request_spec.get("path")
    if method not in {"GET", "POST"} or not isinstance(path, str) or not path.startswith("/"):
        raise ValueError("contract request requires GET/POST and an absolute path")
    body = request_spec.get("body")
    data = None
    headers: dict[str, str] = {}
    if method == "POST":
        data = json.dumps(
            body,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        headers["Content-Type"] = "application/json"
    url = urljoin(f"{base_url.rstrip('/')}/", path.lstrip("/"))
    request = Request(url, data=data, headers=headers, method=method)
    with urlopen(request, timeout=timeout_seconds) as response:
        raw = response.read()
    if not raw.strip():
        raise ValueError("empty response body")
    payload = json.loads(raw)
    if payload is None or payload == {} or payload == []:
        raise ValueError("empty response payload")
    return payload


def check_goldens(
    contracts_path: Path,
    base_url: str,
    environment: str,
    timeout_seconds: float = 30.0,
) -> GateResult:
    contract_document = _load_json(contracts_path)
    if not isinstance(contract_document, dict):
        raise ValueError("contracts document must be a JSON object")
    contracts = _unique_by_id(contract_document.get("contracts"), label="contract")
    required_metadata = (
        "canonical_sha256",
        "request",
        "truth_basis",
        "measured_at",
        "database",
        "runtime_provenance",
    )
    details: list[str] = []
    failures = 0
    checked = 0

    for identifier, contract in contracts.items():
        absent_metadata = [field for field in required_metadata if not contract.get(field)]
        if absent_metadata:
            details.append(f"{identifier}: missing contract metadata {','.join(absent_metadata)}")
            failures += 1
            continue
        checked += 1
        try:
            payload = _fetch_live_payload(contract, base_url, timeout_seconds)
        except (URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            details.append(f"{identifier}: live request failed: {exc}")
            failures += 1
            continue
        actual = _canonical_sha(payload)
        expected = str(contract["canonical_sha256"])
        if actual != expected:
            details.append(f"{identifier}: canonical sha mismatch expected={expected} actual={actual}")
            failures += 1

    return GateResult(
        gate="api_goldens",
        classification="census",
        checked=checked,
        population=len(contracts),
        failures=failures,
        tolerance="exact canonical sha256",
        environment=environment,
        details=tuple(details),
    )


def _parse_pod_logs(values: Sequence[str]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for value in values:
        pod, separator, raw_path = value.partition("=")
        if not separator or not pod or not raw_path:
            raise ValueError("--pod-log must use POD=PATH")
        if pod in result:
            raise ValueError(f"duplicate pod log: {pod}")
        result[pod] = Path(raw_path)
    return result


def check_strict_logs(expected_pods: Sequence[str], pod_log_values: Sequence[str], environment: str) -> GateResult:
    pod_logs = _parse_pod_logs(pod_log_values)
    expected = set(expected_pods)
    if len(expected) != len(expected_pods):
        raise ValueError("expected pod identities must be unique")
    provided = set(pod_logs)
    details: list[str] = []
    failures = 0
    missing = sorted(expected - provided)
    unexpected = sorted(provided - expected)
    if missing:
        details.append(f"missing pod logs: {','.join(missing)}")
        failures += len(missing)
    if unexpected:
        details.append(f"unexpected pod logs: {','.join(unexpected)}")
        failures += len(unexpected)

    for pod in sorted(expected & provided):
        path = pod_logs[pod]
        if not path.is_file():
            details.append(f"{pod}: log file missing: {path}")
            failures += 1
            continue
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if STRICT_LOG_PATTERN.search(line):
                details.append(f"{pod}:{line}")
                failures += 1

    if not expected:
        details.append("empty pod population is a failure")
        failures += 1
    return GateResult(
        gate="strict_logs",
        classification="census",
        checked=len(provided),
        population=len(expected),
        failures=failures,
        tolerance="zero strict log matches",
        environment=environment,
        details=tuple(details),
    )


def check_population(candidates_path: Path, census_path: Path, environment: str) -> GateResult:
    candidates = _load_json(candidates_path)
    census = _load_json(census_path)
    if not isinstance(candidates, list):
        raise ValueError("candidates must be a JSON array")
    if not isinstance(census, dict) or not isinstance(census.get("population"), int):
        raise ValueError("census requires an integer population")
    if not census.get("source"):
        raise ValueError("census requires an independent source description")
    checked = len(candidates)
    population = census["population"]
    details: list[str] = []
    failures = abs(population - checked)
    if checked == 0:
        details.append("empty population is a failure")
        failures = max(failures, 1)
    if checked != population:
        details.append(f"population mismatch: got {checked}, expected {population}")
    return GateResult(
        gate="population",
        classification="census",
        checked=checked,
        population=population,
        failures=failures,
        tolerance="exact identity count from independent census",
        environment=environment,
        details=tuple(details),
    )


def _require_provenance(document: dict[str, Any], expected: str, label: str) -> list[str]:
    return [] if document.get("provenance") == expected else [f"{label}: invalid provenance {document.get('provenance')!r}"]


def check_segment_sum_evidence(
    evidence: dict[str, Any],
    abs_tol: float,
    environment: str,
) -> GateResult:
    classification = evidence.get("classification")
    if classification not in {"census", "sample"}:
        raise ValueError("classification must be census or sample")
    observations = evidence.get("observations")
    if not isinstance(observations, list):
        raise ValueError("segment observations must be an array")
    expected = set(SEGMENT_LEVELS)
    observed: dict[str, dict[str, Any]] = {}
    for item in observations:
        if not isinstance(item, dict):
            raise ValueError("segment observations must be objects")
        level = str(item.get("level") or "")
        if not level:
            raise ValueError("segment observation level is required")
        if level in observed:
            raise ValueError(f"duplicate segment level: {level}")
        observed[level] = item

    provenance_failures = _require_provenance(evidence, SEGMENT_PROVENANCE, "segment_sum")
    details = list(provenance_failures)
    failures = 0
    missing = sorted(expected - set(observed))
    unexpected = sorted(set(observed) - expected)
    if missing:
        details.append("missing levels: " + ",".join(missing))
        failures += len(missing)
    if unexpected:
        details.append("unexpected levels: " + ",".join(unexpected))
        failures += len(unexpected)
    for key in sorted(expected & set(observed)):
        item = observed[key]
        try:
            segment_sum = float(item["segment_sum"])
            market_total = float(item["market_total"])
        except (KeyError, TypeError, ValueError) as exc:
            details.append(f"{key}: invalid numeric observation: {exc}")
            failures += 1
            continue
        if not all(math.isfinite(value) for value in (segment_sum, market_total)):
            details.append(f"{key}: non-finite numeric observation")
            failures += 1
            continue
        difference = abs(segment_sum - market_total)
        if difference > abs_tol:
            details.append(
                f"{key}: sum mismatch segment_sum={segment_sum} "
                f"market_total={market_total} difference={difference}"
            )
            failures += 1

    if not expected:
        details.append("empty expected segment population is a failure")
        failures += 1
    return GateResult(
        gate="segment_sum",
        classification=str(classification),
        checked=len(observed),
        population=len(expected),
        failures=failures + len(provenance_failures),
        tolerance=f"abs_tol={abs_tol},rel_tol=0",
        environment=environment,
        details=tuple(details),
    )


def check_market_growth_evidence(
    evidence: dict[str, Any],
    expected_population: int,
    abs_tol: float,
    environment: str,
) -> GateResult:
    if evidence.get("classification") != "census":
        raise ValueError("market growth evidence requires classification=census")
    observations = evidence.get("observations")
    if not isinstance(observations, list):
        raise ValueError("market growth observations must be an array")
    provenance_failures = _require_provenance(evidence, MARKET_GROWTH_PROVENANCE, "market_growth")
    details = list(provenance_failures)
    failures = 0
    identities: set[tuple[str, str]] = set()
    for item in observations:
        if not isinstance(item, dict):
            raise ValueError("market growth observations must be objects")
        identity = (str(item.get("source") or ""), str(item.get("market") or ""))
        if not all(identity):
            raise ValueError("market growth observation identity is incomplete")
        if identity in identities:
            raise ValueError(f"duplicate market growth identity: {'|'.join(identity)}")
        identities.add(identity)
        if item.get("error"):
            details.append(f"{'|'.join(identity)}: live request failed: {item['error']}")
            failures += 1
            continue
        actual = item.get("actual")
        expected = item.get("expected")
        if expected is None:
            details.append(f"{'|'.join(identity)}: independent expected value is unavailable")
            failures += 1
            continue
        expected_end_period = item.get("expected_end_period")
        actual_end_period = item.get("actual_end_period")
        if expected_end_period is not None and actual_end_period != expected_end_period:
            details.append(
                f"{'|'.join(identity)}: growth endpoint mismatch "
                f"actual={actual_end_period!r} expected={expected_end_period!r}"
            )
            failures += 1
        try:
            actual_value = float(actual)
            expected_value = float(expected)
        except (TypeError, ValueError) as exc:
            details.append(f"{'|'.join(identity)}: invalid growth value: {exc}")
            failures += 1
            continue
        sentinel_distance = abs(Decimal(str(actual_value)) + Decimal("100"))
        if sentinel_distance <= Decimal(str(abs_tol)):
            details.append(f"{'|'.join(identity)}: -100 growth sentinel is forbidden")
            failures += 1
        if not all(math.isfinite(value) for value in (actual_value, expected_value)):
            details.append(f"{'|'.join(identity)}: non-finite growth value")
            failures += 1
            continue
        if abs(actual_value) >= 1000.0:
            details.append(f"{'|'.join(identity)}: extreme growth value {actual_value}")
            failures += 1
        if abs(actual_value - expected_value) > abs_tol:
            details.append(
                f"{'|'.join(identity)}: growth mismatch actual={actual_value} "
                f"expected={expected_value} difference={abs(actual_value - expected_value)}"
            )
            failures += 1
    if len(observations) != expected_population:
        details.append(f"population mismatch: got {len(observations)}, expected {expected_population}")
        failures += abs(expected_population - len(observations)) or 1
    if not observations:
        details.append("empty market growth population is a failure")
        failures += 1
    return GateResult(
        gate="market_growth",
        classification="census",
        checked=len(observations),
        population=expected_population,
        failures=failures + len(provenance_failures),
        tolerance=f"abs_tol={abs_tol},rel_tol=0",
        environment=environment,
        details=tuple(details),
    )


def _contribution_total(section: object) -> float:
    if not isinstance(section, dict) or not isinstance(section.get("top_contributors"), list):
        raise ValueError("contribution section requires top_contributors")
    return sum(float(item["contribution_value"]) for item in section["top_contributors"]) + float(
        section.get("others_total") or 0.0
    )


def check_growth_windows(evidence_path: Path, abs_tol: float, environment: str) -> GateResult:
    document = _load_json(evidence_path)
    if not isinstance(document, dict) or document.get("classification") not in {"census", "sample"}:
        raise ValueError("growth evidence requires census or sample classification")
    cases = document.get("cases")
    if not isinstance(cases, list):
        raise ValueError("growth evidence cases must be an array")
    expected_keys = ("1y", "2y", "3y", "4y", "5y")
    details: list[str] = []
    failures = 0
    identities: set[str] = set()
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("id"), str):
            raise ValueError("growth cases require string ids")
        identifier = case["id"]
        if identifier in identities:
            raise ValueError(f"duplicate growth case identity: {identifier}")
        identities.add(identifier)
        windows = case.get("windows")
        expected_starts = case.get("expected_period_starts")
        expected_market_starts = case.get("expected_market_starts")
        expected_truncated_raw = case.get("expected_truncated_windows")
        if not isinstance(windows, dict) or set(windows) != set(expected_keys):
            details.append(f"{identifier}: windows must be exactly 1y,2y,3y,4y,5y")
            failures += 1
            continue
        if not isinstance(expected_starts, dict) or not isinstance(expected_market_starts, dict):
            details.append(f"{identifier}: independent expected starts are required")
            failures += 1
            continue
        if not isinstance(expected_truncated_raw, list) or any(
            not isinstance(key, str) for key in expected_truncated_raw
        ):
            details.append(f"{identifier}: expected_truncated_windows must be a string array")
            failures += 1
            continue
        expected_truncated = set(expected_truncated_raw)
        if not expected_truncated <= set(expected_keys):
            details.append(f"{identifier}: expected_truncated_windows contains unknown windows")
            failures += 1
            continue
        complete_starts: list[str] = []
        complete_signatures: set[str] = set()
        for key in expected_keys:
            window = windows[key]
            if not isinstance(window, dict):
                details.append(f"{identifier}|{key}: window must be an object")
                failures += 1
                continue
            start = str(window.get("period_start") or "")
            if start != str(expected_starts.get(key) or ""):
                details.append(f"{identifier}|{key}: period_start expected={expected_starts.get(key)} actual={start}")
                failures += 1
            try:
                market_start = float(window["market_start"])
                expected_market_start = float(expected_market_starts[key])
                market_end = float(window["market_end"])
                market_growth = float(window["market_growth"])
                brand_total = _contribution_total(window["by_brand"])
                company_total = _contribution_total(window["by_company"])
            except (KeyError, TypeError, ValueError) as exc:
                details.append(f"{identifier}|{key}: invalid numeric evidence: {exc}")
                failures += 1
                continue
            for label, difference in (
                ("market_start", abs(market_start - expected_market_start)),
                ("market_growth", abs((market_end - market_start) - market_growth)),
                ("brand_sum", abs(brand_total - market_growth)),
                ("company_sum", abs(company_total - market_growth)),
                ("brand_company", abs(brand_total - company_total)),
            ):
                if difference > abs_tol:
                    details.append(f"{identifier}|{key}: {label} difference={difference}")
                    failures += 1
            signature = _canonical_sha(
                {
                    "by_brand": window["by_brand"],
                    "by_company": window["by_company"],
                }
            )
            if key in expected_truncated:
                if window.get("reason") != "earliest_available" or window.get("period_start_actual") != start:
                    details.append(f"{identifier}|{key}: invalid truncated-history metadata")
                    failures += 1
            else:
                if window.get("reason") is not None or window.get("period_start_actual") is not None:
                    details.append(f"{identifier}|{key}: unexpected truncated-history metadata")
                    failures += 1
                complete_starts.append(start)
                complete_signatures.add(signature)
        complete_count = len(expected_keys) - len(expected_truncated)
        if len(set(complete_starts)) != complete_count:
            details.append(f"{identifier}: non-truncated period starts are not distinct")
            failures += 1
        if len(complete_signatures) != complete_count:
            details.append(f"{identifier}: non-truncated contribution payloads are not distinct")
            failures += 1
    if not cases:
        details.append("empty growth-window population is a failure")
        failures += 1
    return GateResult(
        gate="growth_windows",
        classification=str(document["classification"]),
        checked=len(cases),
        population=len(cases),
        failures=failures,
        tolerance=f"abs_tol={abs_tol},rel_tol=0",
        environment=environment,
        details=tuple(details),
    )


def _period_interval(value: object) -> tuple[int, int] | None:
    text = str(value)
    if not PERIOD_TOKEN.fullmatch(text):
        return None
    year = int(text[:4])
    suffix = text[5:] if len(text) > 4 else ""
    if suffix.startswith("Q"):
        start = year * 12 + (int(suffix[1:]) - 1) * 3
        return start, start + 2
    if suffix:
        month = int(suffix)
        index = year * 12 + month - 1
        return index, index
    return year * 12, year * 12 + 11


def _period_sections(value: object, path: str = "$") -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    if isinstance(value, dict):
        keys = list(value)
        if keys and all(_period_interval(key) is not None for key in keys):
            sections[path] = [str(key) for key in keys]
        periods = value.get("periods")
        if isinstance(periods, list) and periods and all(_period_interval(item) is not None for item in periods):
            sections[f"{path}.periods"] = [str(item) for item in periods]
        for key, item in value.items():
            sections.update(_period_sections(item, f"{path}.{key}"))
    elif isinstance(value, list):
        point_periods: list[str] = []
        for item in value:
            if not isinstance(item, dict):
                point_periods = []
                break
            period = item.get("period", item.get("period_full", item.get("year")))
            if _period_interval(period) is None:
                point_periods = []
                break
            point_periods.append(str(period))
        if point_periods:
            sections[path] = point_periods
        for index, item in enumerate(value):
            sections.update(_period_sections(item, f"{path}[{index}]"))
    return sections


def _period_values(value: object) -> object:
    if isinstance(value, dict):
        keys = list(value)
        if keys and all(_period_interval(key) is not None for key in keys):
            return [_period_values(value[key]) for key in sorted(keys)]
        return {
            str(key): _period_values(item)
            for key, item in value.items()
            if key not in {"period", "period_full", "periods", "year"}
        }
    if isinstance(value, list):
        return [_period_values(item) for item in value]
    return value


def check_period_ranges(evidence_path: Path, environment: str) -> GateResult:
    document = _load_json(evidence_path)
    if not isinstance(document, dict):
        raise ValueError("period-range evidence must be a JSON object")
    if document.get("classification") not in {"census", "sample"}:
        raise ValueError("period-range evidence requires census or sample classification")
    cases = document.get("cases")
    if not isinstance(cases, list):
        raise ValueError("period-range evidence cases must be an array")

    details: list[str] = []
    failures = 0
    identities: set[str] = set()
    checked = 0
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("id"), str):
            raise ValueError("period-range cases require a string id")
        identifier = case["id"]
        if identifier in identities:
            raise ValueError(f"duplicate period-range identity: {identifier}")
        identities.add(identifier)
        window_a = case.get("window_a")
        window_b = case.get("window_b")
        window_a_repeat = case.get("window_a_repeat")
        if not all(isinstance(window, dict) for window in (window_a, window_b, window_a_repeat)):
            details.append(f"{identifier}: window evidence must be objects")
            failures += 1
            continue
        if not all(
            all(key in window for key in ("start", "end", "payload"))
            for window in (window_a, window_b, window_a_repeat)
        ):
            details.append(f"{identifier}: window evidence missing start, end, or payload")
            failures += 1
            continue
        if _canonical_sha(_period_values(window_a["payload"])) == _canonical_sha(
            _period_values(window_b["payload"])
        ):
            details.append(f"{identifier}: period windows produced identical values")
            failures += 1
        if _canonical_sha(window_a["payload"]) != _canonical_sha(window_a_repeat["payload"]):
            details.append(f"{identifier}: A-B-A replay changed window A")
            failures += 1
        for window_name, window in (
            ("window_a", window_a),
            ("window_b", window_b),
            ("window_a_repeat", window_a_repeat),
        ):
            if not all(key in window for key in ("start", "end", "payload")):
                details.append(f"{identifier}|{window_name}: missing start, end, or payload")
                failures += 1
                continue
            sections = _period_sections(window["payload"])
            if not sections:
                details.append(f"{identifier}|{window_name}: empty period-section population")
                failures += 1
                continue
            start_interval = _period_interval(window["start"])
            end_interval = _period_interval(window["end"])
            if start_interval is None or end_interval is None:
                details.append(f"{identifier}|{window_name}: invalid requested period range")
                failures += 1
                continue
            for path, periods in sections.items():
                checked += 1
                outside = []
                for period in periods:
                    interval = _period_interval(period)
                    if interval is None or interval[1] < start_interval[0] or interval[0] > end_interval[1]:
                        outside.append(period)
                if outside:
                    details.append(
                        f"{identifier}|{window_name}|{path}: periods outside window {','.join(outside)}"
                    )
                    failures += len(outside)
    if not cases:
        details.append("empty period-range case population is a failure")
        failures += 1
    return GateResult(
        gate="period_ranges",
        classification=str(document["classification"]),
        checked=checked,
        population=checked,
        failures=failures,
        tolerance="exact canonical values and inclusive period bounds",
        environment=environment,
        details=tuple(details),
    )


def check_brand_source_evidence(
    evidence: dict[str, Any],
    expected_population: int,
    environment: str,
) -> GateResult:
    if evidence.get("classification") != "census":
        raise ValueError("brand source evidence requires classification=census")
    observations = evidence.get("observations")
    if not isinstance(observations, list):
        raise ValueError("brand source observations must be an array")
    provenance_failures = _require_provenance(evidence, BRAND_SOURCE_PROVENANCE, "brand_sources")
    details = list(provenance_failures)
    observed: dict[tuple[str, str, str], dict[str, Any]] = {}
    for item in observations:
        if not isinstance(item, dict):
            raise ValueError("brand source observations must be objects")
        identity = tuple(str(item.get(field) or "") for field in ("brand", "view", "source"))
        if not all(identity):
            raise ValueError("brand source observation identity is incomplete")
        if identity in observed:
            raise ValueError(f"duplicate brand source identity: {'|'.join(identity)}")
        if not isinstance(item.get("listed"), bool) or not isinstance(item.get("has_data"), bool):
            raise ValueError("brand source observations require boolean listed and has_data")
        observed[identity] = item

    failures = 0
    if len(observed) != expected_population:
        details.append(f"population mismatch: got {len(observed)}, expected {expected_population}")
        failures += abs(expected_population - len(observed)) or 1
    for identity in sorted(observed):
        item = observed[identity]
        if item.get("error"):
            details.append(f"{'|'.join(identity)}: probe failed: {item['error']}")
            failures += 1
            continue
        if item["listed"] != item["has_data"]:
            details.append(
                f"{'|'.join(identity)}: listed/data mismatch "
                f"listed={str(item['listed']).lower()} has_data={str(item['has_data']).lower()}"
            )
            failures += 1
    if not observations:
        details.append("empty observation population is a failure")
        failures = max(failures, 1)
    return GateResult(
        gate="brand_sources",
        classification="census",
        checked=len(observed),
        population=expected_population,
        failures=failures + len(provenance_failures),
        tolerance="exact listed == has_data, missing=fail",
        environment=environment,
        details=tuple(details),
    )


def check_cause_assembly(evidence_path: Path, environment: str) -> GateResult:
    document = _load_json(evidence_path)
    if not isinstance(document, dict) or document.get("classification") != "census":
        raise ValueError("cause assembly evidence requires classification=census")
    expected_raw = document.get("expected_cases")
    cases = document.get("cases")
    if not isinstance(expected_raw, list) or not expected_raw or not all(
        isinstance(identifier, str) and identifier for identifier in expected_raw
    ):
        raise ValueError("cause assembly expected_cases must be a non-empty string array")
    if len(set(expected_raw)) != len(expected_raw):
        raise ValueError("cause assembly expected_cases must be unique")
    if not isinstance(cases, list):
        raise ValueError("cause assembly cases must be an array")
    try:
        max_after_ms = float(document["max_after_ms"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("cause assembly max_after_ms must be numeric") from exc
    if max_after_ms <= 0:
        raise ValueError("cause assembly max_after_ms must be positive")
    if not isinstance(document.get("cache_expanded"), bool):
        raise ValueError("cause assembly cache_expanded must be boolean")
    if document["cache_expanded"] and document.get("invalidation_verified") is not True:
        raise ValueError("expanded cache requires invalidation_verified=true")

    observed: dict[str, dict[str, Any]] = {}
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("id"), str) or not case["id"]:
            raise ValueError("cause assembly cases require string ids")
        identifier = case["id"]
        if identifier in observed:
            raise ValueError(f"duplicate cause assembly identity: {identifier}")
        observed[identifier] = case

    expected = set(expected_raw)
    details: list[str] = []
    failures = 0
    missing = sorted(expected - set(observed))
    unexpected = sorted(set(observed) - expected)
    if missing:
        details.append("missing identities: " + ",".join(missing))
        failures += len(missing)
    if unexpected:
        details.append("unexpected identities: " + ",".join(unexpected))
        failures += len(unexpected)
    for identifier in sorted(expected & set(observed)):
        case = observed[identifier]
        before_name = case.get("before_payload")
        after_name = case.get("after_payload")
        if not isinstance(before_name, str) or not isinstance(after_name, str):
            details.append(f"{identifier}: payload paths must be strings")
            failures += 1
            continue
        before_path = evidence_path.parent / before_name
        after_path = evidence_path.parent / after_name
        try:
            before_bytes = before_path.read_bytes()
            after_bytes = after_path.read_bytes()
            before_ms = float(case["before_ms"])
            after_ms = float(case["after_ms"])
        except (OSError, KeyError, TypeError, ValueError) as exc:
            details.append(f"{identifier}: invalid evidence: {exc}")
            failures += 1
            continue
        if before_bytes != after_bytes:
            details.append(f"{identifier}: byte mismatch")
            failures += 1
        if before_ms <= 0 or after_ms <= 0:
            details.append(f"{identifier}: timings must be positive")
            failures += 1
        elif after_ms >= before_ms:
            details.append(f"{identifier}: no improvement before_ms={before_ms} after_ms={after_ms}")
            failures += 1
        if after_ms > max_after_ms:
            details.append(
                f"{identifier}: after_ms={after_ms} exceeds max_after_ms={max_after_ms}"
            )
            failures += 1
    if not cases:
        details.append("empty cause assembly population is a failure")
        failures = max(failures, 1)
    return GateResult(
        gate="cause_assembly",
        classification="census",
        checked=len(observed),
        population=len(expected),
        failures=failures,
        tolerance=f"exact bytes;after<before;after_ms<={max_after_ms}",
        environment=environment,
        details=tuple(details),
    )


def check_cause_null_integrity(evidence_path: Path, environment: str) -> GateResult:
    document = _load_json(evidence_path)
    if not isinstance(document, dict) or document.get("classification") != "census":
        raise ValueError("cause null integrity evidence requires classification=census")
    candidates = document.get("candidates")
    candidate_breakdown = document.get("candidate_breakdown")
    semantics = document.get("semantics")
    numeric_observations = document.get("numeric_observations")
    performance_cases = document.get("performance_cases")
    try:
        candidate_population = int(document["candidate_population"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("cause null integrity candidate_population must be numeric") from exc
    if candidate_population <= 0 or not isinstance(candidates, list):
        raise ValueError("cause null integrity requires a non-empty candidate census")
    if not isinstance(candidate_breakdown, dict) or not candidate_breakdown:
        raise ValueError("cause null integrity requires a candidate pattern breakdown")
    if not isinstance(semantics, list) or not semantics:
        raise ValueError("cause null integrity requires semantic observations")
    if not isinstance(numeric_observations, list):
        raise ValueError("cause null integrity requires numeric observations")
    if not isinstance(performance_cases, list) or not performance_cases:
        raise ValueError("cause null integrity requires performance cases")

    details: list[str] = []
    failures = 0
    candidate_ids: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, dict) or not isinstance(candidate.get("id"), str):
            raise ValueError("cause null integrity candidates require string ids")
        identifier = candidate["id"]
        if identifier in candidate_ids:
            raise ValueError(f"duplicate cause null candidate: {identifier}")
        candidate_ids.add(identifier)
        if candidate.get("calculation_distortion") is not False:
            details.append(f"{identifier}: calculation distortion remains")
            failures += 1
        if candidate.get("denominator_contamination") is not False:
            details.append(f"{identifier}: denominator contamination remains")
            failures += 1
    if len(candidate_ids) != candidate_population:
        details.append(
            f"candidate census mismatch checked={len(candidate_ids)} population={candidate_population}"
        )
        failures += abs(candidate_population - len(candidate_ids)) or 1
    try:
        breakdown_population = sum(int(value) for value in candidate_breakdown.values())
    except (TypeError, ValueError) as exc:
        raise ValueError("cause null integrity candidate breakdown must be numeric") from exc
    if breakdown_population != candidate_population:
        details.append(
            f"candidate pattern mismatch breakdown={breakdown_population} population={candidate_population}"
        )
        failures += abs(candidate_population - breakdown_population) or 1

    semantic_ids: set[str] = set()
    for observation in semantics:
        if not isinstance(observation, dict) or not isinstance(observation.get("id"), str):
            raise ValueError("cause null semantic observations require string ids")
        identifier = observation["id"]
        if identifier in semantic_ids:
            raise ValueError(f"duplicate cause null semantic identity: {identifier}")
        semantic_ids.add(identifier)
        kind = observation.get("kind")
        value = observation.get("value")
        if kind == "missing" and value is not None:
            details.append(f"{identifier}: missing value was coerced to zero or another number")
            failures += 1
        elif kind == "real_zero":
            try:
                real_zero_preserved = not isinstance(value, bool) and Decimal(str(value)) == Decimal("0")
            except (ValueError, ArithmeticError):
                real_zero_preserved = False
            if not real_zero_preserved:
                details.append(f"{identifier}: real zero was not preserved")
                failures += 1
        elif kind not in {"missing", "real_zero"}:
            raise ValueError(f"unsupported cause null semantic kind: {kind}")

    numeric_ids: set[str] = set()
    for observation in numeric_observations:
        if not isinstance(observation, dict) or not isinstance(observation.get("id"), str):
            raise ValueError("cause null numeric observations require string ids")
        identifier = observation["id"]
        if identifier in numeric_ids:
            raise ValueError(f"duplicate cause null numeric identity: {identifier}")
        numeric_ids.add(identifier)
        try:
            value = Decimal(str(observation["value"]))
        except (KeyError, ValueError, ArithmeticError) as exc:
            raise ValueError(f"{identifier}: invalid numeric observation") from exc
        if not value.is_finite() or value <= Decimal("-100") or abs(value) >= Decimal("1000"):
            details.append(f"{identifier}: prohibited extreme numeric value={observation['value']}")
            failures += 1

    performance_ids: set[str] = set()
    for case in performance_cases:
        if not isinstance(case, dict) or not isinstance(case.get("id"), str):
            raise ValueError("cause null performance cases require string ids")
        identifier = case["id"]
        if identifier in performance_ids:
            raise ValueError(f"duplicate cause null performance identity: {identifier}")
        performance_ids.add(identifier)
        try:
            before_calls = int(case["before_calls"])
            after_calls = int(case["after_calls"])
            before_ms = float(case["before_ms"])
            after_ms = float(case["after_ms"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{identifier}: invalid performance evidence") from exc
        if before_calls <= 0 or before_ms <= 0 or after_ms <= 0:
            raise ValueError(f"{identifier}: performance evidence must be positive")
        if after_calls > before_calls:
            details.append(
                f"{identifier}: call count increased before={before_calls} after={after_calls}"
            )
            failures += 1
        ratio = after_ms / before_ms
        if ratio > 1.2:
            details.append(f"{identifier}: latency ratio {ratio:.6f} exceeds 1.2")
            failures += 1

    checked = len(candidate_ids) + len(semantic_ids) + len(numeric_ids) + len(performance_ids)
    population = candidate_population + len(semantics) + len(numeric_observations) + len(performance_cases)
    return GateResult(
        gate="cause_null_integrity",
        classification="census",
        checked=checked,
        population=population,
        failures=failures,
        tolerance="exact null/zero;finite -100<x<1000;calls_after<=before;latency_ratio<=1.2",
        environment=environment,
        details=tuple(details),
    )


def check_competition_ranking(
    observations_path: Path,
    expected_years: Sequence[str],
    abs_tol: float,
    environment: str,
) -> GateResult:
    observations = _load_json(observations_path)
    if not isinstance(observations, dict):
        raise ValueError("competition ranking observations must be an object")
    details: list[str] = []
    failures = 0
    totals: dict[tuple[str, str], float] = {}
    identities: set[tuple[str, str]] = set()
    for entity in ("brand", "company"):
        payload = observations.get(entity)
        if not isinstance(payload, dict) or not isinstance(payload.get("yearly"), list):
            details.append(f"{entity}: yearly census missing")
            failures += 1
            continue
        for year_item in payload["yearly"]:
            if not isinstance(year_item, dict) or "year" not in year_item or not isinstance(year_item.get("rankings"), list):
                details.append(f"{entity}: invalid yearly ranking")
                failures += 1
                continue
            year = str(year_item["year"])
            identity = (entity, year)
            if identity in identities:
                details.append(f"{entity}|{year}: duplicate identity")
                failures += 1
                continue
            identities.add(identity)
            rows = year_item["rankings"]
            visible = [row for row in rows if isinstance(row, dict) and not row.get("is_others")]
            ranks = [row.get("rank") for row in visible]
            expected = list(range(1, len(visible) + 1))
            if ranks != expected:
                details.append(
                    f"{entity}|{year}: non-contiguous ranks got={','.join(map(str, ranks))} "
                    f"expected={','.join(map(str, expected))}"
                )
                failures += 1
            try:
                totals[identity] = sum(float(row["value"]) for row in rows)
                if any(row.get("ms_pct") is None for row in rows):
                    raise ValueError("null share")
            except (KeyError, TypeError, ValueError) as exc:
                details.append(f"{entity}|{year}: invalid ranking value: {exc}")
                failures += 1

    expected = {(entity, str(year)) for entity in ("brand", "company") for year in expected_years}
    missing = sorted(expected - identities)
    unexpected = sorted(identities - expected)
    if missing:
        details.append("missing entity/year identities: " + ", ".join(f"{entity}|{year}" for entity, year in missing))
        failures += len(missing)
    if unexpected:
        details.append(
            "unexpected entity/year identities: " + ", ".join(f"{entity}|{year}" for entity, year in unexpected)
        )
        failures += len(unexpected)
    for year in sorted({str(year) for year in expected_years}):
        brand_total = totals.get(("brand", year))
        company_total = totals.get(("company", year))
        if brand_total is None or company_total is None:
            details.append(f"{year}: brand/company census pair missing")
            failures += 1
            continue
        difference = abs(brand_total - company_total)
        if difference > abs_tol:
            details.append(
                f"{year}: entity total mismatch brand={brand_total} company={company_total} difference={difference}"
            )
            failures += 1
    if not expected:
        details.append("empty expected competition ranking census is a failure")
        failures += 1
    return GateResult(
        gate="competition_ranking",
        classification="census",
        checked=len(identities),
        population=len(expected),
        failures=failures,
        tolerance=f"abs_tol={abs_tol},rel_tol=0",
        environment=environment,
        details=tuple(details),
    )


def check_f116_correctness(evidence_path: Path, environment: str) -> GateResult:
    document = _load_json(evidence_path)
    if not isinstance(document, dict) or document.get("classification") != "census":
        raise ValueError("F-116 evidence must be a census object")

    sections = {
        "specialty": document.get("specialty_observations"),
        "brand_storage": document.get("brand_storage"),
        "api": document.get("api_cases"),
        "canonical": document.get("canonical_cells"),
        "performance": document.get("performance_cases"),
    }
    for label, values in sections.items():
        if not isinstance(values, list) or not values:
            raise ValueError(f"F-116 evidence requires non-empty {label} census")

    details: list[str] = []
    failures = 0
    checked = 0
    identities: set[str] = set()

    def identity(item: object, section: str) -> tuple[str, dict[str, Any]]:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            raise ValueError(f"F-116 {section} entries require string ids")
        identifier = f"{section}:{item['id']}"
        if identifier in identities:
            raise ValueError(f"duplicate F-116 evidence identity: {identifier}")
        identities.add(identifier)
        return identifier, item

    abs_tol = Decimal("0.01")
    for raw in sections["specialty"]:
        identifier, item = identity(raw, "specialty")
        checked += 1
        try:
            market_total = Decimal(str(item["market_total"]))
            specialty_total = Decimal(str(item["specialty_total"]))
            parent_rows = int(item["parent_rows"])
            detail_count = int(item["detail_count"])
            overcount_ratio = Decimal(str(item["overcount_ratio"]))
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            raise ValueError(f"{identifier}: invalid specialty evidence") from exc
        if abs(market_total - specialty_total) > abs_tol:
            details.append(
                f"{identifier}: specialty total mismatch market={market_total} specialty={specialty_total}"
            )
            failures += 1
        if parent_rows != 0:
            details.append(f"{identifier}: aggregate parent rows remain={parent_rows}")
            failures += 1
        if detail_count <= 0:
            details.append(f"{identifier}: no detail specialties remain")
            failures += 1
        if overcount_ratio > Decimal("1.0000000001"):
            details.append(f"{identifier}: overcount ratio remains={overcount_ratio}")
            failures += 1

    for raw in sections["brand_storage"]:
        identifier, item = identity(raw, "brand_storage")
        checked += 1
        expected = item.get("expected_brands")
        stored = item.get("stored_brands")
        if not isinstance(expected, list) or not isinstance(stored, list):
            raise ValueError(f"{identifier}: brand sets must be arrays")
        if len(set(map(str, expected))) != len(expected) or len(set(map(str, stored))) != len(stored):
            raise ValueError(f"{identifier}: brand sets must contain unique values")
        missing = sorted(set(map(str, expected)) - set(map(str, stored)))
        unexpected = sorted(set(map(str, stored)) - set(map(str, expected)))
        if missing or unexpected:
            details.append(
                f"{identifier}: stored brand census mismatch missing={','.join(missing)} "
                f"unexpected={','.join(unexpected)}"
            )
            failures += 1

    for raw in sections["api"]:
        identifier, item = identity(raw, "api")
        checked += 1
        returned = item.get("returned_brands")
        expected = item.get("expected_brands")
        if not isinstance(returned, list) or not isinstance(expected, list):
            raise ValueError(f"{identifier}: API brand sets must be arrays")
        if list(map(str, returned)) != list(map(str, expected)) or len(returned) > 6:
            details.append(
                f"{identifier}: API selection changed returned={returned!r} expected={expected!r}"
            )
            failures += 1
        try:
            before_bytes = int(item["response_bytes_before"])
            after_bytes = int(item["response_bytes_after"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{identifier}: invalid response size evidence") from exc
        if before_bytes <= 0 or after_bytes <= 0 or after_bytes > before_bytes:
            details.append(
                f"{identifier}: response size grew before={before_bytes} after={after_bytes}"
            )
            failures += 1

    for raw in sections["canonical"]:
        identifier, item = identity(raw, "canonical")
        checked += 1
        source = item.get("expected_source")
        brand_value = item.get("brand_value")
        product_value = item.get("product_value")
        result_value = item.get("result_value")
        expected_value = brand_value if source == "brand" else product_value if source == "product" else None
        if source not in {"brand", "product", "missing"}:
            raise ValueError(f"{identifier}: invalid expected_source={source!r}")
        if result_value != expected_value:
            details.append(
                f"{identifier}: canonical precedence mismatch source={source} "
                f"expected={expected_value!r} actual={result_value!r}"
            )
            failures += 1

    for raw in sections["performance"]:
        identifier, item = identity(raw, "performance")
        checked += 1
        try:
            before_ms = float(item["before_ms"])
            after_ms = float(item["after_ms"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{identifier}: invalid performance evidence") from exc
        if before_ms <= 0 or after_ms <= 0:
            raise ValueError(f"{identifier}: performance timings must be positive")
        ratio = after_ms / before_ms
        if ratio > 1.2:
            details.append(f"{identifier}: latency ratio {ratio:.6f} exceeds 1.2")
            failures += 1

    population = sum(len(values) for values in sections.values())
    return GateResult(
        gate="f116_correctness",
        classification="census",
        checked=checked,
        population=population,
        failures=failures,
        tolerance="specialty_abs=0.01;brand/api/canonical=exact;latency_ratio<=1.2",
        environment=environment,
        details=tuple(details),
    )


def _run_segment_gate(args: argparse.Namespace) -> GateResult:
    evidence = collect_segment_sum_evidence(args.base_url, timeout_seconds=args.timeout_seconds, env=dict(os.environ))
    write_evidence(args.evidence_output, evidence)
    return check_segment_sum_evidence(evidence, args.abs_tol, args.environment)


def _run_market_growth_gate(args: argparse.Namespace) -> GateResult:
    evidence = collect_market_growth_evidence(
        args.base_url,
        timeout_seconds=args.timeout_seconds,
        max_workers=args.max_workers,
        env=dict(os.environ),
    )
    write_evidence(args.evidence_output, evidence)
    return check_market_growth_evidence(evidence, 902, args.abs_tol, args.environment)


def _run_brand_source_gate(args: argparse.Namespace) -> GateResult:
    evidence = collect_brand_source_evidence(
        args.base_url,
        timeout_seconds=args.timeout_seconds,
        max_workers=args.max_workers,
    )
    write_evidence(args.evidence_output, evidence)
    return check_brand_source_evidence(evidence, 100, args.environment)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fail-closed release acceptance gates")
    subparsers = parser.add_subparsers(dest="command", required=True)

    goldens = subparsers.add_parser("goldens")
    goldens.add_argument("--base-url", required=True)
    goldens.add_argument("--timeout-seconds", type=float, default=30.0)
    goldens.add_argument("--environment", default="local")

    logs = subparsers.add_parser("strict-logs")
    logs.add_argument("--expected-pod", action="append", default=[])
    logs.add_argument("--pod-log", action="append", default=[])
    logs.add_argument("--environment", default="local")

    population = subparsers.add_parser("population")
    population.add_argument("--candidates", type=Path, required=True)
    population.add_argument("--census", type=Path, required=True)
    population.add_argument("--environment", default="local")

    segment = subparsers.add_parser("segment-sum")
    segment.add_argument("--base-url", required=True)
    segment.add_argument("--timeout-seconds", type=float, default=30.0)
    segment.add_argument("--abs-tol", type=float, default=0.01)
    segment.add_argument("--evidence-output", type=Path)
    segment.add_argument("--environment", default="local")

    market_growth = subparsers.add_parser("market-growth")
    market_growth.add_argument("--base-url", required=True)
    market_growth.add_argument("--timeout-seconds", type=float, default=30.0)
    market_growth.add_argument("--max-workers", type=int, default=8)
    market_growth.add_argument("--abs-tol", type=float, default=0.0001)
    market_growth.add_argument("--evidence-output", type=Path)
    market_growth.add_argument("--environment", default="local")

    growth = subparsers.add_parser("growth-windows")
    growth.add_argument("--evidence", type=Path, required=True)
    growth.add_argument("--abs-tol", type=float, default=0.01)
    growth.add_argument("--environment", default="local")

    periods = subparsers.add_parser("period-ranges")
    periods.add_argument("--evidence", type=Path, required=True)
    periods.add_argument("--environment", default="local")

    sources = subparsers.add_parser("brand-sources")
    sources.add_argument("--base-url", required=True)
    sources.add_argument("--timeout-seconds", type=float, default=30.0)
    sources.add_argument("--max-workers", type=int, default=8)
    sources.add_argument("--evidence-output", type=Path)
    sources.add_argument("--environment", default="local")

    cause_assembly = subparsers.add_parser("cause-assembly")
    cause_assembly.add_argument("--evidence", type=Path, required=True)
    cause_assembly.add_argument("--environment", default="local")

    cause_null_integrity = subparsers.add_parser("cause-null-integrity")
    cause_null_integrity.add_argument("--evidence", type=Path, required=True)
    cause_null_integrity.add_argument("--environment", default="local")

    competition = subparsers.add_parser("competition-ranking")
    competition.add_argument("--observations", type=Path, required=True)
    competition.add_argument("--expected-year", action="append", required=True)
    competition.add_argument("--abs-tol", type=float, default=0.01)
    competition.add_argument("--environment", default="local")

    f116 = subparsers.add_parser("f116-correctness")
    f116.add_argument("--evidence", type=Path, required=True)
    f116.add_argument("--environment", default="local")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    handlers: dict[str, Callable[[], GateResult]] = {
        "goldens": lambda: check_goldens(
            TRACKED_GOLDEN_CONTRACTS,
            args.base_url,
            args.environment,
            args.timeout_seconds,
        ),
        "strict-logs": lambda: check_strict_logs(args.expected_pod, args.pod_log, args.environment),
        "population": lambda: check_population(args.candidates, args.census, args.environment),
        "segment-sum": lambda: _run_segment_gate(args),
        "market-growth": lambda: _run_market_growth_gate(args),
        "growth-windows": lambda: check_growth_windows(
            args.evidence,
            args.abs_tol,
            args.environment,
        ),
        "period-ranges": lambda: check_period_ranges(args.evidence, args.environment),
        "brand-sources": lambda: _run_brand_source_gate(args),
        "cause-assembly": lambda: check_cause_assembly(args.evidence, args.environment),
        "cause-null-integrity": lambda: check_cause_null_integrity(args.evidence, args.environment),
        "competition-ranking": lambda: check_competition_ranking(
            args.observations,
            args.expected_year,
            args.abs_tol,
            args.environment,
        ),
        "f116-correctness": lambda: check_f116_correctness(args.evidence, args.environment),
    }
    try:
        result = handlers[args.command]()
    except (OSError, RuntimeError, TypeError, ValueError, json.JSONDecodeError) as exc:
        result = GateResult(
            gate=args.command.replace("-", "_"),
            classification="census",
            checked=0,
            population=0,
            failures=1,
            tolerance="not evaluated",
            environment=getattr(args, "environment", "local"),
            details=(f"gate input error: {exc}",),
        )
    print(result.render())
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
