from __future__ import annotations

from dataclasses import dataclass
import hashlib
from itertools import groupby


class SemanticOccurrenceConflict(RuntimeError):
    """Raised when equal semantic events receive different classifications."""


@dataclass(frozen=True, slots=True)
class SemanticOccurrence:
    stage_generation_id: str
    stage_row_id: int
    semantic_event_key_v1: str
    scope_id: str
    brand: str


@dataclass(frozen=True, slots=True)
class OccurrenceResult:
    stage_row_id: int
    topic_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SemanticAssignment:
    semantic_event_key_v1: str
    scope_id: str
    brand: str
    topic_id: str
    topic_set_version: str
    prompt_version: str
    batch_id: str


@dataclass(frozen=True, slots=True)
class SemanticStatus:
    semantic_event_key_v1: str
    scope_id: str
    topic_set_version: str
    classified_stage_generation_id: str
    prompt_version: str
    batch_id: str
    status: str
    assignment_count: int


@dataclass(frozen=True, slots=True)
class SemanticBatch:
    batch_id: str
    wave_no: int
    batch_ordinal: int
    scope_id: str
    brand: str
    occurrence_sha256: str
    occurrences: tuple[SemanticOccurrence, ...]


@dataclass(frozen=True, slots=True)
class CanonicalSemanticResult:
    semantic_event_key_v1: str
    scope_id: str
    topic_set_version: str
    topic_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SemanticWorkSelection:
    classification_batch: SemanticBatch | None
    reused_occurrence_count: int
    covered_occurrence_count: int


@dataclass(frozen=True, slots=True)
class ReconciledBatch:
    assignments: tuple[SemanticAssignment, ...]
    statuses: tuple[SemanticStatus, ...]
    covered_occurrence_count: int
    covered_stage_row_ids: tuple[int, ...]


def build_semantic_batches(
    occurrences: tuple[SemanticOccurrence, ...],
    *,
    topic_set_version: str,
    prompt_version: str,
    wave_no: int,
    batch_size: int,
) -> tuple[SemanticBatch, ...]:
    """Build deterministic occurrence-preserving scope/brand batches."""
    if batch_size <= 0:
        raise ValueError("batch_size must be positive")
    ordered = sorted(occurrences, key=lambda item: (item.scope_id, item.brand, item.stage_row_id))
    batches: list[SemanticBatch] = []
    for (scope_id, brand), grouped in groupby(ordered, key=lambda item: (item.scope_id, item.brand)):
        group = tuple(grouped)
        for ordinal, offset in enumerate(range(0, len(group), batch_size), start=1):
            chunk = group[offset : offset + batch_size]
            batches.append(
                _semantic_batch(
                    occurrences=chunk,
                    topic_set_version=topic_set_version,
                    prompt_version=prompt_version,
                    wave_no=wave_no,
                    scope_id=scope_id,
                    brand=brand,
                    batch_ordinal=ordinal,
                )
            )
    return tuple(batches)


def select_semantic_work(
    batch: SemanticBatch,
    *,
    topic_set_version: str,
    existing_results: tuple[CanonicalSemanticResult, ...],
) -> SemanticWorkSelection:
    """Select one representative per unclassified semantic identity."""
    existing_identities = {
        (item.semantic_event_key_v1, item.scope_id, item.topic_set_version)
        for item in existing_results
    }
    representatives: dict[tuple[str, str, str], SemanticOccurrence] = {}
    reused_occurrences = 0
    for occurrence in sorted(batch.occurrences, key=lambda item: item.stage_row_id):
        identity = (
            occurrence.semantic_event_key_v1,
            occurrence.scope_id,
            topic_set_version,
        )
        if identity in existing_identities:
            reused_occurrences += 1
            continue
        representatives.setdefault(identity, occurrence)

    selected = tuple(representatives[identity] for identity in sorted(representatives))
    classification_batch = None
    if selected:
        classification_batch = SemanticBatch(
            batch_id=batch.batch_id,
            wave_no=batch.wave_no,
            batch_ordinal=batch.batch_ordinal,
            scope_id=batch.scope_id,
            brand=batch.brand,
            occurrence_sha256=_occurrence_hash(selected),
            occurrences=selected,
        )
    return SemanticWorkSelection(
        classification_batch=classification_batch,
        reused_occurrence_count=reused_occurrences,
        covered_occurrence_count=len(batch.occurrences),
    )


