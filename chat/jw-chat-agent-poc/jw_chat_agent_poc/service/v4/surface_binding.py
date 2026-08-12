from __future__ import annotations

from collections.abc import Mapping, Sequence
from hashlib import sha256
import re
from typing import Any

from jw_chat_agent_poc.service.v4.lossless_contracts import EvidenceSet
from jw_chat_agent_poc.service.v4.retrieval_events import RetrievalEvent


_ENTITY_PATTERNS = (
    re.compile(r"\bNCT\d{8}\b", re.IGNORECASE),
    re.compile(r"\b[A-Z]{2,}[A-Z0-9]*-\d+[A-Za-z]?\b"),
    re.compile(r"\b[A-Z][A-Za-z]+(?:\s+[A-Za-z][A-Za-z0-9-]+)+\b"),
    re.compile(r"[가-힣A-Za-z0-9]{2,}(?:제약|바이오|약품|헬스케어)"),
)
_TABLE_DELIMITER_RE = re.compile(
    r"^\s*\|(?:\s*:?-{3,}:?\s*\|)+\s*$"
)


def sanitize_bound_surface(
    question: str,
    answer: str,
    evidence_sets: Sequence[EvidenceSet],
    retrieval_events: Sequence[RetrievalEvent],
) -> tuple[str, dict[str, Any]]:
    corpus = " ".join(
        (
            question,
            *(_payload_text(record.payload) for item in evidence_sets for record in item.records),
            *(
                f"{ref.title or ''} {ref.url}"
                for item in evidence_sets
                for ref in item.source_refs
            ),
            *(
                f"{ref.title or ''} {ref.url}"
                for item in evidence_sets
                for record in item.records
                for ref in record.source_refs
            ),
            *(event.entity_id or "" for event in retrieval_events),
        )
    ).casefold()
    output: list[str] = []
    removed_hashes: list[str] = []
    for line in answer.splitlines():
        if _is_structural_line(line):
            output.append(line)
            continue
        candidates = tuple(
            dict.fromkeys(
                (
                    *(
                        match.group(0).strip()
                        for pattern in _ENTITY_PATTERNS
                        for match in pattern.finditer(line)
                    ),
                    *_claimed_query_entities(line),
                )
            )
        )
        unsupported = tuple(item for item in candidates if item.casefold() not in corpus)
        if unsupported:
            removed_hashes.append(sha256(line.encode("utf-8")).hexdigest())
            continue
        output.append(line)
    sanitized = "\n".join(output)
    return sanitized, {
        "answer_mutation": sanitized != answer,
        "removed_unbound_lines": len(removed_hashes),
        "removed_line_sha256": removed_hashes,
    }


def _is_structural_line(line: str) -> bool:
    stripped = line.strip()
    return (
        not stripped
        or stripped.startswith("```")
        or bool(_TABLE_DELIMITER_RE.fullmatch(line))
    )


def _payload_text(value: Any) -> str:
    if isinstance(value, Mapping):
        return " ".join(f"{key} {_payload_text(item)}" for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return " ".join(_payload_text(item) for item in value)
    return str(value or "")


def _claimed_query_entities(line: str) -> tuple[str, ...]:
    match = re.search(r"질의에\s*포함된\s+(.+?)\s+관련", line)
    if match is None:
        return ()
    return tuple(
        value.strip(" ,·")
        for value in re.split(r"\s+(?:및|와|과)\s+|\s*[,，·]\s*", match.group(1))
        if value.strip(" ,·")
    )
