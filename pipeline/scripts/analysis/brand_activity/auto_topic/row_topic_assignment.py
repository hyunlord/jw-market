from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Final


PROMPT_VERSION: Final = "row_topic_v1"


class AssignmentParseError(RuntimeError):
    """Raised when a model response cannot be used without guessing."""


@dataclass(frozen=True, slots=True)
class TopicRubric:
    """One fixed topic that may be assigned to keyword rows."""

    topic_id: str
    label: str
    definition: str
    keywords: tuple[str, ...] = ()
    brand: str = ""


@dataclass(frozen=True, slots=True)
class AssignmentInputRow:
    """One keyword-stage row used for row-level topic assignment."""

    row_id: int
    scope_id: str
    brand: str
    keyword_text: str
    stage_row_sha256: str = ""
    period_ym: str = ""
    visit_location: str = ""
    specialty: str = ""
    interest: str = ""
    prescription_evolution: str = ""


@dataclass(frozen=True, slots=True)
class RowTopicAssignment:
    """One normalized row-topic yes/no assignment."""

    row_id: int
    scope_id: str
    brand: str
    topic_id: str
    topic_set_version: str
    prompt_version: str
    batch_id: str


@dataclass(frozen=True, slots=True)
class AssignmentParseResult:
    """Parsed assignments plus row ids that still need a no-guess fallback."""

    assignments: list[RowTopicAssignment]
    missing_row_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class AssignmentFilters:
    """Optional local aggregation filters for row-topic shares."""

    period_from: str = ""
    period_to: str = ""
    visit_locations: tuple[str, ...] = ()
    specialties: tuple[str, ...] = ()
    interests: tuple[str, ...] = ()
    prescription_evolutions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TopicShareSummary:
    """Payload-compatible affected-row share for one topic."""

    topic_id: str
    label: str
    affected_row_count: int
    share_pct: float

    def as_payload(self) -> dict[str, str | int | float]:
        """Return the legacy topic_shares item shape."""
        return {
            "topic_id": self.topic_id,
            "label": self.label,
            "affected_row_count": self.affected_row_count,
            "share_pct": self.share_pct,
        }


def row_topic_prompt(rubric: tuple[TopicRubric, ...], rows: tuple[AssignmentInputRow, ...]) -> list[dict[str, str]]:
    """Build the v1 independent yes/no topic-assignment prompt."""
    topic_lines = [
        f"- {topic.topic_id} | {topic.label} | {topic.definition} | keywords={', '.join(topic.keywords)}"
        for topic in rubric
    ]
    row_lines = [f"{row.row_id}\t{row.brand}\t{_compact(row.keyword_text)}" for row in rows]
    return [
        {
            "role": "system",
            "content": (
                "You assign fixed topic IDs to Korean pharmaceutical keyword-event rows. "
                "Do not create new topics. Use [] when no listed topic is substantially conveyed. "
                "Each row-topic judgment is independent yes/no; one row may carry multiple topics. "
                "Return compact JSON only."
            ),
        },
        {
            "role": "user",
            "content": (
                "Assign every input row_id exactly once as "
                "{\"assignments\":[{\"row_id\":123,\"topics\":[\"T1\"]}]}.\n"
                "Criterion: does this row substantially convey this topic, not merely mention market background?\n\n"
                "Rubric:\n"
                + "\n".join(topic_lines)
                + "\n\nRows (row_id, brand, keyword_text):\n"
                + "\n".join(row_lines)
            ),
        },
    ]


def parse_assignment_response(
    content: str,
    rows: list[AssignmentInputRow],
    known_topic_ids: set[str],
    topic_set_version: str,
    batch_id: str,
) -> list[RowTopicAssignment]:
    """Parse and enforce exact id echo, known topic ids, and no guessing."""
    parsed = parse_assignment_response_allow_missing(content, rows, known_topic_ids, topic_set_version, batch_id)
    if parsed.missing_row_ids:
        raise AssignmentParseError(f"missing row_id(s): {list(parsed.missing_row_ids)[:10]}")
    return parsed.assignments


