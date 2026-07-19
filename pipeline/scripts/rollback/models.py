from __future__ import annotations

from dataclasses import dataclass
from typing import Final


REQUIRED_COMPONENTS: Final[frozenset[str]] = frozenset(
    {"general", "strategic", "analysis_cache", "fdm"}
)


@dataclass(frozen=True, slots=True)
class TableBackup:
    live_table: str
    backup_table: str
    expected_rows: int
    expected_digest: str


@dataclass(frozen=True, slots=True)
class PromotionGeneration:
    promotion_run_id: str
    epoch: str
    ingest_run_id: str
    serving_db: str
    generation_db: str
    status: str
    promoted_at: str


@dataclass(frozen=True, slots=True)
class RollbackEvent:
    promotion_run_id: str
    actor: str
    reason: str
    rolled_back_at: str


@dataclass(frozen=True, slots=True)
class RollbackPlan:
    promotion_run_id: str
    target_db: str
    epoch: str
    ingest_run_id: str
    tables: tuple[TableBackup, ...]
    moves: tuple[tuple[str, str], ...]
    cache_tables: tuple[str, ...]
    warning: str


@dataclass(frozen=True, slots=True)
class RollbackResult:
    promotion_run_id: str
    changed: bool
    validated_tables: int


@dataclass(frozen=True, slots=True)
class RetentionPlan:
    protected_serving_db: str
    retained_generations: tuple[str, ...]
    generation_candidates: tuple[str, ...]
    retained_backup_runs: tuple[str, ...]
    backup_run_candidates: tuple[str, ...]
