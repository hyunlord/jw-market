"""Atomic publication gates for MI Master refresh candidates."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Mapping

from pipeline.etl.mi_master_refresh.contracts import (
    MiMasterRefreshCandidate,
    RefreshPublishPlan,
    RefreshPublishResult,
)
from pipeline.etl.mi_master_refresh.provenance import (
    validate_candidate_approval_identity,
    validate_candidate_seed,
)


def validate_refresh_publish_plan(plan: RefreshPublishPlan) -> None:
    validate_candidate_seed(plan.candidate)
    if plan.corpus is None:
        raise ValueError("publish plan requires candidate and backup corpus")
    if plan.approval_identity is None:
        raise ValueError("publish plan requires approval identity")
    validate_candidate_approval_identity(plan.candidate, plan.approval_identity)
    if not plan.corpus.candidate_dir.is_dir() or not plan.corpus.backup_dir.is_dir():
        raise ValueError("publish plan corpus paths must exist")
    if plan.corpus.candidate_dir != plan.candidate_dir:
        raise ValueError("publish plan candidate corpus does not match candidate_dir")
    if not plan.journal_path.is_file():
        raise ValueError("publish plan requires pre-created journal")


def atomic_publish_candidate(plan: RefreshPublishPlan) -> RefreshPublishResult:
    recover_incomplete_publish(plan)
    validate_refresh_publish_plan(plan)
    if not plan.candidate_dir.is_dir():
        raise ValueError(f"candidate_dir is not a directory: {plan.candidate_dir}")
    if not plan.live_dir.is_dir():
        raise ValueError(f"live_dir is not a directory: {plan.live_dir}")
    backup_target = plan.backup_dir / plan.candidate.candidate_id
    if backup_target.exists():
        raise ValueError(f"backup already exists: {backup_target}")
    plan.backup_dir.mkdir(parents=True, exist_ok=True)
    plan.journal_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(plan.live_dir, backup_target)
    _append_journal(
        plan.journal_path,
        "backup_created",
        plan.candidate,
        {"backup_dir": str(backup_target)},
    )
    temporary_live = _temporary_live_path(plan)
    if temporary_live.exists():
        shutil.rmtree(temporary_live)
    shutil.copytree(plan.candidate_dir, temporary_live)
    old_live = _old_live_path(plan)
    if old_live.exists():
        shutil.rmtree(old_live)
    _append_journal(
        plan.journal_path,
        "publish_swap_started",
        plan.candidate,
        {"old_live": str(old_live), "temporary_live": str(temporary_live)},
    )
    os.replace(plan.live_dir, old_live)
    try:
        os.replace(temporary_live, plan.live_dir)
    except BaseException:
        _restore_old_live(plan, old_live)
        raise
    shutil.rmtree(old_live)
    _append_journal(
        plan.journal_path,
        "candidate_published",
        plan.candidate,
        {"live_dir": str(plan.live_dir)},
    )
    return RefreshPublishResult(plan.live_dir, backup_target, plan.journal_path)


def recover_incomplete_publish(plan: RefreshPublishPlan) -> None:
    old_live = _old_live_path(plan)
    if plan.live_dir.exists() or not old_live.exists():
        return
    _restore_old_live(plan, old_live)


def _restore_old_live(plan: RefreshPublishPlan, old_live: Path) -> None:
    if plan.live_dir.exists():
        return
    os.replace(old_live, plan.live_dir)
    _append_journal(
        plan.journal_path,
        "publish_recovered",
        plan.candidate,
        {"live_dir": str(plan.live_dir)},
    )


def _temporary_live_path(plan: RefreshPublishPlan) -> Path:
    return (
        plan.live_dir.parent
        / f".{plan.live_dir.name}.{plan.candidate.candidate_id}.tmp"
    )


def _old_live_path(plan: RefreshPublishPlan) -> Path:
    return plan.live_dir.parent / f".{plan.live_dir.name}.{plan.candidate.candidate_id}.old"


def _append_journal(
    path: Path,
    event: str,
    candidate: MiMasterRefreshCandidate,
    extra: Mapping[str, str],
) -> None:
    payload = {
        "event": event,
        "candidate_id": candidate.candidate_id,
        "mi_master_sha256": candidate.mi_master_sha256,
        "manifest_sha256": candidate.manifest_sha256,
        **dict(extra),
    }
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
