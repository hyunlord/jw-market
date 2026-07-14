from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, Callable, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen


STRICT_LOG_PATTERN = re.compile(r"Traceback|(?:^|\s)ERROR(?:\s|:|$)|(?:^|\s)5[0-9]{2}(?:\s|$)")
IDENTITY_FIELDS = ("market", "period", "source", "measure", "level")
PERIOD_TOKEN = re.compile(r"^\d{4}(?:-(?:0[1-9]|1[0-2]|Q[1-4]))?$")


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
    def effective_failures(self) -> int:
        if self.failures:
            return self.failures
        if self.population <= 0 or self.checked != self.population:
            return 1
        return 0

    @property
    def exit_code(self) -> int:
        return 1 if self.effective_failures else 0

    def render(self) -> str:
        fields = (
            f"gate={self.gate}",
            f"classification={self.classification}",
            f"checked={self.checked}",
            f"population={self.population}",
            "missing=fail",
            f"tolerance={self.tolerance}",
            f"failures={self.effective_failures}",
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


def check_goldens(contracts_path: Path, observations_path: Path, environment: str) -> GateResult:
    contract_document = _load_json(contracts_path)
    if not isinstance(contract_document, dict):
        raise ValueError("contracts document must be a JSON object")
    contracts = _unique_by_id(contract_document.get("contracts"), label="contract")
    observations = _unique_by_id(_load_json(observations_path), label="observation")
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
    expected_ids = set(contracts)
    observed_ids = set(observations)
    missing = sorted(expected_ids - observed_ids)
    unexpected = sorted(observed_ids - expected_ids)
    if missing:
        details.append(f"missing identities: {','.join(missing)}")
        failures += len(missing)
    if unexpected:
        details.append(f"unexpected identities: {','.join(unexpected)}")
        failures += len(unexpected)

    for identifier, contract in contracts.items():
        absent_metadata = [field for field in required_metadata if not contract.get(field)]
        if absent_metadata:
            details.append(f"{identifier}: missing contract metadata {','.join(absent_metadata)}")
            failures += 1
            continue
        observation = observations.get(identifier)
        if observation is None:
            continue
        if "payload" not in observation:
            details.append(f"{identifier}: observation payload missing")
            failures += 1
            continue
        actual = _canonical_sha(observation["payload"])
        expected = str(contract["canonical_sha256"])
        if actual != expected:
            details.append(f"{identifier}: canonical sha mismatch expected={expected} actual={actual}")
            failures += 1

    return GateResult(
        gate="api_goldens",
        classification="census",
        checked=len(observations),
        population=len(contracts),
        failures=failures,
        tolerance="exact canonical sha256",
        environment=environment,
        details=tuple(details),
    )


def _require_tracked_file(repo_root: Path, path: Path) -> Path:
    root = repo_root.resolve(strict=True)
    resolved = path.resolve(strict=True)
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("contract registry must be inside the repository") from exc
    tracked = subprocess.run(
        ["git", "-C", str(root), "ls-files", "--error-unmatch", "--", str(relative)],
        check=False,
        capture_output=True,
        text=True,
    )
    if tracked.returncode != 0:
        raise ValueError("contract registry must be tracked by git")
    return resolved


def _live_contracts(contracts_path: Path) -> tuple[list[dict[str, Any]], set[str]]:
    document = _load_json(contracts_path)
    if not isinstance(document, dict):
        raise ValueError("contracts document must be a JSON object")
    contracts = list(_unique_by_id(document.get("contracts"), label="contract").values())
    required_metadata = (
        "request",
        "truth_basis_status",
        "truth_basis",
        "measured_at",
        "database",
        "build_sha",
        "runtime_digest",
    )
    enabled_ids: set[str] = set()
    for contract in contracts:
        identifier = contract["id"]
        absent = [field for field in required_metadata if not contract.get(field)]
        if absent:
            raise ValueError(f"{identifier}: missing contract metadata {','.join(absent)}")
        request = contract["request"]
        if not isinstance(request, dict) or request.get("method") != "GET":
            raise ValueError(f"{identifier}: live golden request must use GET")
        path = request.get("path")
        if not isinstance(path, str) or not path.startswith("/") or urlsplit(path).netloc:
            raise ValueError(f"{identifier}: request path must be an absolute-path reference")
        if contract.get("gate_enabled") is True:
            if contract.get("truth_basis_status") != "confirmed":
                raise ValueError(f"{identifier}: enabled contract requires confirmed truth basis")
            digest = contract.get("canonical_sha256")
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError(f"{identifier}: enabled contract requires canonical_sha256")
            enabled_ids.add(identifier)
            continue
        if contract.get("truth_basis_status") != "unconfirmed":
            raise ValueError(f"{identifier}: disabled contract must be truth_basis_status=unconfirmed")
        if contract.get("canonical_sha256") is not None:
            raise ValueError(f"{identifier}: unconfirmed contract must not define an expected hash")
    return contracts, enabled_ids


def check_live_goldens(
    repo_root: Path,
    contracts_path: Path,
    base_url: str,
    timeout: float,
    environment: str,
) -> GateResult:
    tracked_path = _require_tracked_file(repo_root, contracts_path)
    contracts, enabled_ids = _live_contracts(tracked_path)
    details: list[str] = []
    population = len(enabled_ids)
    failures = 0
    if population == 0:
        details.append("empty enabled golden population is a failure")
        failures = 1

    parsed_base = urlsplit(base_url)
    if parsed_base.scheme not in {"http", "https"} or not parsed_base.netloc:
        raise ValueError("base URL must use http or https and include a host")
    normalized_base = base_url.rstrip("/") + "/"
    checked = 0
    for contract in contracts:
        identifier = contract["id"]
        enabled = identifier in enabled_ids
        request_contract = contract["request"]
        path = str(request_contract["path"])
        url = urljoin(normalized_base, path.lstrip("/"))
        headers = {
            "Accept": "application/json",
            "User-Agent": "jw-market-tracked-golden-gate/20260714",
        }
        declared_headers = request_contract.get("headers", {})
        if not isinstance(declared_headers, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in declared_headers.items()
        ):
            raise ValueError(f"{identifier}: request headers must be a string map")
        headers.update(declared_headers)
        checked += int(enabled)
        try:
            with urlopen(Request(url, headers=headers, method="GET"), timeout=timeout) as response:
                status = response.status
                body = response.read()
        except HTTPError as exc:
            status = exc.code
            body = exc.read()
        except (TimeoutError, URLError, OSError) as exc:
            label = "golden_http" if enabled else "golden_observation"
            details.append(f"{label}={identifier} status=unreachable bytes=0 error={type(exc).__name__}")
            failures += int(enabled)
            continue

        if not enabled:
            try:
                observed_sha = _canonical_sha(json.loads(body)) if status == 200 else "unavailable"
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
                observed_sha = "invalid_json"
            details.append(
                f"golden_observation={identifier} status={status} bytes={len(body)} "
                f"actual={observed_sha} expected=unconfirmed request=GET {path}"
            )
            continue

        details.append(
            f"golden_http={identifier} status={status} bytes={len(body)} "
            f"request=GET {path}"
        )
        if status != 200:
            details.append(f"golden_status={identifier} matched=false reason=http_{status}")
            failures += 1
            continue
        try:
            payload = json.loads(body)
            actual = _canonical_sha(payload)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
            details.append(
                f"golden_status={identifier} matched=false reason=invalid_json "
                f"error={type(exc).__name__}"
            )
            failures += 1
            continue
        expected = str(contract["canonical_sha256"])
        matched = actual == expected
        details.append(
            f"golden_status={identifier} matched={'true' if matched else 'false'} "
            f"actual={actual} expected={expected}"
        )
        failures += int(not matched)

    return GateResult(
        gate="live_api_goldens",
        classification="census",
        checked=checked,
        population=population,
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

    strict_matches = 0
    scanned = 0
    for pod in sorted(expected & provided):
        path = pod_logs[pod]
        if not path.is_file():
            details.append(f"{pod}: log file missing: {path}")
            failures += 1
            continue
        scanned += 1
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if STRICT_LOG_PATTERN.search(line):
                details.append(f"{pod}:{line}")
                failures += 1
                strict_matches += 1

    if not expected:
        details.append("empty pod population is a failure")
        failures += 1
    details.insert(0, f"strict_log_pods_scanned={scanned}")
    details.insert(0, f"strict_log_matches={strict_matches}")
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


def check_population(
    candidates_path: Path,
    census_path: Path,
    environment: str,
    gate_id: str,
) -> GateResult:
    candidates = _load_json(candidates_path)
    census = _load_json(census_path)
    if not isinstance(candidates, list):
        raise ValueError("candidates must be a JSON array")
    if not isinstance(census, dict) or not isinstance(census.get("population"), int):
        raise ValueError("census requires an integer population")
    if not census.get("source"):
        raise ValueError("census requires an independent source description")
    if gate_id in {"f062_molecule_parity", "f062_corpus_parity"}:
        if census.get("source_kind") != "direct_db_count":
            raise ValueError(f"{gate_id} requires source_kind=direct_db_count")
        query = census.get("query")
        if not isinstance(query, str) or "count(" not in query.lower():
            raise ValueError(f"{gate_id} requires the independent COUNT query")
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
        gate=gate_id,
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fail-closed release acceptance gates")
    subparsers = parser.add_subparsers(dest="command", required=True)

    goldens = subparsers.add_parser("goldens")
    goldens.add_argument("--contracts", type=Path, required=True)
    goldens.add_argument("--observations", type=Path, required=True)
    goldens.add_argument("--environment", default="local")

    live_goldens = subparsers.add_parser("live-goldens")
    live_goldens.add_argument("--repo-root", type=Path, required=True)
    live_goldens.add_argument("--contracts", type=Path, required=True)
    live_goldens.add_argument("--base-url", required=True)
    live_goldens.add_argument("--timeout", type=float, default=30.0)
    live_goldens.add_argument("--environment", default="runtime")

    logs = subparsers.add_parser("strict-logs")
    logs.add_argument("--expected-pod", action="append", default=[])
    logs.add_argument("--pod-log", action="append", default=[])
    logs.add_argument("--environment", default="local")

    population = subparsers.add_parser("population")
    population.add_argument("--candidates", type=Path, required=True)
    population.add_argument("--census", type=Path, required=True)
    population.add_argument("--gate-id", default="population")
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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    handlers: dict[str, Callable[[], GateResult]] = {
        "goldens": lambda: check_goldens(args.contracts, args.observations, args.environment),
        "live-goldens": lambda: check_live_goldens(
            args.repo_root,
            args.contracts,
            args.base_url,
            args.timeout,
            args.environment,
        ),
        "strict-logs": lambda: check_strict_logs(args.expected_pod, args.pod_log, args.environment),
        "population": lambda: check_population(
            args.candidates,
            args.census,
            args.environment,
            args.gate_id,
        ),
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
