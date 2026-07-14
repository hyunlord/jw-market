from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any, Callable, Sequence
from urllib.error import URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen


STRICT_LOG_PATTERN = re.compile(r"Traceback|(?:^|\s)ERROR(?:\s|:|$)|(?:^|\s)5[0-9]{2}(?:\s|$)")
IDENTITY_FIELDS = ("market", "period", "source", "measure", "level")
PERIOD_TOKEN = re.compile(r"^\d{4}(?:-(?:0[1-9]|1[0-2]|Q[1-4]))?$")
TRACKED_GOLDEN_CONTRACTS = (
    Path(__file__).resolve().parents[3] / "tests" / "api" / "api_golden_contracts.json"
)


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


def _identity(item: dict[str, Any]) -> tuple[str, ...]:
    missing = [field for field in IDENTITY_FIELDS if field not in item]
    if missing:
        raise ValueError(f"segment identity missing fields: {','.join(missing)}")
    return tuple(str(item[field]) for field in IDENTITY_FIELDS)


def check_segment_sums(
    expected_path: Path,
    observations_path: Path,
    abs_tol: float,
    environment: str,
) -> GateResult:
    expected_document = _load_json(expected_path)
    observations_document = _load_json(observations_path)
    if not isinstance(expected_document, dict):
        raise ValueError("expected identities document must be an object")
    classification = expected_document.get("classification")
    if classification not in {"census", "sample"}:
        raise ValueError("classification must be census or sample")
    expected_items = expected_document.get("identities")
    if not isinstance(expected_items, list) or not isinstance(observations_document, list):
        raise ValueError("segment identities and observations must be arrays")
    expected = {_identity(item) for item in expected_items if isinstance(item, dict)}
    observed: dict[tuple[str, ...], dict[str, Any]] = {}
    for item in observations_document:
        if not isinstance(item, dict):
            raise ValueError("segment observations must be objects")
        key = _identity(item)
        if key in observed:
            raise ValueError(f"duplicate segment identity: {'|'.join(key)}")
        observed[key] = item

    details: list[str] = []
    failures = 0
    missing = sorted(expected - set(observed))
    unexpected = sorted(set(observed) - expected)
    if missing:
        details.append("missing identities: " + ",".join("|".join(item) for item in missing))
        failures += len(missing)
    if unexpected:
        details.append("unexpected identities: " + ",".join("|".join(item) for item in unexpected))
        failures += len(unexpected)
    for key in sorted(expected & set(observed)):
        item = observed[key]
        try:
            segment_sum = float(item["segment_sum"])
            market_total = float(item["market_total"])
        except (KeyError, TypeError, ValueError) as exc:
            details.append(f"{'|'.join(key)}: invalid numeric observation: {exc}")
            failures += 1
            continue
        difference = abs(segment_sum - market_total)
        if difference > abs_tol:
            details.append(
                f"{'|'.join(key)}: sum mismatch segment_sum={segment_sum} "
                f"market_total={market_total} difference={difference}"
            )
            failures += 1

    if not expected:
        details.append("empty expected segment population is a failure")
        failures += 1
    return GateResult(
        gate="segment_sum",
        classification=classification,
        checked=len(observed),
        population=len(expected),
        failures=failures,
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


def check_brand_sources(
    expectations_path: Path,
    observations_path: Path,
    environment: str,
) -> GateResult:
    expectations = _load_json(expectations_path)
    observations = _load_json(observations_path)
    if not isinstance(expectations, dict) or expectations.get("classification") != "census":
        raise ValueError("brand source expectations require classification=census")
    dimensions: list[list[str]] = []
    for field in ("brands", "views", "sources"):
        values = expectations.get(field)
        if not isinstance(values, list) or not values or not all(isinstance(value, str) for value in values):
            raise ValueError(f"brand source expectations require non-empty string {field}")
        if len(set(values)) != len(values):
            raise ValueError(f"brand source expectation {field} must be unique")
        dimensions.append(values)
    if not isinstance(observations, list):
        raise ValueError("brand source observations must be an array")

    expected = {
        (brand, view, source)
        for brand in dimensions[0]
        for view in dimensions[1]
        for source in dimensions[2]
    }
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

    details: list[str] = []
    failures = 0
    missing = sorted(expected - set(observed))
    unexpected = sorted(set(observed) - expected)
    if missing:
        details.append("missing identities: " + ",".join("|".join(item) for item in missing))
        failures += len(missing)
    if unexpected:
        details.append("unexpected identities: " + ",".join("|".join(item) for item in unexpected))
        failures += len(unexpected)
    for identity in sorted(expected & set(observed)):
        item = observed[identity]
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
        population=len(expected),
        failures=failures,
        tolerance="exact listed == has_data, missing=fail",
        environment=environment,
        details=tuple(details),
    )


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
    segment.add_argument("--expected-identities", type=Path, required=True)
    segment.add_argument("--observations", type=Path, required=True)
    segment.add_argument("--abs-tol", type=float, default=0.01)
    segment.add_argument("--environment", default="local")

    growth = subparsers.add_parser("growth-windows")
    growth.add_argument("--evidence", type=Path, required=True)
    growth.add_argument("--abs-tol", type=float, default=0.01)
    growth.add_argument("--environment", default="local")

    periods = subparsers.add_parser("period-ranges")
    periods.add_argument("--evidence", type=Path, required=True)
    periods.add_argument("--environment", default="local")

    sources = subparsers.add_parser("brand-sources")
    sources.add_argument("--expectations", type=Path, required=True)
    sources.add_argument("--observations", type=Path, required=True)
    sources.add_argument("--environment", default="local")
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
        "segment-sum": lambda: check_segment_sums(
            args.expected_identities,
            args.observations,
            args.abs_tol,
            args.environment,
        ),
        "growth-windows": lambda: check_growth_windows(
            args.evidence,
            args.abs_tol,
            args.environment,
        ),
        "period-ranges": lambda: check_period_ranges(args.evidence, args.environment),
        "brand-sources": lambda: check_brand_sources(
            args.expectations,
            args.observations,
            args.environment,
        ),
    }
    try:
        result = handlers[args.command]()
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
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