def parse_assignment_response_allow_missing(
    content: str,
    rows: list[AssignmentInputRow],
    known_topic_ids: set[str],
    topic_set_version: str,
    batch_id: str,
) -> AssignmentParseResult:
    """Parse assignments while surfacing omitted row ids for a smaller fallback call."""
    payload = _parse_json_object(content)
    items = payload.get("assignments")
    if not isinstance(items, list):
        raise AssignmentParseError("assignments must be a list")
    expected_ids = {row.row_id for row in rows}
    seen_ids: set[int] = set()
    row_by_id = {row.row_id: row for row in rows}
    assignments: list[RowTopicAssignment] = []
    for value in items:
        if not isinstance(value, dict):
            raise AssignmentParseError("assignment item must be an object")
        row_id = _parse_row_id(value.get("row_id"))
        if row_id in seen_ids:
            raise AssignmentParseError(f"duplicate row_id: {row_id}")
        seen_ids.add(row_id)
        if row_id not in expected_ids:
            raise AssignmentParseError(f"unexpected row_id: {row_id}")
        topics = value.get("topics")
        if not isinstance(topics, list):
            raise AssignmentParseError(f"topics must be a list for row_id: {row_id}")
        normalized_topics = _normalize_topics(topics)
        unknown = sorted({topic for topic in normalized_topics if topic not in known_topic_ids})
        if unknown:
            raise AssignmentParseError(f"unknown topic for row_id {row_id}: {unknown}")
        row = row_by_id[row_id]
        assignments.extend(
            RowTopicAssignment(
                row_id=row_id,
                scope_id=row.scope_id,
                brand=row.brand,
                topic_id=topic,
                topic_set_version=topic_set_version,
                prompt_version=PROMPT_VERSION,
                batch_id=batch_id,
            )
            for topic in normalized_topics
        )
    missing = sorted(expected_ids - seen_ids)
    return AssignmentParseResult(assignments=assignments, missing_row_ids=tuple(missing))


def aggregate_topic_shares(
    rows: list[AssignmentInputRow],
    assignments: list[RowTopicAssignment],
    *,
    labels: dict[str, str] | None = None,
    filters: AssignmentFilters | None = None,
) -> list[TopicShareSummary]:
    """Aggregate independent row-topic assignments into legacy share items."""
    filtered_ids = {row.row_id for row in rows if _matches(row, filters or AssignmentFilters())}
    denominator = len(filtered_ids)
    if denominator == 0:
        return []
    counts: dict[str, set[int]] = {}
    for assignment in assignments:
        if assignment.row_id in filtered_ids:
            counts.setdefault(assignment.topic_id, set()).add(assignment.row_id)
    label_map = labels or {}
    return [
        TopicShareSummary(
            topic_id=topic_id,
            label=label_map.get(topic_id, ""),
            affected_row_count=len(row_ids),
            share_pct=round(len(row_ids) * 100.0 / denominator, 2),
        )
        for topic_id, row_ids in sorted(counts.items(), key=lambda item: (-len(item[1]), item[0]))
    ]


def _parse_json_object(content: str) -> dict[str, object]:
    stripped = content.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`").strip()
        if stripped.lower().startswith("json"):
            stripped = stripped[4:].strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end < start:
        raise AssignmentParseError("response has no JSON object")
    try:
        value = json.loads(stripped[start : end + 1])
    except json.JSONDecodeError as exc:
        raise AssignmentParseError(f"invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise AssignmentParseError("response JSON must be an object")
    return value


def _parse_row_id(value: object) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    raise AssignmentParseError(f"invalid row_id: {value}")


def _normalize_topics(topics: list[object]) -> list[str]:
    """Normalize explicit none sentinels without inventing topic assignments."""
    normalized: list[str] = []
    for topic in topics:
        if topic == "[]":
            continue
        if isinstance(topic, str):
            normalized.append(topic)
            continue
        normalized.append("")
    return normalized


def _matches(row: AssignmentInputRow, filters: AssignmentFilters) -> bool:
    return (
        (not filters.period_from or row.period_ym >= filters.period_from)
        and (not filters.period_to or row.period_ym <= filters.period_to)
        and (not filters.visit_locations or row.visit_location in filters.visit_locations)
        and (not filters.specialties or row.specialty in filters.specialties)
        and (not filters.interests or row.interest in filters.interests)
        and (not filters.prescription_evolutions or row.prescription_evolution in filters.prescription_evolutions)
    )


def _compact(value: str) -> str:
    return " ".join(value.split())