def rewave_semantic_batch(
    batch: SemanticBatch,
    *,
    topic_set_version: str,
    prompt_version: str,
    wave_no: int,
) -> SemanticBatch:
    """Reidentify an already chunked batch for its deterministic global wave."""
    return _semantic_batch(
        occurrences=batch.occurrences,
        topic_set_version=topic_set_version,
        prompt_version=prompt_version,
        wave_no=wave_no,
        scope_id=batch.scope_id,
        brand=batch.brand,
        batch_ordinal=batch.batch_ordinal,
    )


def reconcile_occurrence_results(
    occurrences: tuple[SemanticOccurrence, ...],
    results: tuple[OccurrenceResult, ...],
    *,
    topic_set_version: str,
    prompt_version: str,
    batch_id: str,
) -> ReconciledBatch:
    """Collapse stable classifications while retaining occurrence coverage."""
    occurrence_by_id = {item.stage_row_id: item for item in occurrences}
    result_by_id = {item.stage_row_id: tuple(sorted(set(item.topic_ids))) for item in results}
    if len(occurrence_by_id) != len(occurrences) or len(result_by_id) != len(results):
        raise SemanticOccurrenceConflict("SEMANTIC_OCCURRENCE_CONFLICT: duplicate stage_row_id")
    if set(occurrence_by_id) != set(result_by_id):
        raise SemanticOccurrenceConflict("SEMANTIC_OCCURRENCE_CONFLICT: result occurrence set mismatch")

    topics_by_identity: dict[tuple[str, str], tuple[str, ...]] = {}
    representative: dict[tuple[str, str], SemanticOccurrence] = {}
    for row_id in sorted(occurrence_by_id):
        occurrence = occurrence_by_id[row_id]
        identity = (occurrence.semantic_event_key_v1, occurrence.scope_id)
        topics = result_by_id[row_id]
        previous = topics_by_identity.setdefault(identity, topics)
        if previous != topics:
            raise SemanticOccurrenceConflict(
                "SEMANTIC_OCCURRENCE_CONFLICT: equal semantic key has different topic sets"
            )
        representative.setdefault(identity, occurrence)

    assignments: list[SemanticAssignment] = []
    statuses: list[SemanticStatus] = []
    for identity in sorted(topics_by_identity):
        occurrence = representative[identity]
        topics = topics_by_identity[identity]
        assignments.extend(
            SemanticAssignment(
                semantic_event_key_v1=occurrence.semantic_event_key_v1,
                scope_id=occurrence.scope_id,
                brand=occurrence.brand,
                topic_id=topic_id,
                topic_set_version=topic_set_version,
                prompt_version=prompt_version,
                batch_id=batch_id,
            )
            for topic_id in topics
        )
        statuses.append(
            SemanticStatus(
                semantic_event_key_v1=occurrence.semantic_event_key_v1,
                scope_id=occurrence.scope_id,
                topic_set_version=topic_set_version,
                classified_stage_generation_id=occurrence.stage_generation_id,
                prompt_version=prompt_version,
                batch_id=batch_id,
                status="classified" if topics else "unresolved_missing",
                assignment_count=len(topics),
            )
        )
    covered_ids = tuple(sorted(occurrence_by_id))
    return ReconciledBatch(tuple(assignments), tuple(statuses), len(covered_ids), covered_ids)


def _occurrence_hash(occurrences: tuple[SemanticOccurrence, ...]) -> str:
    digest = hashlib.sha256()
    for item in occurrences:
        digest.update(
            f"{item.stage_generation_id}|{item.stage_row_id}|{item.semantic_event_key_v1}\n".encode("ascii")
        )
    return digest.hexdigest()


def _semantic_batch(
    *,
    occurrences: tuple[SemanticOccurrence, ...],
    topic_set_version: str,
    prompt_version: str,
    wave_no: int,
    scope_id: str,
    brand: str,
    batch_ordinal: int,
) -> SemanticBatch:
    occurrence_sha256 = _occurrence_hash(occurrences)
    identity = "\x1f".join(
        (
            occurrences[0].stage_generation_id,
            topic_set_version,
            prompt_version,
            str(wave_no),
            scope_id,
            brand,
            str(batch_ordinal),
            occurrence_sha256,
        )
    )
    return SemanticBatch(
        batch_id=hashlib.sha256(identity.encode("utf-8")).hexdigest(),
        wave_no=wave_no,
        batch_ordinal=batch_ordinal,
        scope_id=scope_id,
        brand=brand,
        occurrence_sha256=occurrence_sha256,
        occurrences=occurrences,
    )
