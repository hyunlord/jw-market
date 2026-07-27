"""Trigger service: webhook receiver + ledger status API (D-1 option (a)).

Deliberately NOT part of jw-market-backend-api — ingest load/failure must never
share a process, pod, or endpoint with serving (STOP ①). Runs from the same
pipeline-orchestrator image (fastapi/uvicorn/PyMySQL already included):

    uvicorn --factory pipeline.scripts.ingest_hook.app:build --port 8080

Endpoints (the site's whole contract surface):
  POST /ingest/webhook   {"manifest_path": "<path under INGEST_INPUT_ROOT>"}
  GET  /ingest/status    ?epoch=&category=&manifest_sha=
  POST /ingest/force-stop (exact active run only)
  POST /ingest/reconcile  (promote queued submissions; sweep/ops helper)
  GET  /healthz
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from time import sleep as _sleep

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


class ForceStopPayload(BaseModel):
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
            # status="failed" is reported-not-raised by record_stage, so the
            # mark_failed below always runs and the original submission error is
            # re-raised to the caller. Nothing is reported as success here.
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
        # The Job now exists in Kubernetes, so record that truth in the ledger
        # BEFORE the observation row. record_stage is fail-closed for forward
        # progress; if it were first, a stage-table outage would skip mark_running
        # and leave the row queued, and the next promote would submit a second Job
        # for the same submission under a new run_id.
        self.ledger.mark_running(entry.epoch, category, entry.manifest_sha, job_name=name, run_id=run_id)
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
        if entry.status != "running":
            raise HTTPException(
                status_code=409,
                detail=f"force stop requires a running ledger row, got {entry.status}",
            )
        if entry.run_id != run_id:
            raise HTTPException(status_code=409, detail="run_id does not match the active ledger row")
        expected_name = job_launcher.job_name(category, manifest_sha, run_id)
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


class _Counterpart:
    """Result of reading the ledger this pod is not bound to.

    ``available`` is the whole point: entry=None means "no such row there" only
    when available is True.  When available is False the row is simply unknown,
    and the two must never be reported through the same field.
    """

    __slots__ = ("ledger", "entry", "available", "error")

    def __init__(self, ledger=None, entry=None, available: bool = False, error: str | None = None):
        self.ledger = ledger
        self.entry = entry
        self.available = available
        self.error = error


def _ledger_source_name() -> str:
    """Name of the ledger this pod is configured to bind."""
    try:
        return config.configured_ledger_source()
    except Exception:  # noqa: BLE001 — an unreadable env must not 500 the status read
        return "unknown"


def _read_counterpart(epoch: str, category: str, manifest_sha: str):
    """Read the other ledger, never raising into the response path.

    A failure here must not turn a working status read into a 500 — the bound
    ledger's answer is still worth returning — but it must not be silently
    downgraded to "absent" either, so the reason travels in the result.
    """
    try:
        source = config.counterpart_ledger_source()
    except Exception as exc:  # noqa: BLE001
        return None, _Counterpart(error=f"{type(exc).__name__}: {exc}")
    if source is None:
        return None, _Counterpart(available=True)
    ledger = None
    try:
        ledger = config.open_ledger_by_source(source)
        entry = ledger.status(epoch, category, manifest_sha)
        return source, _Counterpart(ledger=ledger, entry=entry, available=True)
    except Exception as exc:  # noqa: BLE001 — reported, not swallowed
        return source, _Counterpart(ledger=ledger, error=f"{type(exc).__name__}: {exc}")


def _ledgers_agree(answered_entry, other_entry, other_available: bool) -> bool | None:
    """True/False only when both sides are actually known; None otherwise.

    None means "cannot say" — either the other ledger was unreadable or there is
    no other ledger to compare against.  It never means "they match".
    """
    if not other_available:
        return None
    if answered_entry is None or other_entry is None:
        # One side has no such row. That is a real disagreement only if the
        # other side does have it, which is the case here (answered_entry came
        # from a row that exists).
        return False if (answered_entry is None) != (other_entry is None) else None
    return answered_entry.status == other_entry.status


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
        # Two ledgers can hold the same identity with different outcomes: the
        # rehearsal sqlite this pod may be bound to, and the operational mart
        # ledger the sweep and the ingest Jobs actually write.  The binding is a
        # side effect of the load-output env, so "which ledger this pod opened"
        # is not the same question as "which ledger recorded the run".  Read both
        # and say which one the reported values came from, rather than answering
        # from whichever one happens to be bound.
        primary_source = _ledger_source_name()
        primary_entry = service.ledger.status(epoch, category, manifest_sha)
        counterpart_source, counterpart = _read_counterpart(epoch, category, manifest_sha)

        # The operational record wins when it has the row; otherwise fall back to
        # whichever ledger does have it.  Stages and signals are then read from
        # the SAME ledger as the entry so the row and its evidence never come
        # from different sides of the split.
        if primary_source == "d2" and primary_entry is not None:
            entry, ledger_source, evidence_ledger = primary_entry, primary_source, service.ledger
        elif counterpart_source == "d2" and counterpart.entry is not None:
            entry, ledger_source, evidence_ledger = counterpart.entry, "d2", counterpart.ledger
        elif primary_entry is not None:
            entry, ledger_source, evidence_ledger = primary_entry, primary_source, service.ledger
        elif counterpart.entry is not None:
            entry, ledger_source = counterpart.entry, counterpart_source or "unknown"
            evidence_ledger = counterpart.ledger
        else:
            # Absent from every ledger we could read.  If one was configured but
            # unreadable we do not actually know it is absent there, so the
            # detail says so instead of asserting a clean "not found".
            detail = f"unknown submission identity in {primary_source}"
            if counterpart.error is not None:
                detail += f"; {counterpart_source} unreadable: {counterpart.error}"
            elif counterpart_source is not None:
                detail += f" or {counterpart_source}"
            raise HTTPException(status_code=404, detail=detail)

        # The reported "other" ledger is the one that did NOT supply the values
        # above.  Reporting the counterpart unconditionally would name the same
        # ledger twice whenever the counterpart is the one that answered, which
        # would hide exactly the disagreement this field exists to surface.
        if ledger_source == primary_source:
            other_source = counterpart_source
            other_entry = counterpart.entry
            other_available = counterpart.available
            other_error = counterpart.error
        else:
            other_source = primary_source
            other_entry = primary_entry
            other_available = True  # the bound ledger was read to get here
            other_error = None
        # Additive only (backward compatible): existing keys are unchanged; the
        # stage list / current_stage / log_ref / observation_* are new.
        #
        # An observation read failure still degrades to an empty list instead of a
        # 500 — the ledger row itself is readable and callers depend on getting it.
        # But an empty list must not be indistinguishable from a failed read, so the
        # failure is reported in observation_available / observation_error rather
        # than silently discarded. `available=true` + `[]` means there genuinely are
        # no rows; `available=false` means the rows are unknown.
        observation_errors: list[str] = []
        try:
            events = evidence_ledger.stage_events(epoch, category, manifest_sha)
        except Exception as exc:  # noqa: BLE001 — reported below, not swallowed
            events = []
            observation_errors.append(f"stage_events: {type(exc).__name__}: {exc}")
        try:
            signals = evidence_ledger.signal_events(epoch, category, manifest_sha)
        except Exception as exc:  # noqa: BLE001 — reported below, not swallowed
            signals = []
            observation_errors.append(f"signal_events: {type(exc).__name__}: {exc}")
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
            # Which ledger the values above came from, and what the other one
            # says.  counterpart_status is None for two different reasons, so
            # counterpart_available separates "no such row there" from "could
            # not read there".
            "ledger_source": ledger_source,
            "ledger_bound": primary_source,
            "counterpart_source": other_source,
            "counterpart_available": other_available,
            "counterpart_error": other_error,
            "counterpart_status": other_entry.status if other_entry is not None else None,
            "counterpart_finished_at": (
                other_entry.finished_at if other_entry is not None else None
            ),
            "ledgers_agree": _ledgers_agree(entry, other_entry, other_available),
            "observation_available": not observation_errors,
            "observation_error": "; ".join(observation_errors) or None,
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
