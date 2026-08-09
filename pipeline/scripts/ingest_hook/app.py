"""Trigger service: webhook receiver + ledger status API (D-1 option (a)).

Deliberately NOT part of jw-market-backend-api — ingest load/failure must never
share a process, pod, or endpoint with serving (STOP ①). Runs from the same
pipeline-orchestrator image (fastapi/uvicorn/PyMySQL already included):

    uvicorn --factory pipeline.scripts.ingest_hook.app:build --port 8080

Endpoints (the site's whole contract surface):
  POST /ingest/webhook   {"manifest_path": "<path under INGEST_INPUT_ROOT>"}
  POST /ingest/terminal  (terminal completion signal from an ingest Job)
  GET  /ingest/queue     optional ?category=
  GET  /ingest/status    ?epoch=&category=&manifest_sha=
  GET  /ingest/history   ?limit=&offset=
  POST /ingest/force-stop (exact active run only)
  POST /ingest/reconcile  (promote queued submissions; sweep/ops helper)
  GET  /healthz
"""
from __future__ import annotations

import os
import threading
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from time import sleep as _sleep
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, model_validator

from pipeline.scripts.ingest_hook import config, job_launcher, job_runner, stage_logs
from pipeline.scripts.ingest_hook.category_map import UnknownCategoryError, resolve_category
from pipeline.scripts.ingest_hook.contract import ContractError, load_manifest, parse_manifest_bytes
from pipeline.scripts.ingest_hook.workbook_source_validation import (
    SourceValidationError,
    detect_workbook_source,
)
from pipeline.scripts.ingest_hook.ledger import (
    STATUS_AWAITING_APPROVAL,
    STATUS_COMPLETE,
    STATUS_FAILED,
    STATUS_GATE_FAILED,
    STATUS_PUBLISH_RUNNING,
    STATUS_QUEUED,
    STATUS_RUNNING,
    Ledger,
    LedgerConnectionError,
)
from pipeline.scripts.ingest_hook.source_inventory import (
    DEFAULT_INVENTORY_ROOT,
    SourceInventoryError,
    read_inventory_snapshot,
)

PORTAL_QUEUE_CATEGORIES = frozenset(
    {
        "ubist",
        "iqvia_nsa",
        "iqvia_csd_channel",
        "iqvia_csd_keyword",
        "mi_master",
    }
)
CONTENT_CLASSIFIED_CATEGORIES = frozenset(
    {"ubist", "iqvia_nsa", "iqvia_csd_channel", "iqvia_csd_keyword"}
)


def _utc_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


class WebhookPayload(BaseModel):
    manifest_path: str


class ForceStopPayload(BaseModel):
    epoch: str
    category: str
    manifest_sha: str
    run_id: str
    requested_by: str


class AffectedScopePayload(BaseModel):
    dimension: Literal["atc4", "source"]
    count: int = Field(ge=0)
    values: list[str]

    @model_validator(mode="after")
    def validate_snapshot(self) -> AffectedScopePayload:
        if self.count != len(self.values):
            raise ValueError("affected_scope count must match values")
        if any(not value.strip() for value in self.values):
            raise ValueError("affected_scope values must be non-empty")
        if len(set(self.values)) != len(self.values):
            raise ValueError("affected_scope values must be unique")
        return self


class TerminalPayload(BaseModel):
    event: str
    source: str | None = None
    category: str | None = None
    epoch: str
    manifest_sha: str
    mode: str = "unknown"
    schema_version: str | None = None
    event_id: str | None = None
    run_id: str | None = None
    target_schema: str | None = None
    published_at: str | None = None
    occurred_at: str | None = None
    period: str | dict | None = None
    rows_loaded: int | None = None
    affected_scope: AffectedScopePayload | None = None


class PublishApprovalPayload(BaseModel):
    epoch: str
    category: str
    manifest_sha: str
    run_id: str
    requested_by: str


class AutomaticPublishPayload(BaseModel):
    epoch: str
    category: str
    manifest_sha: str
    run_id: str


class CompleteReingestPayload(BaseModel):
    epoch: str
    category: str
    manifest_sha: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_id: str
    mode: Literal["mart_from_existing_raw"]
    requested_by: str = Field(min_length=1, max_length=64)
    reason: str = Field(min_length=1, max_length=4000)


