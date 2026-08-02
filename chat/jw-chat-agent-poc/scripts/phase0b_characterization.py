from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any


SNAPSHOT_FIELDS = (
    "case_id",
    "question",
    "router_planner",
    "tool_call_fingerprints",
    "evidence_facts",
    "gate_decisions",
    "disposition",
    "failure_kind",
    "reason_codes",
    "final_answer",
    "final_answer_sha256",
)


class MissingCassetteError(LookupError):
    """Raised when replay has no exact request match."""


class CharacterizationMismatch(AssertionError):
    """Raised when a path or value differs from its snapshot."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class CassetteEntry:
    dependency: str
    operation: str
    request: dict[str, Any]
    response: Any
    request_sha256: str
    response_sha256: str
    recorded_from: str


class ReplayCassette:
    """Exact-match replay store with no live fallback."""

    def __init__(self, entries: tuple[CassetteEntry, ...]) -> None:
        self.entries = entries
        self.live_fallback_attempts = 0
        self._index = {
            (entry.dependency, entry.operation, entry.request_sha256): entry
            for entry in entries
        }
        if len(self._index) != len(entries):
            raise ValueError("duplicate cassette request key")

    @classmethod
    def from_path(cls, path: Path) -> ReplayCassette:
        payload = json.loads(path.read_text(encoding="utf-8"))
        entries = tuple(CassetteEntry(**raw) for raw in payload["entries"])
        cassette = cls(entries)
        for entry in entries:
            if fingerprint(entry.request) != entry.request_sha256:
                raise ValueError(f"request checksum mismatch: {entry.operation}")
            if fingerprint(entry.response) != entry.response_sha256:
                raise ValueError(f"response checksum mismatch: {entry.operation}")
        return cassette

    def replay(self, dependency: str, operation: str, request: dict[str, Any]) -> Any:
        key = (dependency, operation, fingerprint(request))
        entry = self._index.get(key)
        if entry is None:
            raise MissingCassetteError(
                f"no cassette entry for dependency={dependency} operation={operation} "
                f"request_sha256={key[2]}"
            )
        return deepcopy(entry.response)


def snapshot_from_observation(observation: dict[str, Any]) -> dict[str, Any]:
    snapshot = {field: deepcopy(observation.get(field)) for field in SNAPSHOT_FIELDS}
    answer = str(snapshot["final_answer"] or "")
    snapshot["final_answer_sha256"] = hashlib.sha256(answer.encode("utf-8")).hexdigest()
    return snapshot


def compare_snapshot(expected: dict[str, Any], actual: dict[str, Any]) -> None:
    differing = [field for field in SNAPSHOT_FIELDS if expected.get(field) != actual.get(field)]
    if differing:
        raise CharacterizationMismatch("characterization fields changed: " + ", ".join(differing))
