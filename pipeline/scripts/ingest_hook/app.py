"""Trigger service: webhook receiver + ledger status API (D-1 option (a)).

Deliberately NOT part of jw-market-backend-api — ingest load/failure must never
share a process, pod, or endpoint with serving (STOP ①). Runs from the same
pipeline-orchestrator image (fastapi/uvicorn/PyMySQL already included):

    uvicorn --factory pipeline.scripts.ingest_hook.app:build --port 8080

Endpoints (the site's whole contract surface):
  POST /ingest/webhook   {"manifest_path": "<path under INGEST_INPUT_ROOT>"}
  GET  /ingest/status    ?epoch=&category=&manifest_sha=
  POST /ingest/reconcile  (promote queued submissions; sweep/ops helper)
  GET  /healthz
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from pipeline.scripts.ingest_hook import config, job_launcher, stage_logs
from pipeline.scripts.ingest_hook.category_map import UnknownCategoryError, resolve_category
from pipeline.scripts.ingest_hook.contract import ContractError, load_manifest, parse_manifest_bytes
from pipeline.scripts.ingest_hook.ledger import (
    STATUS_COMPLETE,
    STATUS_FAILED,
    Ledger,
    LedgerConnectionError,
)


class WebhookPayload(BaseModel):
    manifest_path: str


class IngestService:
    def __init__(
        self,
        ledger: Ledger,
        input_root: Path | None,
        transport=None,
        s3=None,
        inspect_transport=None,
        now=None,
    ):
        self.ledger = ledger
        self.input_root = input_root
        self.transport = transport
        self.s3 = s3
        self.inspect_transport = inspect_transport
        self.now = now or (lambda: datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f"))

    # -- promotion: one running Job per category, FIFO within a category ----
    def promote(self, category: str) -> str | None:
        if self.ledger.running_in_category(category) > 0:
            return None
        entry = self.ledger.next_queued(category)
        if entry is None:
            return None
        run_id = self.now()
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
        self.ledger.mark_running(entry.epoch, category, entry.manifest_sha, job_name=name, run_id=run_id)
        return name

    def reconcile_terminal_jobs(self) -> dict:
        """Repair stale running rows from Kubernetes truth, then unblock FIFO.

        The ledger transition and append-only evidence are one DB transaction.
        Job creation is necessarily outside that transaction, so promotion uses
        the existing idempotent path immediately afterward. If the process dies
        between those steps, the queued row remains eligible for the next
        reconcile instead of leaving the category blocked by stale ``running``.
        """
        actions: list[dict] = []
        reconciled = 0
        inspection_failures = 0
        for entry in self.ledger.running_entries():
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

            changed = self.ledger.reconcile_terminal(
                entry.epoch,
                entry.category,
                entry.manifest_sha,
                status=ledger_status,
                reason=reason,
                actor="terminal_job_reconciler",
                source=source,
                evidence=observation.evidence,
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
            promoted = self.promote(entry.category)
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
        launched = self.promote(manifest.category) if decision.action == "queued" else None
        return {
            "epoch": manifest.epoch,
            "category": manifest.category,
            "manifest_sha": manifest.manifest_sha,
            "decision": decision.action,
            "status": decision.status,
            "reason": decision.reason,
            "job_name": launched,
        }


def create_app(service: IngestService) -> FastAPI:
    app = FastAPI(title="jw-ingest-hook", docs_url=None, redoc_url=None)

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
        current_stage = next(
            (event.stage for event in reversed(events) if event.status == "running"), None
        )
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
            "current_stage": current_stage,
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

    @app.post("/ingest/reconcile")
    def reconcile() -> dict:
        terminal = service.reconcile_terminal_jobs()
        launched = {
            category: name
            for category in service.ledger.queued_categories()
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
