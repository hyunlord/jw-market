from __future__ import annotations

from pipeline.scripts.ingest_hook import csd_keyword_activation as activation


class _Cursor:
    def __init__(self, rows: list[dict[str, object]] | None = None) -> None:
        self.rows = rows or []
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, sql: str, params: tuple[object, ...] = ()) -> None:
        self.calls.append((" ".join(sql.split()), params))

    def fetchall(self):
        return self.rows


class _Connection:
    def __init__(self, rows: list[dict[str, object]] | None = None) -> None:
        self.recording_cursor = _Cursor(rows)
        self.commits = 0

    def cursor(self):
        return self.recording_cursor

    def commit(self) -> None:
        self.commits += 1


def test_plan_uses_run_scoped_candidate_and_preserves_live_as_old() -> None:
    plan = activation.plan_for_run("20260808010203000000")

    assert plan.candidate_base == "jw_brand_activity_keyword_20260808010203000000"
    assert plan.raw.candidate.schema == f"{plan.candidate_base}_raw"
    assert plan.stage.candidate.schema == f"{plan.candidate_base}_stage"
    assert plan.raw.live == activation.TableRef(
        "jw_brand_activity_raw_stage", "raw_keyword_events"
    )
    assert plan.stage.live == activation.TableRef(
        "jw_brand_activity_stage", "km_keyword_event_stage"
    )
    assert plan.raw.rollback.table.endswith("__old_20260808010203000000")
    assert plan.stage.rollback.table.endswith("__old_20260808010203000000")
    assert all(len(ref.schema) <= 64 and len(ref.table) <= 64 for ref in plan.table_refs())


def test_plan_uses_configured_live_schemas() -> None:
    plan = activation.plan_for_run(
        "20260808010203000000",
        raw_schema="jw_brand_activity_raw_stage",
        stage_schema="jw_brand_activity_stage",
    )

    assert plan.raw.live.schema == "jw_brand_activity_raw_stage"
    assert plan.stage.live.schema == "jw_brand_activity_stage"


def test_publish_uses_definer_wrapper_instead_of_direct_rename() -> None:
    connection = _Connection()
    plan = activation.plan_for_run("20260808010203000000")

    evidence = activation.CandidateEvidence(
        raw_rows=123,
        stage_rows=45,
        period_count=36,
        min_period="2023-06",
        max_period="2026-05",
    )

    activation.publish_candidate(connection, plan, evidence)

    assert len(connection.recording_cursor.calls) == 1
    sql, params = connection.recording_cursor.calls[0]
    assert sql == "CALL `jw_csd_keyword_control`.`csd_keyword_atomic_publish`(%s,%s,%s,%s,%s,%s,%s,%s)"
    assert params == (
        "20260808010203000000",
        "jw_brand_activity_raw_stage",
        "jw_brand_activity_stage",
        123,
        45,
        36,
        "2023-06",
        "2026-05",
    )


def test_evidence_payload_round_trip_keeps_actual_counts_and_periods() -> None:
    evidence = activation.CandidateEvidence(
        raw_rows=123,
        stage_rows=45,
        period_count=36,
        min_period="2023-06",
        max_period="2026-05",
    )

    assert activation.evidence_from_payload(activation.evidence_payload(evidence)) == evidence