class IngestService:
    def __init__(
        self,
        ledger: Ledger,
        input_root: Path | None,
        transport=None,
        s3=None,
        inspect_transport=None,
        delete_transport=None,
        list_transport=None,
        now=None,
        timestamp=None,
        sleep=None,
        deletion_attempts: int = 30,
        inventory_root: Path | None = None,
    ):
        self.ledger = ledger
        self.input_root = input_root
        self.transport = transport
        self.s3 = s3
        self.inspect_transport = inspect_transport
        self.delete_transport = delete_transport
        self.list_transport = list_transport
        self.now = now or (lambda: datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f"))
        self.timestamp = timestamp or (lambda: datetime.now(timezone.utc).isoformat())
        self.sleep = sleep or _sleep
        self.deletion_attempts = deletion_attempts
        self.inventory_root = inventory_root or DEFAULT_INVENTORY_ROOT
        self._promotion_locks: dict[str, threading.Lock] = {}
        self._promotion_locks_guard = threading.Lock()

    def _category_promotion_lock(self, category: str) -> threading.Lock:
        with self._promotion_locks_guard:
            return self._promotion_locks.setdefault(category, threading.Lock())

    def request_complete_reingest(self, payload: CompleteReingestPayload) -> dict:
        if payload.category not in {"ubist", "iqvia_nsa"}:
            raise HTTPException(
                status_code=422,
                detail="complete reingest supports ubist and iqvia_nsa only",
            )
        entry = self.ledger.status(
            payload.epoch,
            payload.category,
            payload.manifest_sha,
        )
        if entry is None:
            raise HTTPException(status_code=404, detail="unknown submission identity")
        if entry.status != STATUS_COMPLETE:
            raise HTTPException(
                status_code=409,
                detail="parent identity must be complete before mart_from_existing_raw",
            )
        blockers = self.ledger.blocking_entries(payload.category)
        if blockers:
            blocker = blockers[0]
            raise HTTPException(
                status_code=409,
                detail=(
                    "category has an active ingest or publish: "
                    f"status={blocker.status} run_id={blocker.run_id}"
                ),
            )
        try:
            canonical_request_id = str(uuid.UUID(payload.request_id))
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=422,
                detail="request_id must be a canonical UUID",
            ) from exc
        if canonical_request_id != payload.request_id:
            raise HTTPException(
                status_code=422,
                detail="request_id must be a canonical UUID",
            )
        run_id = self.now()
        if len(run_id) != 20 or not run_id.isdigit():
            raise RuntimeError("complete reingest attempt run_id must be exactly 20 digits")
        affected_scope = {
            "dimension": "source",
            "count": 1,
            "values": [payload.category],
        }
        job_image = config.job_image()
        image_digest = (
            f"sha256:{job_image.rsplit('@sha256:', 1)[1]}"
            if "@sha256:" in job_image
            else None
        )
        try:
            decision = self.ledger.record_complete_reingest_request(
                payload.epoch,
                payload.category,
                payload.manifest_sha,
                request_id=canonical_request_id,
                run_id=run_id,
                mode=payload.mode,
                requested_by=payload.requested_by,
                reason=payload.reason,
                affected_scope=affected_scope,
                code_revision=os.environ.get("APP_VERSION"),
                image_digest=image_digest,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        expected_name = job_launcher.complete_reingest_job_name(
            payload.category,
            payload.manifest_sha,
            decision.run_id,
        )
        if not decision.created:
            return {
                "action": "exists",
                "created": False,
                "request_id": decision.request_id,
                "run_id": decision.run_id,
                "job_name": expected_name,
                "affected_scope": affected_scope,
            }

        started_at = self.timestamp()
        try:
            submitted_name = job_launcher.submit_complete_reingest_job(
                epoch=payload.epoch,
                category=payload.category,
                manifest_sha=payload.manifest_sha,
                manifest_path=decision.manifest_path,
                request_id=decision.request_id,
                run_id=decision.run_id,
                affected_scope=affected_scope,
                transport=self.transport,
                inspect_transport=self.inspect_transport,
                list_transport=self.list_transport,
            )
            if submitted_name != expected_name:
                raise RuntimeError(
                    "complete reingest submission returned an unexpected name: "
                    f"expected={expected_name} actual={submitted_name}"
                )
        except Exception as exc:
            failure_reason = f"job submission failed: {type(exc).__name__}: {exc}"
            self.ledger.record_stage(
                payload.epoch,
                payload.category,
                payload.manifest_sha,
                run_id=decision.run_id,
                seq=0,
                stage="job_submit",
                status="failed",
                reason=failure_reason,
                started_at=started_at,
                finished_at=self.timestamp(),
            )
            self.ledger.record_complete_reingest_terminal(
                payload.epoch,
                payload.category,
                payload.manifest_sha,
                request_id=decision.request_id,
                run_id=decision.run_id,
                status=STATUS_FAILED,
                reason=failure_reason,
                actor="ingest_hook",
                job_name=expected_name,
                affected_scope=affected_scope,
            )
            raise
        self.ledger.record_stage(
            payload.epoch,
            payload.category,
            payload.manifest_sha,
            run_id=decision.run_id,
            seq=0,
            stage="job_submit",
            status="complete",
            started_at=started_at,
            finished_at=self.timestamp(),
        )
        self.ledger.record_stage(
            payload.epoch,
            payload.category,
            payload.manifest_sha,
            run_id=decision.run_id,
            seq=1,
            stage="request_validate",
            status="running",
            reason="waiting for complete reingest Job",
            started_at=self.timestamp(),
        )
        return {
            "action": "submitted",
            "created": True,
            "request_id": decision.request_id,
            "run_id": decision.run_id,
            "job_name": submitted_name,
            "affected_scope": affected_scope,
        }

    def drain_idle_queues(self) -> dict[str, dict[str, str]]:
        """Make one startup pass over queued categories missed by callbacks."""
        launched: dict[str, str] = {}
        automatic_publishes: dict[str, str] = {}
        errors: dict[str, str] = {}
        for candidate in self.ledger.awaiting_publish_candidates():
            if not isinstance(candidate.payload.get("automatic_publish"), dict):
                continue
            try:
                result = self.publish_automatic(
                    AutomaticPublishPayload(
                        epoch=candidate.epoch,
                        category=candidate.category,
                        manifest_sha=candidate.manifest_sha,
                        run_id=candidate.build_run_id,
                    )
                )
                automatic_publishes[candidate.category] = str(
                    result["publish_job_name"]
                )
            except Exception as exc:  # one category must not block startup
                errors[candidate.category] = f"{type(exc).__name__}: {exc}"
        categories = set(self.ledger.queued_categories())
        categories.update(
            candidate.category for candidate in self.ledger.awaiting_publish_candidates()
        )
        for category in sorted(categories):
            try:
                job_name = self.promote(category)
            except Exception as exc:  # one category must not block service startup
                errors[category] = f"{type(exc).__name__}: {exc}"
                continue
            if job_name is not None:
                launched[category] = job_name
        result = {"launched": launched, "errors": errors}
        if automatic_publishes:
            result["automatic_publishes"] = automatic_publishes
        return result

    # -- promotion: one running Job per category, FIFO within a category ----
    def promote(self, category: str) -> str | None:
        with self._category_promotion_lock(category):
            self._expire_publish_candidates(category)
            reconciliation = self.reconcile_terminal_jobs(
                category=category,
                promote_after=False,
            )
            if reconciliation["inspection_failures"]:
                raise RuntimeError(
                    "category terminal reconciliation inspection failed; "
                    "promotion remains blocked"
                )
            entry = self.ledger.next_queued(category)
            if entry is None:
                return None
            return self._claim_and_submit(entry)

    def promote_exact(self, epoch: str, category: str, manifest_sha: str) -> str | None:
        """Promote one exact queued identity while preserving category serialisation."""
        with self._category_promotion_lock(category):
            self._expire_publish_candidates(category)
            reconciliation = self.reconcile_terminal_jobs(
                category=category,
                promote_after=False,
            )
            if reconciliation["inspection_failures"]:
                raise RuntimeError(
                    "category terminal reconciliation inspection failed; "
                    "promotion remains blocked"
                )
            entry = self.ledger.status(epoch, category, manifest_sha)
            if entry is None:
                raise RuntimeError("exact promotion identity is absent from the ledger")
            if entry.status != STATUS_QUEUED:
                raise RuntimeError(
                    f"exact promotion requires queued status, got {entry.status!r}"
                )
            return self._claim_and_submit(entry)

    def _expire_publish_candidates(self, category: str) -> list[tuple[str, str, str]]:
        now = _utc_timestamp(self.timestamp())
        expired: list[tuple[str, str, str]] = []
        for candidate in self.ledger.awaiting_publish_candidates(category):
            if now <= _utc_timestamp(candidate.expires_at):
                continue
            if self.ledger.mark_publish_candidate_expired(
                candidate.epoch,
                candidate.category,
                candidate.manifest_sha,
                build_run_id=candidate.build_run_id,
                actor="ingest_hook",
            ):
                self._cleanup_expired_publish_candidate(candidate)
                expired.append(
                    (candidate.epoch, candidate.category, candidate.manifest_sha)
                )
        return expired

    @staticmethod
    def _cleanup_expired_publish_candidate(candidate) -> None:
        journal_value = str(candidate.payload.get("activation_journal") or "").strip()
        source_db = str(candidate.payload.get("source_db") or "").strip()
        if not journal_value or not source_db:
            return
        from pipeline.scripts.ingest_hook import ubist_mart_activation

        conn = config.open_mart_connection(source_db)
        try:
            ubist_mart_activation.discard_unpublished_candidate(
                conn,
                Path(journal_value),
            )
        finally:
            conn.close()

    def _claim_and_submit(self, entry) -> str | None:
        category = entry.category
        run_id = self.now()
        expected_name = job_launcher.job_name(
            category,
            entry.manifest_sha,
            run_id,
        )
        claimed = self.ledger.claim_queued(
            entry.epoch,
            category,
            entry.manifest_sha,
            job_name=expected_name,
            run_id=run_id,
        )
        if not claimed:
            return None
        started_at = datetime.now(timezone.utc).isoformat()
        try:
            name = job_launcher.submit_job(
                category=category,
                manifest_sha=entry.manifest_sha,
                manifest_path=entry.manifest_path,
                transport=self.transport,
                run_id=run_id,
                inspect_transport=self.inspect_transport,
            )
        except Exception as exc:  # noqa: BLE001 - transport is an external boundary
            reason = f"job submission failed: {type(exc).__name__}: {exc}"
            self.ledger.record_stage(
                entry.epoch,
                category,
                entry.manifest_sha,
                run_id=run_id,
                seq=0,
                stage="job_submit",
                status="failed",
                reason=reason,
                started_at=started_at,
                finished_at=datetime.now(timezone.utc).isoformat(),
            )
            self.ledger.mark_failed(
                entry.epoch,
                category,
                entry.manifest_sha,
                reason=reason,
            )
            raise
        if name != expected_name:
            reason = (
                "job submission returned an unexpected name: "
                f"expected={expected_name} actual={name}"
            )
            self.ledger.mark_failed(
                entry.epoch,
                category,
                entry.manifest_sha,
                reason=reason,
            )
            raise RuntimeError(reason)
        self.ledger.record_stage(
            entry.epoch,
            category,
            entry.manifest_sha,
            run_id=run_id,
            seq=0,
            stage="job_submit",
            status="complete",
            started_at=started_at,
            finished_at=datetime.now(timezone.utc).isoformat(),
        )
        return name

    def reconcile_terminal_jobs(
        self,
        category: str | None = None,
        *,
        promote_after: bool = True,
    ) -> dict:
        """Repair stale running rows from Kubernetes truth, then unblock FIFO.

        The ledger transition and append-only evidence are one DB transaction.
        Job creation is necessarily outside that transaction, so promotion uses
        the existing idempotent path immediately afterward. If the process dies
        between those steps, the queued row remains eligible for the next
        reconcile instead of leaving the category blocked by stale ``running``.

        A category-scoped pre-promotion pass disables recursive promotion while
        preserving the same terminal reconciliation contract.
        """
        actions: list[dict] = []
        reconciled = 0
        inspection_failures = 0
        entries = [
            *self.ledger.running_entries(category),
            *self.ledger.publish_running_entries(category),
        ]
        for entry in entries:
            if entry.job_name is None:
                observation = job_launcher.JobObservation(
                    status="Absent",
                    reason="ledger job_name missing",
                    evidence={
                        "job_name": None,
                        "job_status": "Absent",
                        "conditions": [],
                    },
                )
            else:
                try:
                    observation = job_launcher.inspect_job(
                        entry.job_name,
                        transport=self.inspect_transport,
                    )
                except Exception as exc:  # noqa: BLE001 - Kubernetes is an external boundary
                    inspection_failures += 1
                    actions.append(
                        {
                            "epoch": entry.epoch,
                            "category": entry.category,
                            "manifest_sha": entry.manifest_sha,
                            "job_name": entry.job_name,
                            "action": "inspection-failed",
                            "error": type(exc).__name__,
                        }
                    )
                    continue

            if observation.status in {"Pending", "Running"}:
                actions.append(
                    {
                        "epoch": entry.epoch,
                        "category": entry.category,
                        "manifest_sha": entry.manifest_sha,
                        "job_name": entry.job_name,
                        "action": "untouched",
                        "job_status": observation.status,
                    }
                )
                continue

            if observation.status == "Complete":
                ledger_status = STATUS_COMPLETE
                source = "kubernetes_job_terminal_present"
                reason = (
                    "terminal-present: Kubernetes Job Complete; runner callback "
                    "did not finalize the ledger"
                )
            elif observation.status == "Failed":
                ledger_status = STATUS_FAILED
                source = "kubernetes_job_terminal_present"
                detail = observation.reason or "condition reason unavailable"
                reason = f"terminal-present: Kubernetes Job Failed: {detail}"
            elif observation.status == "Absent":
                ledger_status = STATUS_FAILED
                source = "kubernetes_job_absent"
                reason = (
                    "job-absent: Kubernetes Job not found for a running ledger row; "
                    "it may have expired or been deleted"
                )
            else:
                actions.append(
                    {
                        "epoch": entry.epoch,
                        "category": entry.category,
                        "manifest_sha": entry.manifest_sha,
                        "job_name": entry.job_name,
                        "action": "inspection-failed",
                        "error": f"unsupported status {observation.status}",
                    }
                )
                inspection_failures += 1
                continue

            if (
                entry.status == STATUS_PUBLISH_RUNNING
                and observation.status in {"Failed", "Absent"}
            ):
                try:
                    self._recover_publish_activation(entry)
                except Exception as exc:  # publish recovery must fail closed
                    inspection_failures += 1
                    actions.append(
                        {
                            "epoch": entry.epoch,
                            "category": entry.category,
                            "manifest_sha": entry.manifest_sha,
                            "job_name": entry.job_name,
                            "action": "recovery-failed",
                            "error": type(exc).__name__,
                        }
                    )
                    continue

            changed = self.ledger.reconcile_terminal(
                entry.epoch,
                entry.category,
                entry.manifest_sha,
                status=ledger_status,
                reason=reason,
                actor="terminal_job_reconciler",
                source=source,
                evidence=observation.evidence,
                expected_job_name=entry.job_name,
                expected_run_id=entry.run_id,
                expected_status=entry.status,
            )
            if not changed:
                actions.append(
                    {
                        "epoch": entry.epoch,
                        "category": entry.category,
                        "manifest_sha": entry.manifest_sha,
                        "job_name": entry.job_name,
                        "action": "state-changed-concurrently",
                        "job_status": observation.status,
                    }
                )
                continue

            reconciled += 1
            promoted = self.promote(entry.category) if promote_after else None
            actions.append(
                {
                    "epoch": entry.epoch,
                    "category": entry.category,
                    "manifest_sha": entry.manifest_sha,
                    "job_name": entry.job_name,
                    "action": "reconciled",
                    "job_status": observation.status,
                    "ledger_status": ledger_status,
                    "promoted_job_name": promoted,
                }
            )
        return {
            "checked": len(actions),
            "reconciled": reconciled,
            "inspection_failures": inspection_failures,
            "actions": actions,
        }

    def _recover_publish_activation(self, entry) -> None:
        from pipeline.scripts.ingest_hook import ubist_mart_activation

        candidate = self.ledger.prepared_candidate(
            entry.epoch,
            entry.category,
            entry.manifest_sha,
        )
        if candidate is None:
            raise RuntimeError("publish recovery candidate is absent")
        payload = candidate.payload
        journal = Path(str(payload["activation_journal"]))
        target_db = str(payload["target_db"])
        mode = str(payload.get("mode") or "production")
        spec = resolve_category(entry.category)
        conn = config.open_mart_connection(target_db)
        lock_name = (
            ubist_mart_activation.shadow_lock_name(target_db)
            if mode == "shadow"
            else ubist_mart_activation.WRITER_LOCK_NAME
        )
        acquired = False
        failure_reason = None
        try:
            ubist_mart_activation.acquire_writer_lock(
                conn,
                timeout_seconds=0,
                lock_name=lock_name,
            )
            acquired = True
            recovered = ubist_mart_activation.recover_incomplete_activations(
                conn,
                output_root=journal.parent,
            )
            if recovered:
                if mode == "shadow":
                    activation = ubist_mart_activation.MartActivation(
                        str(payload["source_db"]),
                        target_db,
                        str(payload["build_db"]),
                    )
                    ubist_mart_activation.validate_shadow_publish(conn, activation)
                else:
                    job_runner._run_commands_with_writer_lock(
                        "refresh-recovery",
                        spec.refresh_argv,
                        connection=conn,
                        lock_name=lock_name,
                    )
                ubist_mart_activation.complete_recovery(recovered)
        except Exception as exc:
            failure_reason = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            if acquired:
                job_runner._release_writer_lock_preserving_primary(
                    conn,
                    lock_name=lock_name,
                    primary_failure_reason=failure_reason,
                )
            conn.close()

    def force_stop(
        self,
        *,
        epoch: str,
        category: str,
        manifest_sha: str,
        run_id: str,
        requested_by: str,
    ) -> dict:
        """Stop one exact active run and reconcile only that ledger identity."""
        entry = self.ledger.status(epoch, category, manifest_sha)
        if entry is None:
            raise HTTPException(status_code=404, detail="unknown submission identity")
        if entry.status not in {STATUS_RUNNING, STATUS_PUBLISH_RUNNING}:
            raise HTTPException(
                status_code=409,
                detail=f"force stop requires an active ledger row, got {entry.status}",
            )
        if entry.run_id != run_id:
            raise HTTPException(status_code=409, detail="run_id does not match the active ledger row")
        if entry.status == STATUS_PUBLISH_RUNNING:
            candidate = self.ledger.prepared_candidate(epoch, category, manifest_sha)
            expected_name = candidate.publish_job_name if candidate is not None else None
        else:
            expected_name = job_launcher.job_name(category, manifest_sha, run_id)
        if not expected_name:
            raise HTTPException(
                status_code=409,
                detail="active publish candidate has no deterministic Job identity",
            )
        if entry.job_name != expected_name:
            raise HTTPException(
                status_code=409,
                detail="ledger job_name does not match the deterministic run identity",
            )
        actor = requested_by.strip()
        if not actor:
            raise HTTPException(status_code=422, detail="requested_by is required")

        observation = job_launcher.inspect_job(
            expected_name,
            transport=self.inspect_transport,
        )
        if observation.status not in {"Pending", "Running"}:
            raise HTTPException(
                status_code=409,
                detail=f"force stop requires an active Kubernetes Job, got {observation.status}",
            )
        try:
            job_launcher.delete_job(
                expected_name,
                observation=observation,
                transport=self.delete_transport,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

        terminal_observation = observation
        for attempt in range(self.deletion_attempts):
            terminal_observation = job_launcher.inspect_job(
                expected_name,
                transport=self.inspect_transport,
            )
            if terminal_observation.status == "Absent":
                break
            if attempt + 1 < self.deletion_attempts:
                self.sleep(1)
        if terminal_observation.status != "Absent":
            raise HTTPException(
                status_code=503,
                detail="Kubernetes Job deletion was not confirmed; ledger remains running",
            )

        if entry.status == STATUS_PUBLISH_RUNNING:
            try:
                self._recover_publish_activation(entry)
            except Exception as exc:
                raise HTTPException(
                    status_code=503,
                    detail="publish Job stopped but activation recovery did not complete",
                ) from exc

        stopped_at = self.timestamp()
        reason = (
            f"PL 강제 정지: 요청자={actor}, 시각={stopped_at}, "
            f"Kubernetes Job={expected_name} 삭제"
        )
        evidence = {
            **observation.evidence,
            "run_id": run_id,
            "deleted_job_status": terminal_observation.status,
            "requested_by": actor,
            "requested_at": stopped_at,
        }
        changed = self.ledger.reconcile_terminal(
            epoch,
            category,
            manifest_sha,
            status=STATUS_FAILED,
            reason=reason,
            actor=actor,
            source="manual_force_stop",
            evidence=evidence,
            expected_job_name=expected_name,
            expected_run_id=run_id,
            expected_status=entry.status,
        )
        if not changed:
            raise HTTPException(
                status_code=409,
                detail="ledger state changed concurrently; no queued submission was promoted",
            )
        promoted = self.promote(category)
        return {
            "epoch": epoch,
            "category": category,
            "manifest_sha": manifest_sha,
            "run_id": run_id,
            "job_name": expected_name,
            "job_status": terminal_observation.status,
            "status": STATUS_FAILED,
            "reason": reason,
            "promoted_job_name": promoted,
        }

    def _read_manifest(self, manifest_path: str):
        if self.s3 is not None:
            key = manifest_path.lstrip("/")
            try:
                return parse_manifest_bytes(self.s3.read(key), manifest_path=key), key
            except FileNotFoundError as exc:
                raise HTTPException(status_code=404, detail=f"manifest not found in bucket: {key}") from exc
        path = (self.input_root / manifest_path).resolve() if not Path(manifest_path).is_absolute() else Path(manifest_path).resolve()
        root = self.input_root.resolve()
        if root not in path.parents and path != root:
            raise HTTPException(status_code=400, detail="manifest_path escapes the input root")
        return load_manifest(path), str(path)

    def _validate_before_queue(self, manifest) -> None:
        """Reject a workbook whose headers contradict the selected source."""
        def validate_root(root: Path) -> None:
            workbook_entries = [
                entry for entry in manifest.files if Path(entry.path).suffix.lower() == ".xlsx"
            ]
            # Portal sources are workbooks. Legacy CSV webhook fixtures retain
            # their existing in-Job G3 behavior and are not content-classified.
            if not workbook_entries:
                return
            if manifest.category in CONTENT_CLASSIFIED_CATEGORIES:
                detected: set[str] = set()
                for entry in workbook_entries:
                    path = (root / entry.path).resolve()
                    try:
                        detected.add(detect_workbook_source(path))
                    except SourceValidationError as exc:
                        raise HTTPException(
                            status_code=422,
                            detail={
                                "code": "source_unrecognized",
                                "selected_category": manifest.category,
                                "detected_category": None,
                                "message": f"파일 내용으로 소스를 판별할 수 없습니다: {exc}",
                            },
                        ) from exc
                if detected and detected != {manifest.category}:
                    detected_label = ",".join(sorted(detected))
                    raise HTTPException(
                        status_code=422,
                        detail={
                            "code": "source_category_mismatch",
                            "selected_category": manifest.category,
                            "detected_category": detected_label,
                            "message": "선택한 소스와 파일 내용이 일치하지 않습니다.",
                        },
                    )
        if self.s3 is None:
            if self.input_root is None:
                raise HTTPException(status_code=500, detail="ingest input root is not configured")
            validate_root(self.input_root)
            return
        with TemporaryDirectory(prefix="ingest-prequeue-") as temp_root:
            root = self.s3.materialize([entry.path for entry in manifest.files], Path(temp_root))
            validate_root(root)

    def receive_webhook(self, manifest_path: str) -> dict:
        try:
            manifest, stored_path = self._read_manifest(manifest_path)
        except ContractError as exc:
            raise HTTPException(status_code=422, detail=f"contract violation: {exc}") from exc
        if not manifest.complete:
            raise HTTPException(status_code=409, detail="manifest is not marked complete; webhook is submit-confirm only")
        try:
            resolve_category(manifest.category)
        except UnknownCategoryError as exc:
            # Reject retired/unknown categories before they can create new
            # ledger history. Existing rows remain queryable through status().
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        self._validate_before_queue(manifest)

        decision = self.ledger.receive(
            manifest.epoch,
            manifest.category,
            manifest.manifest_sha,
            manifest_path=stored_path,
            uploaded_by=manifest.uploaded_by,
        )
        launched = None
        if decision.action == "queued":
            launched = (
                self.promote_exact(
                    manifest.epoch,
                    manifest.category,
                    manifest.manifest_sha,
                )
                if config.webhook_promote_exact()
                else self.promote(manifest.category)
            )
        return {
            "epoch": manifest.epoch,
            "category": manifest.category,
            "manifest_sha": manifest.manifest_sha,
            "decision": decision.action,
            "status": decision.status,
            "reason": decision.reason,
            "job_name": launched,
        }

    def approve_publish(self, payload: PublishApprovalPayload) -> dict:
        actor = payload.requested_by.strip()
        if not actor:
            raise HTTPException(status_code=422, detail="requested_by is required")
        entry = self.ledger.status(
            payload.epoch,
            payload.category,
            payload.manifest_sha,
        )
        if entry is None:
            raise HTTPException(status_code=404, detail="unknown submission identity")
        candidate = self.ledger.prepared_candidate(
            payload.epoch,
            payload.category,
            payload.manifest_sha,
        )
        if candidate is None:
            raise HTTPException(status_code=409, detail="publish candidate is absent")
        if candidate.build_run_id != payload.run_id:
            raise HTTPException(status_code=409, detail="run_id does not match prepared candidate")
        now = self.timestamp()
        if _utc_timestamp(now) > _utc_timestamp(candidate.expires_at):
            expired = self.ledger.mark_publish_candidate_expired(
                payload.epoch,
                payload.category,
                payload.manifest_sha,
                build_run_id=payload.run_id,
                actor=actor,
            )
            if expired:
                try:
                    self._cleanup_expired_publish_candidate(candidate)
                except Exception as exc:  # cleanup is required before releasing residue
                    raise HTTPException(
                        status_code=500,
                        detail=(
                            "publish candidate expired but cleanup requires "
                            "reconciliation"
                        ),
                    ) from exc
            if not expired:
                entry = self.ledger.status(
                    payload.epoch,
                    payload.category,
                    payload.manifest_sha,
                )
                candidate = self.ledger.prepared_candidate(
                    payload.epoch,
                    payload.category,
                    payload.manifest_sha,
                )
                if (
                    entry is not None
                    and entry.status == STATUS_PUBLISH_RUNNING
                    and candidate is not None
                    and candidate.publish_job_name
                ):
                    return {
                        "accepted": True,
                        "status": STATUS_PUBLISH_RUNNING,
                        "publish_job_name": candidate.publish_job_name,
                        "idempotent": True,
                    }
            raise HTTPException(status_code=409, detail="publish candidate expired")
        if entry.status == STATUS_PUBLISH_RUNNING and candidate.publish_job_name:
            return {
                "accepted": True,
                "status": STATUS_PUBLISH_RUNNING,
                "publish_job_name": candidate.publish_job_name,
                "idempotent": True,
            }
        if entry.status != STATUS_AWAITING_APPROVAL:
            raise HTTPException(
                status_code=409,
                detail=f"publish approval requires awaiting_approval, got {entry.status}",
            )
        publish_run_id = self.now()
        expected_name = job_launcher.publish_job_name(
            payload.category,
            payload.manifest_sha,
            publish_run_id,
        )
        changed = self.ledger.mark_publish_running(
            payload.epoch,
            payload.category,
            payload.manifest_sha,
            build_run_id=payload.run_id,
            publish_job_name=expected_name,
            approved_by=actor,
            approved_at=now,
        )
        if not changed:
            refreshed = self.ledger.prepared_candidate(
                payload.epoch,
                payload.category,
                payload.manifest_sha,
            )
            if refreshed and refreshed.publish_job_name:
                return {
                    "accepted": True,
                    "status": STATUS_PUBLISH_RUNNING,
                    "publish_job_name": refreshed.publish_job_name,
                    "idempotent": True,
                }
            if refreshed and _utc_timestamp(now) > _utc_timestamp(refreshed.expires_at):
                expired = self.ledger.mark_publish_candidate_expired(
                    payload.epoch,
                    payload.category,
                    payload.manifest_sha,
                    build_run_id=payload.run_id,
                    actor=actor,
                )
                if expired:
                    self._cleanup_expired_publish_candidate(refreshed)
                raise HTTPException(status_code=409, detail="publish candidate expired")
            raise HTTPException(status_code=409, detail="publish candidate changed concurrently")
        try:
            name = job_launcher.submit_publish_job(
                epoch=payload.epoch,
                category=payload.category,
                manifest_sha=payload.manifest_sha,
                build_run_id=payload.run_id,
                publish_run_id=publish_run_id,
                transport=self.transport,
                inspect_transport=self.inspect_transport,
            )
        except Exception as exc:  # noqa: BLE001 - Kubernetes is an external boundary
            try:
                observation = job_launcher.inspect_job(
                    expected_name,
                    transport=self.inspect_transport,
                )
            except Exception as inspection_exc:  # preserve ambiguous publish ownership
                raise HTTPException(
                    status_code=500,
                    detail=(
                        "publish Job submission outcome is unknown and requires "
                        "reconciliation"
                    ),
                ) from inspection_exc
            if observation.status != "Absent":
                if observation.status in {"Pending", "Running"}:
                    return {
                        "accepted": True,
                        "status": STATUS_PUBLISH_RUNNING,
                        "publish_job_name": expected_name,
                        "idempotent": True,
                        "submission_reconciled": True,
                    }
                self.reconcile_terminal_jobs(
                    category=payload.category,
                    promote_after=False,
                )
                refreshed_entry = self.ledger.status(
                    payload.epoch,
                    payload.category,
                    payload.manifest_sha,
                )
                if refreshed_entry is not None and refreshed_entry.status == STATUS_COMPLETE:
                    return {
                        "accepted": True,
                        "status": STATUS_COMPLETE,
                        "publish_job_name": expected_name,
                        "idempotent": True,
                        "submission_reconciled": True,
                    }
                raise HTTPException(
                    status_code=500,
                    detail="publish Job is terminal and requires reconciliation",
                ) from exc
            restored = self.ledger.restore_awaiting_approval_after_submit_failure(
                payload.epoch,
                payload.category,
                payload.manifest_sha,
                build_run_id=payload.run_id,
                publish_job_name=expected_name,
            )
            if not restored:
                raise HTTPException(
                    status_code=500,
                    detail="publish Job submission failed and ledger requires reconciliation",
                ) from exc
            raise HTTPException(
                status_code=503,
                detail="publish Job submission failed; approval remains retryable",
            ) from exc
        if name != expected_name:
            restored = self.ledger.restore_awaiting_approval_after_submit_failure(
                payload.epoch,
                payload.category,
                payload.manifest_sha,
                build_run_id=payload.run_id,
                publish_job_name=expected_name,
            )
            if not restored:
                raise HTTPException(
                    status_code=500,
                    detail="unexpected publish Job identity requires reconciliation",
                )
            raise HTTPException(status_code=502, detail="publish Job identity mismatch")
        return {
            "accepted": True,
            "status": STATUS_PUBLISH_RUNNING,
            "publish_job_name": name,
            "idempotent": False,
        }

    def publish_automatic(self, payload: AutomaticPublishPayload) -> dict:
        """Publish one exact prepared identity only after every hard gate passed."""
        candidate = self.ledger.prepared_candidate(
            payload.epoch,
            payload.category,
            payload.manifest_sha,
        )
        if candidate is None or candidate.build_run_id != payload.run_id:
            raise HTTPException(status_code=409, detail="automatic publish candidate mismatch")
        contract = candidate.payload.get("automatic_publish")
        if not isinstance(contract, dict):
            raise HTTPException(status_code=409, detail="automatic publish evidence is absent")
        hard_gates = contract.get("hard_gates")
        required = {f"PG-{index}" for index in range(1, 6)}
        if not isinstance(hard_gates, dict) or any(
            hard_gates.get(gate) != "pass" for gate in required
        ):
            raise HTTPException(status_code=409, detail="automatic publish hard gates did not pass")
        candidate_integrity = candidate.payload.get("candidate_integrity")
        build_integrity = candidate.payload.get("build_table_integrity")
        csd_integrity = candidate.payload.get("csd_candidate_evidence")
        keyword_integrity = candidate.payload.get("keyword_candidate_evidence")
        has_ubist_integrity = (
            isinstance(candidate_integrity, dict)
            and isinstance(build_integrity, list)
            and bool(build_integrity)
        )
        has_csd_integrity = (
            isinstance(csd_integrity, dict)
            and isinstance(csd_integrity.get("raw"), dict)
            and isinstance(csd_integrity.get("stage"), dict)
        )
        has_keyword_integrity = (
            isinstance(keyword_integrity, dict)
            and int(keyword_integrity.get("raw_rows", 0)) > 0
            and int(keyword_integrity.get("stage_rows", 0)) > 0
        )
        if not has_ubist_integrity and not has_csd_integrity and not has_keyword_integrity:
            raise HTTPException(status_code=409, detail="automatic publish integrity evidence is absent")
        return self.approve_publish(
            PublishApprovalPayload(
                epoch=payload.epoch,
                category=payload.category,
                manifest_sha=payload.manifest_sha,
                run_id=payload.run_id,
                requested_by="system:full-scan-auto-publish",
            )
        )


def create_app(service: IngestService) -> FastAPI:
    def inventory_summary(entry) -> dict[str, object]:
        empty = {
            "inventory_run_id": None,
            "file_count": None,
            "classified_file_count": None,
            "inventory_file_counts": None,
        }
        if entry is None or not entry.run_id:
            return empty
        try:
            snapshot = read_inventory_snapshot(
                service.inventory_root,
                category=entry.category,
                epoch=entry.epoch,
                manifest_sha=entry.manifest_sha,
                run_id=entry.run_id,
            )
        except (FileNotFoundError, SourceInventoryError):
            return empty
        files = snapshot.get("files")
        if not isinstance(files, list):
            return empty
        counts: dict[str, int] = {}
        for file in files:
            if not isinstance(file, dict):
                continue
            state = file.get("state")
            if isinstance(state, str):
                counts[state] = counts.get(state, 0) + 1
        return {
            "inventory_run_id": entry.run_id,
            "file_count": len(files),
            "classified_file_count": counts.get("classified", 0),
            "inventory_file_counts": counts,
        }

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.startup_queue_drain = service.drain_idle_queues()
        yield

    app = FastAPI(
        title="jw-ingest-hook",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )

    @app.exception_handler(LedgerConnectionError)
    def _ledger_connection_error(request: Request, exc: LedgerConnectionError) -> JSONResponse:
        # The ingest ledger's mysql connection could not be revived (ping +
        # reconnect + one retry all failed). Fail loud with a clear body so the
        # caller/site sees the cause — never a silent success. The webhook is
        # safely retriable: a stale-connection failure writes no ledger row.
        return JSONResponse(
            status_code=500,
            content={"detail": f"ingest ledger database unavailable: {exc}"},
        )

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.post("/ingest/webhook")
    def webhook(payload: WebhookPayload) -> dict:
        return service.receive_webhook(payload.manifest_path)

    @app.post("/ingest/reingest", status_code=202)
    def complete_reingest(payload: CompleteReingestPayload) -> dict:
        return service.request_complete_reingest(payload)

    @app.get("/ingest/queue")
    def queue(category: str | None = None) -> dict:
        if category is not None and category not in PORTAL_QUEUE_CATEGORIES:
            return {"items": []}
        entries = service.ledger.active_entries(category)
        entries = [
            entry for entry in entries if entry.category in PORTAL_QUEUE_CATEGORIES
        ]
        blocking_categories = {
            entry.category
            for entry in service.ledger.blocking_entries(category)
            if entry.category in PORTAL_QUEUE_CATEGORIES
        }
        return {
            "items": [
                {
                    "epoch": entry.epoch,
                    "category": entry.category,
                    "manifest_sha": entry.manifest_sha,
                    "status": entry.status,
                    "reason": entry.reason,
                    "job_name": entry.job_name,
                    "run_id": entry.run_id,
                    "uploaded_by": entry.uploaded_by,
                    "received_at": entry.received_at,
                    "started_at": entry.started_at,
                    "finished_at": entry.finished_at,
                    "blocked_by_category": (
                        entry.status == STATUS_QUEUED
                        and entry.category in blocking_categories
                    ),
                    "requires_reconcile": (
                        entry.status == STATUS_QUEUED
                        and entry.category not in blocking_categories
                    ),
                }
                for entry in entries
            ]
        }

    @app.get("/ingest/status")
    def status(epoch: str, category: str, manifest_sha: str) -> dict:
        entry = service.ledger.status(epoch, category, manifest_sha)
        if entry is None:
            raise HTTPException(status_code=404, detail="unknown submission identity")
        # Additive only (backward compatible): existing keys are unchanged; the
        # stage list / current_stage / log_ref are new. stage_events reads are
        # best-effort — a stage-table read failure degrades to an empty list, never
        # a 500, so status stays available even before the stage table is activated.
        try:
            events = service.ledger.stage_events(epoch, category, manifest_sha)
        except Exception:  # noqa: BLE001 — observation must not break status
            events = []
        try:
            signals = service.ledger.signal_events(epoch, category, manifest_sha)
        except Exception:  # observation remains additive and best-effort
            signals = []
        try:
            expected = job_runner.expected_stages(resolve_category(entry.category))
        except UnknownCategoryError:
            expected = []
        current_stage = None
        if entry.status == STATUS_RUNNING and entry.run_id:
            current_stage = next(
                (
                    event.stage
                    for event in reversed(events)
                    if (
                        event.run_id == entry.run_id
                        or event.run_id.startswith(f"{entry.run_id}:")
                    )
                    and event.status == "running"
                ),
                None,
            )
        category_running = service.ledger.blocking_entries(entry.category)
        blocked_by_category = (
            entry.status in {STATUS_QUEUED, STATUS_AWAITING_APPROVAL}
            and bool(category_running)
        )
        blocker = category_running[0] if blocked_by_category else None
        candidate = service.ledger.prepared_candidate(epoch, category, manifest_sha)
        now = service.timestamp()
        summary = inventory_summary(entry)
        return {
            "epoch": entry.epoch,
            "category": entry.category,
            "manifest_sha": entry.manifest_sha,
            "status": entry.status,
            "reason": entry.reason,
            "job_name": entry.job_name,
            "uploaded_by": entry.uploaded_by,
            "received_at": entry.received_at,
            "finished_at": entry.finished_at,
            "row_counts": entry.row_counts,
            **summary,
            # -- new (additive) --
            "blocked_by_category": blocked_by_category,
            "requires_reconcile": (
                entry.status == STATUS_QUEUED and not blocked_by_category
            ),
            "category_blocker": (
                {
                    "epoch": blocker.epoch,
                    "manifest_sha": blocker.manifest_sha,
                    "run_id": blocker.run_id,
                    "job_name": blocker.job_name,
                }
                if blocker is not None
                else None
            ),
            "current_stage": current_stage,
            "expected_stages": expected,
            "stages": [
                {
                    "run_id": event.run_id,
                    "seq": event.seq,
                    "stage": event.stage,
                    "status": event.status,
                    "reason": event.reason,
                    "started_at": event.started_at,
                    "finished_at": event.finished_at,
                    "duration_ms": event.duration_ms,
                }
                for event in events
            ],
            "signals": [
                {
                    "run_id": signal.run_id,
                    "event": signal.event,
                    "mode": signal.mode,
                    "rows_loaded": signal.rows_loaded,
                    "delivery_status": signal.delivery_status,
                    "attempts": signal.attempts,
                    "reason": signal.reason,
                    "payload": signal.payload,
                    "created_at": signal.created_at,
                }
                for signal in signals
            ],
            "prepared": (
                {
                    "run_id": candidate.build_run_id,
                    "prepared_at": candidate.prepared_at,
                    "expires_at": candidate.expires_at,
                    "expired": now > candidate.expires_at,
                    "publish_job_name": candidate.publish_job_name,
                }
                if candidate is not None
                else None
            ),
            "log_ref": {
                "job_name": entry.job_name,
                "run_id": entry.run_id,
                # The body is exposed separately through a bounded, paged API.
                "durable_log_hint": (
                    f"{config.log_root_hint()}/{entry.job_name}/"
                    if entry.job_name else None
                ),
                "endpoint": "/ingest/logs" if entry.job_name else None,
            },
        }

    @app.get("/ingest/history")
    def history(
        limit: int = Query(default=100, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ) -> dict:
        identities, has_more = service.ledger.history_identities(
            limit=limit, offset=offset
        )
        items = []
        entry_cache = {}
        event_cache = {}
        transition_cache = {}
        summary_cache: dict[tuple[str, str, str], dict[str, object]] = {}
        for identity in identities:
            identity_key = (identity.epoch, identity.category, identity.manifest_sha)
            if identity_key not in entry_cache:
                entry_cache[identity_key] = service.ledger.status(*identity_key)
            entry = entry_cache[identity_key]
            if identity_key not in event_cache:
                event_cache[identity_key] = service.ledger.stage_events(*identity_key)
            if identity_key not in transition_cache:
                transition_cache[identity_key] = service.ledger.status_transitions(
                    *identity_key
                )
            identity_events = event_cache[identity_key]
            identity_transitions = transition_cache[identity_key]
            events = [
                event
                for event in identity_events
                if event.run_id == identity.run_id
            ]
            run_transitions = [
                transition
                for transition in identity_transitions
                if transition.evidence.get("run_id") == identity.run_id
            ]
            reingest_request = next(
                (
                    transition
                    for transition in run_transitions
                    if transition.source == "complete_reingest_request"
                ),
                None,
            )
            reingest_terminal = next(
                (
                    transition
                    for transition in reversed(run_transitions)
                    if transition.source == "complete_reingest_terminal"
                ),
                None,
            )
            reingest = None
            if reingest_request is not None:
                request_evidence = reingest_request.evidence
                attempt_status = (
                    reingest_terminal.status
                    if reingest_terminal is not None
                    else "running"
                )
                reingest = {
                    "request_id": request_evidence.get("request_id"),
                    "mode": request_evidence.get("mode"),
                    "requested_by": reingest_request.actor,
                    "reason": reingest_request.reason,
                    "affected_scope": request_evidence.get("affected_scope"),
                    "code_revision": request_evidence.get("code_revision"),
                    "image_digest": request_evidence.get("image_digest"),
                    "status": attempt_status,
                    "terminal_reason": (
                        reingest_terminal.reason
                        if reingest_terminal is not None
                        else None
                    ),
                    "job_name": (
                        reingest_terminal.job_name
                        if reingest_terminal is not None
                        else job_launcher.complete_reingest_job_name(
                            identity.category,
                            identity.manifest_sha,
                            identity.run_id,
                        )
                    ),
                }
            run_entry = entry if entry is not None and entry.run_id == identity.run_id else None
            if identity_key not in summary_cache:
                summary_cache[identity_key] = inventory_summary(entry)
            summary = summary_cache[identity_key]
            items.append(
                {
                    "epoch": identity.epoch,
                    "category": identity.category,
                    "manifest_sha": identity.manifest_sha,
                    "run_id": identity.run_id,
                    "observed_at": identity.observed_at,
                    "row_counts": entry.row_counts if entry is not None else None,
                    **summary,
                    "ledger": (
                        {
                            "status": run_entry.status,
                            "reason": run_entry.reason,
                            "job_name": run_entry.job_name,
                            "uploaded_by": run_entry.uploaded_by,
                            "received_at": run_entry.received_at,
                            "started_at": run_entry.started_at,
                            "finished_at": run_entry.finished_at,
                        }
                        if run_entry is not None
                        else None
                    ),
                    "reingest": reingest,
                    "stages": [
                        {
                            "seq": event.seq,
                            "stage": event.stage,
                            "status": event.status,
                            "reason": event.reason,
                            "started_at": event.started_at,
                            "finished_at": event.finished_at,
                            "duration_ms": event.duration_ms,
                        }
                        for event in events
                    ],
                    "identity_stages": [
                        {
                            "run_id": event.run_id,
                            "seq": event.seq,
                            "stage": event.stage,
                            "status": event.status,
                            "reason": event.reason,
                            "started_at": event.started_at,
                            "finished_at": event.finished_at,
                            "duration_ms": event.duration_ms,
                        }
                        for event in identity_events
                    ],
                }
            )
        return {
            "items": items,
            "next_offset": offset + limit if has_more else None,
        }

    @app.get("/ingest/inventory")
    def inventory(
        epoch: str,
        category: str,
        manifest_sha: str,
        run_id: str,
    ) -> dict:
        try:
            return read_inventory_snapshot(
                service.inventory_root,
                category=category,
                epoch=epoch,
                manifest_sha=manifest_sha,
                run_id=run_id,
            )
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=404, detail="inventory snapshot not recorded"
            ) from exc
        except SourceInventoryError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/ingest/terminal")
    def terminal(payload: TerminalPayload) -> dict:
        category = payload.source or payload.category
        if category is None:
            raise HTTPException(status_code=422, detail="source is required")
        if payload.source is not None and payload.category not in {None, payload.source}:
            raise HTTPException(
                status_code=422,
                detail="source and legacy category must match",
            )
        if category not in {
            "ubist",
            "iqvia_nsa",
            "iqvia_csd_channel",
            "iqvia_csd_keyword",
        }:
            raise HTTPException(
                status_code=422,
                detail=f"unsupported completion source {category!r}",
            )
        if payload.schema_version not in {None, "1"}:
            raise HTTPException(
                status_code=422,
                detail=f"unsupported completion schema_version {payload.schema_version!r}",
            )
        if payload.schema_version == "1":
            required_v1 = {
                "event_id": payload.event_id,
                "run_id": payload.run_id,
                "occurred_at": payload.occurred_at,
                "period": payload.period,
                "rows_loaded": payload.rows_loaded,
            }
            missing = sorted(
                name for name, value in required_v1.items() if value is None or value == ""
            )
            if payload.event == "complete":
                if not payload.target_schema:
                    missing.append("target_schema")
                if not payload.published_at:
                    missing.append("published_at")
            if missing:
                raise HTTPException(
                    status_code=422,
                    detail=f"missing completion v1 fields: {', '.join(sorted(missing))}",
                )
        expected_status_by_event = {
            "complete": STATUS_COMPLETE,
            "failed": STATUS_FAILED,
            "gate_failed": STATUS_GATE_FAILED,
        }
        expected_status = expected_status_by_event.get(payload.event)
        if expected_status is None:
            raise HTTPException(
                status_code=422,
                detail=f"unsupported terminal event {payload.event!r}",
            )
        entry = service.ledger.status(
            payload.epoch,
            category,
            payload.manifest_sha,
        )
        if entry is None:
            raise HTTPException(status_code=404, detail="unknown submission identity")
        if entry.status != expected_status:
            raise HTTPException(
                status_code=409,
                detail=(
                    "terminal callback requires the ledger slot to be released: "
                    f"event={payload.event} ledger_status={entry.status}"
                ),
            )
        agent_job_name = None
        agent_trigger_status = "not_applicable"
        agent_trigger_reason = None
        if payload.event == "complete" and payload.mode == "production":
            try:
                agent_job_name = job_launcher.submit_agent_refresh_job(
                    epoch=payload.epoch,
                    category=category,
                    manifest_sha=payload.manifest_sha,
                    ingest_run_id=entry.run_id,
                    transport=service.transport,
                    inspect_transport=service.inspect_transport,
                    list_transport=service.list_transport,
                    affected_scope=(
                        payload.affected_scope.model_dump()
                        if payload.affected_scope is not None
                        else None
                    ),
                )
                if agent_job_name is None:
                    timestamp = service.timestamp()
                    service.ledger.record_stage(
                        payload.epoch,
                        category,
                        payload.manifest_sha,
                        run_id=f"{entry.run_id}:agent-refresh",
                        seq=1,
                        stage="agent_refresh",
                        status="skipped",
                        reason="not_applicable",
                        started_at=timestamp,
                        finished_at=timestamp,
                        duration_ms=0,
                    )
                else:
                    agent_trigger_status = "submitted"
            except job_launcher.AgentRefreshCapacityError as exc:
                agent_trigger_status = "deferred_capacity"
                agent_trigger_reason = str(exc)
            except Exception as exc:  # agent work is a separate failure domain
                agent_trigger_status = "failed"
                agent_trigger_reason = type(exc).__name__
        promoted = service.promote(category)
        return {
            "accepted": True,
            "category": category,
            "terminal_status": entry.status,
            "promoted_job_name": promoted,
            "agent_job_name": agent_job_name,
            "agent_trigger_status": agent_trigger_status,
            "agent_trigger_reason": agent_trigger_reason,
        }

    @app.post("/ingest/publish/approve")
    def approve_publish(payload: PublishApprovalPayload) -> dict:
        return service.approve_publish(payload)

    @app.post("/ingest/publish/automatic")
    def publish_automatic(payload: AutomaticPublishPayload) -> dict:
        return service.publish_automatic(payload)

    @app.get("/ingest/logs")
    def logs(
        epoch: str,
        category: str,
        manifest_sha: str,
        run_id: str,
        stage: str | None = None,
        offset: int = 0,
        limit: int = 65536,
    ) -> dict:
        entry = service.ledger.status(epoch, category, manifest_sha)
        if entry is None:
            raise HTTPException(status_code=404, detail="unknown submission identity")
        events = service.ledger.stage_events(epoch, category, manifest_sha)
        if run_id != entry.run_id and not any(event.run_id == run_id for event in events):
            raise HTTPException(status_code=404, detail="unknown run_id")
        name = job_launcher.job_name(category, manifest_sha, run_id)
        try:
            page = stage_logs.read_log_page(
                config.log_root(),
                job_name=name,
                stage=stage,
                offset=offset,
                limit=limit,
            )
        except FileNotFoundError:
            raise HTTPException(
                status_code=404,
                detail={
                    "reason": "log_not_available",
                    "message": "The durable log is absent or has expired.",
                },
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {
            "epoch": epoch,
            "category": category,
            "manifest_sha": manifest_sha,
            "run_id": run_id,
            "stage": stage,
            "text": page.text,
            "total_bytes": page.total_bytes,
            "next_offset": page.next_offset,
            "truncated": page.truncated,
        }

    @app.post("/ingest/force-stop")
    def force_stop(payload: ForceStopPayload) -> dict:
        return service.force_stop(
            epoch=payload.epoch,
            category=payload.category,
            manifest_sha=payload.manifest_sha,
            run_id=payload.run_id,
            requested_by=payload.requested_by,
        )

    @app.post("/ingest/reconcile")
    def reconcile() -> dict:
        terminal = service.reconcile_terminal_jobs()
        categories = set(service.ledger.queued_categories())
        categories.update(
            candidate.category
            for candidate in service.ledger.awaiting_publish_candidates()
        )
        launched = {
            category: name
            for category in sorted(categories)
            if (name := service.promote(category)) is not None
        }
        return {"terminal": terminal, "launched": launched}

    return app


def build() -> FastAPI:
    """uvicorn --factory entrypoint (production wiring from env)."""
    # sqlite ledgers self-create; the mysql ingest_ledger DDL is applied
    # manually at activation (PL gate) — never implicitly from service boot.
    ledger = config.open_configured_ledger()
    s3 = config.open_input_source()
    input_root = None if s3 is not None else config.input_root()
    return create_app(IngestService(ledger, input_root, s3=s3))
