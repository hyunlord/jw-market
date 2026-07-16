"""Fail-closed acceptance contracts for the deep-research rollout."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import re
from typing import Any, Mapping, Sequence


_AMOUNT_RE = re.compile(r"(?<![\d.])([0-9][0-9,]*(?:\.[0-9]+)?)\s*억원")
_CONTINUATION_RE = re.compile(
    r"^(?:하지만|다만|그러나|그런데|실제로?|실제\s*값|정정|오류|대신|아니(?:라|고)?)"
)
_STRICT_LOG_RE = re.compile(
    r"Traceback|\bERROR\b|"
    r"(?:HTTP(?:/\d(?:\.\d)?)?[\"']?\s+|status(?:_code)?[=:]\s*)5\d\d\b"
)


@dataclass(frozen=True, slots=True)
class ExactAmountContract:
    expected_amount: Decimal
    period_terms: tuple[str, ...]
    subject_terms: tuple[str, ...]
    metric_terms: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GateResult:
    gate: str
    checked: int
    population: int
    failure_reasons: tuple[str, ...] = ()
    classification: str = "census"
    missing: str = "fail"
    tolerance: str = "exact"
    environment: str = "live"
    details: Mapping[str, Any] = field(default_factory=dict)

    @property
    def exit_code(self) -> int:
        return 1 if self.failure_reasons else 0

    @property
    def failures(self) -> tuple[str, ...]:
        return self.failure_reasons

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate": self.gate,
            "classification": self.classification,
            "checked": self.checked,
            "population": self.population,
            "missing": self.missing,
            "tolerance": self.tolerance,
            "failures": len(self.failure_reasons),
            "failure_reasons": list(self.failure_reasons),
            "exit_code": self.exit_code,
            "environment": self.environment,
            "details": dict(self.details),
        }


def exact_amount_gate(
    gate: str,
    text: str,
    contract: ExactAmountContract,
    *,
    environment: str = "live",
) -> GateResult:
    scoped_lines: list[str] = []
    scoped_amounts: list[Decimal] = []
    previous_scoped = False
    for raw_line in re.split(r"(?<=[.!?。！？])\s+|\n+", str(text or "")):
        line = raw_line.strip()
        if not line:
            continue
        fully_scoped = (
            _contains_any(line, contract.period_terms)
            and _contains_any(line, contract.subject_terms)
            and _contains_any(line, contract.metric_terms)
        )
        inherited_scope = previous_scoped and _CONTINUATION_RE.match(line) is not None
        if not fully_scoped and not inherited_scope:
            previous_scoped = False
            continue
        scoped_lines.append(line)
        scoped_amounts.extend(_decimal_tokens(_AMOUNT_RE.findall(line)))
        previous_scoped = fully_scoped or inherited_scope

    unique_amounts = sorted(set(scoped_amounts))
    failures: list[str] = []
    if not scoped_lines:
        failures.append("scoped_claim_missing")
    if not unique_amounts:
        failures.append("scoped_amount_missing")
    elif unique_amounts != [contract.expected_amount]:
        failures.append(
            "scoped_amount_mismatch:"
            f"actual={_decimal_strings(unique_amounts)}:"
            f"expected={_decimal_text(contract.expected_amount)}"
        )
    return GateResult(
        gate=gate,
        checked=0 if failures else 1,
        population=1,
        failure_reasons=tuple(failures),
        environment=environment,
        details={
            "expected_amount_억원": _decimal_text(contract.expected_amount),
            "scoped_lines": scoped_lines,
            "scoped_amounts_억원": _decimal_strings(unique_amounts),
        },
    )


def concentration_gate(
    gate: str,
    text: str,
    *,
    expected: Mapping[str, Decimal],
    environment: str = "live",
) -> GateResult:
    rendered = str(text or "")
    observed: dict[str, list[str]] = {}
    checks: dict[str, bool] = {}
    failures: list[str] = []
    for metric, expected_value in expected.items():
        pattern = re.compile(
            rf"(?i)(?<![A-Za-z0-9_]){re.escape(metric)}(?![A-Za-z0-9_])"
            r"\s*(?:값은|는|은|가|:|=)?\s*([0-9][0-9,]*(?:\.[0-9]+)?)\s*%?"
        )
        values = sorted(set(_decimal_tokens(pattern.findall(rendered))))
        observed[metric] = _decimal_strings(values)
        checks[metric] = values == [expected_value]
        if not values:
            failures.append(f"{metric}_missing")
        elif values != [expected_value]:
            failures.append(
                f"{metric}_mismatch:"
                f"actual={_decimal_strings(values)}:expected={_decimal_text(expected_value)}"
            )
    return GateResult(
        gate=gate,
        checked=sum(1 for passed in checks.values() if passed),
        population=len(expected),
        failure_reasons=tuple(failures),
        environment=environment,
        details={
            "expected": {key: _decimal_text(value) for key, value in expected.items()},
            "observed": observed,
            "checks": checks,
        },
    )


def derived_parity_gate(report: object, *, environment: str = "live") -> GateResult:
    checked = _integer_field(report, "checked")
    population = _integer_field(report, "population")
    source_failures = tuple(str(item) for item in _field(report, "failures", ()))
    source_exit_code = _integer_field(report, "exit_code")
    classification = str(_field(report, "classification", ""))
    failures: list[str] = []
    if classification != "census":
        failures.append(f"classification_not_census:{classification or '<missing>'}")
    if population <= 0:
        failures.append(f"population_not_positive:{population}")
    if checked != population:
        failures.append(f"checked_population_mismatch:{checked}/{population}")
    failures.extend(f"source_failure:{item}" for item in source_failures)
    if source_exit_code != 0:
        failures.append(f"source_exit_code:{source_exit_code}")
    return GateResult(
        gate="derived_parity",
        checked=checked,
        population=population,
        failure_reasons=tuple(failures),
        environment=environment,
    )


def strict_log_gate(
    text: str,
    statuses: Sequence[Mapping[str, object]],
    *,
    desired_replicas: int,
    environment: str = "live",
) -> GateResult:
    failures: list[str] = []
    if isinstance(desired_replicas, bool) or desired_replicas <= 0:
        failures.append(f"desired_population_not_positive:{desired_replicas}")

    pod_names = [str(status.get("pod") or "") for status in statuses]
    if len(statuses) != desired_replicas:
        failures.append(f"retrieved_population_mismatch:{len(statuses)}/{desired_replicas}")
    if any(not name for name in pod_names):
        failures.append("pod_name_missing")
    if len(set(pod_names)) != len(pod_names):
        failures.append("duplicate_pod_status")
    for status in statuses:
        pod = str(status.get("pod") or "<missing>")
        exit_code = _coerce_int(status.get("exit_code"), default=-1)
        stderr_bytes = _coerce_int(status.get("stderr_bytes"), default=-1)
        if exit_code != 0:
            failures.append(f"log_retrieval_failed:{pod}:exit={exit_code}")
        if stderr_bytes != 0:
            failures.append(f"log_retrieval_stderr:{pod}:bytes={stderr_bytes}")

    matches = [line for line in str(text or "").splitlines() if _STRICT_LOG_RE.search(line)]
    failures.extend(f"strict_log_match:{line}" for line in matches)
    return GateResult(
        gate="strict_logs",
        checked=len(statuses),
        population=desired_replicas,
        failure_reasons=tuple(failures),
        environment=environment,
        details={
            "pod_statuses": [dict(status) for status in statuses],
            "log_line_count": len(str(text or "").splitlines()),
            "match_count": len(matches),
            "matches": matches[:50],
        },
    )


def provenance_gate(
    gate: str,
    actual: Mapping[str, object],
    expected: Mapping[str, object],
    *,
    environment: str = "live",
) -> GateResult:
    failures: list[str] = []
    for key in sorted(expected.keys() - actual.keys()):
        failures.append(f"missing_field:{key}")
    for key in sorted(actual.keys() - expected.keys()):
        failures.append(f"unexpected_field:{key}")
    for key in sorted(expected.keys() & actual.keys()):
        if actual[key] != expected[key]:
            failures.append(
                f"field_mismatch:{key}:actual={actual[key]!r}:expected={expected[key]!r}"
            )
    population = len(expected)
    checked = population - sum(1 for failure in failures if not failure.startswith("unexpected_field:"))
    return GateResult(
        gate=gate,
        checked=max(0, checked),
        population=population,
        failure_reasons=tuple(failures),
        environment=environment,
        details={"actual": dict(actual), "expected": dict(expected)},
    )


def _contains_any(text: str, terms: Sequence[str]) -> bool:
    return any(term in text for term in terms)


def _decimal_tokens(tokens: Sequence[str]) -> list[Decimal]:
    values: list[Decimal] = []
    for token in tokens:
        try:
            values.append(Decimal(token.replace(",", "")))
        except InvalidOperation:
            continue
    return values


def _decimal_strings(values: Sequence[Decimal]) -> list[str]:
    return [_decimal_text(value) for value in values]


def _decimal_text(value: Decimal) -> str:
    return format(value, "f")


def _field(source: object, name: str, default: object) -> object:
    if isinstance(source, Mapping):
        return source.get(name, default)
    return getattr(source, name, default)


def _integer_field(source: object, name: str) -> int:
    return _coerce_int(_field(source, name, -1), default=-1)


def _coerce_int(value: object, *, default: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
