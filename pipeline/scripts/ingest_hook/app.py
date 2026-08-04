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
  POST /ingest/force-stop (exact active run only)
  POST /ingest/reconcile  (promote queued submissions; sweep/ops helper)
  GET  /healthz
"""
from __future__ import annotations

import threading
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from time import sleep as _sleep

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from pipeline.scripts.ingest_hook import config, job_launcher, job_runner, stage_logs
from pipeline.scripts.ingest_hook.category_map import UnknownCategoryError, resolve_category
from pipeline.scripts.ingest_hook.contract import ContractError, load_manifest, parse_manifest_bytes
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

PORTAL_QUEUE_CATEGORIES = frozenset(
    {
        "ubist",
        "iqvia_nsa",
        "iqvia_csd_channel",
        "iqvia_csd_keyword",
        "mi_master",
    }
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


class TerminalPayload(BaseModel):
    event: str
    category: str
    epoch: str
    manifest_sha: str
    mode: str = "unknown"


class PublishApprovalPayload(BaseModel):
    epoch: str
    category: str
    manifest_sha: str
    run_id: str
    requested_by: str


class IngestService:
    def __init__(
        self,
        ledger: Ledger,
        input_root: Path | None,
        transport=None,
        s3=None,
        inspect_transport=None,
        delete_transport=None,
        now=None,
        timestamp=None,
        sleep=None,
        deletion_attempts: int = 30,
    ):
        self.ledger = ledger
        self.input_root = input_root
        self.transport = transport
        self.s3 = s3
        self.inspect_transport = inspect_transport
        self.delete_transport = delete_transport
        self.now = now or (lambda: datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f"))
        self.timestamp = timestamp or (lambda: datetime.now(timezone.utc).isoformat())
        self.sleep = sleep or _sleep
        self.deletion_attempts = deletion_attempts
        self._promotion_locks: dict[str, threading.Lock] = {}
        self._promotion_locks_guard = threading.Lock()

    def _category_promotion_lock(self, category: str) -> threading.Lock:
        with self._promotion_locks_guard:
            return self._promotion_locks.setdefault(category, threading.Lock())

    def drain_idle_queues(self) -> dict[str, dict[str, str]]:
        """Make one startup pass over queued categories missed by callbacks."""
        launched: dict[str, str] = {}
        errors: dict[str, str] = {}
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
        return {"launched": launched, "errors": errors}

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


def create_app(service: IngestService) -> FastAPI:
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

    @app.post("/ingest/terminal")
    def terminal(payload: TerminalPayload) -> dict:
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
            payload.category,
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
                    category=payload.category,
                    manifest_sha=payload.manifest_sha,
                    ingest_run_id=entry.run_id,
                    transport=service.transport,
                    inspect_transport=service.inspect_transport,
                )
                agent_trigger_status = "submitted"
            except Exception as exc:  # agent work is a separate failure domain
                agent_trigger_status = "failed"
                agent_trigger_reason = type(exc).__name__
        promoted = service.promote(payload.category)
        return {
            "accepted": True,
            "category": payload.category,
            "terminal_status": entry.status,
            "promoted_job_name": promoted,
            "agent_job_name": agent_job_name,
            "agent_trigger_status": agent_trigger_status,
            "agent_trigger_reason": agent_trigger_reason,
        }

    @app.post("/ingest/publish/approve")
    def approve_publish(payload: PublishApprovalPayload) -> dict:
        return service.approve_publish(payload)

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
