"""MariaDB adapter for atomic CSD raw and stage publication."""
from __future__ import annotations

import os
import re
from collections import Counter
from dataclasses import dataclass

from pipeline.scripts.ingest_hook.csd_activation_journal import ActivationJournal
from pipeline.scripts.ingest_hook.csd_publication import (
    DISPLAY_MONTHS,
    RETAIN_MONTHS,
    PublicationPlan,
    PublicationRecord,
)
from pipeline.scripts.ingest_hook.csd_publication_provenance import (
    Cursor,
    bounded_id,
    delete_if_present,
    quote_id,
    record,
)


@dataclass(frozen=True, slots=True)
class DatasetContract:
    raw_table: str
    stage_table: str


DATASETS = {
    "iqvia_csd_channel": DatasetContract(
        "raw_csd_channel_dynamics", "csd_channel_dynamics_stage"
    ),
    "iqvia_csd_keyword": DatasetContract(
        "raw_keyword_events", "km_keyword_event_stage"
    ),
}


def require_promotion_approval() -> None:
    if os.environ.get("INGEST_CSD_PROMOTION_APPROVED", "").strip() != "1":
        raise RuntimeError(
            "production CSD publication requires INGEST_CSD_PROMOTION_APPROVED=1"
        )


class MariaDbBackend:
    """Build in isolated schemas and switch one source pair with one RENAME."""

    def __init__(self, plan: PublicationPlan, rows: object) -> None:
        import pymysql

        require_promotion_approval()
        safe_run = re.sub(r"[^A-Za-z0-9_]", "_", plan.run_id)
        self.safe_run = safe_run
        self.contract = DATASETS[plan.category]
        self.live_raw = os.environ.get(
            "INGEST_CSD_RAW_SCHEMA", "jw_brand_activity_raw_stage"
        ).strip()
        self.live_stage = os.environ.get(
            "INGEST_CSD_STAGE_SCHEMA", "jw_brand_activity_stage"
        ).strip()
        prefix = os.environ.get(
            "INGEST_CSD_BUILD_PREFIX", "jw_brand_activity_ingest"
        ).strip()
        for value in (self.live_raw, self.live_stage, prefix):
            if not re.fullmatch(r"[A-Za-z0-9_]+", value):
                raise RuntimeError(f"unsafe CSD schema identifier: {value!r}")
        self.build_raw = bounded_id(prefix, safe_run, "raw")
        self.build_stage = bounded_id(prefix, safe_run, "stage")
        self.rows = rows
        incoming_rows = (
            rows.csd if plan.category == "iqvia_csd_channel" else rows.keyword
        )
        self._source_period_counts = dict(
            Counter(str(row.period_ym) for row in incoming_rows)
        )
        self._expected_raw_counts: dict[str, int] | None = None
        self._expected_stage_counts: dict[str, int] | None = None
        self.conn = pymysql.connect(
            host=os.environ.get("MARIADB_HOST", "127.0.0.1"),
            port=int(os.environ.get("MARIADB_PORT", "3306")),
            user=os.environ.get("MARIADB_USER", ""),
            password=os.environ.get("MARIADB_PASSWORD", ""),
            charset="utf8mb4",
            autocommit=False,
        )
        self._backup_raw = bounded_id(self.contract.raw_table, "old", safe_run)
        self._backup_stage = bounded_id(self.contract.stage_table, "old", safe_run)
        self.journal = ActivationJournal(
            self.conn,
            category=plan.category,
            live_raw=self.live_raw,
            live_stage=self.live_stage,
            raw_table=self.contract.raw_table,
            stage_table=self.contract.stage_table,
            backup_raw=self._backup_raw,
            backup_stage=self._backup_stage,
        )

    def acquire(self, _plan: PublicationPlan) -> None:
        self.journal.acquire()

    def release(self, _plan: PublicationPlan) -> None:
        self.journal.release()

    def recover(self, _plan: PublicationPlan) -> None:
        self.journal.recover()

    def arm_recovery(self, plan: PublicationPlan) -> None:
        self.journal.arm(plan.run_id)

    def prepare(self, _plan: PublicationPlan) -> None:
        cursor = self.conn.cursor()
        try:
            for schema in (self.build_raw, self.build_stage):
                cursor.execute(f"CREATE SCHEMA {quote_id(schema)}")
            for live_schema, build_schema, table in (
                (self.live_raw, self.build_raw, self.contract.raw_table),
                (self.live_stage, self.build_stage, self.contract.stage_table),
            ):
                cursor.execute(
                    f"CREATE TABLE {quote_id(build_schema)}.{quote_id(table)} "
                    f"LIKE {quote_id(live_schema)}.{quote_id(table)}"
                )
                cursor.execute(
                    f"INSERT INTO {quote_id(build_schema)}.{quote_id(table)} "
                    f"SELECT * FROM {quote_id(live_schema)}.{quote_id(table)}"
                )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        finally:
            cursor.close()

    def replace_periods(
        self, plan: PublicationPlan, periods: tuple[str, ...]
    ) -> None:
        from pipeline.scripts.etl.brand_activity.raw_db import DbConfig, load_sources

        scope = "csd" if plan.category == "iqvia_csd_channel" else "keyword"
        load_sources(
            DbConfig(
                host=os.environ.get("MARIADB_HOST", "127.0.0.1"),
                port=int(os.environ.get("MARIADB_PORT", "3306")),
                user=os.environ.get("MARIADB_USER", ""),
                password=os.environ.get("MARIADB_PASSWORD", ""),
                raw_schema=self.build_raw,
                stage_schema=self.build_stage,
            ),
            self.rows,
            None,
            stage_scope=scope,
            replace_periods=periods,
            retention_months=RETAIN_MONTHS,
            display_months=DISPLAY_MONTHS,
        )
        cursor = self.conn.cursor()
        try:
            candidate_raw = self._period_counts(
                cursor, schema=self.build_raw, table=self.contract.raw_table
            )
            for period in periods:
                expected = self._source_period_counts.get(period, 0)
                actual = candidate_raw.get(period, 0)
                if actual != expected:
                    raise RuntimeError(
                        "CSD candidate raw row count mismatch for submitted period "
                        f"{period}: expected={expected} actual={actual}"
                    )
            self._expected_raw_counts = candidate_raw
            self._expected_stage_counts = self._period_counts(
                cursor, schema=self.build_stage, table=self.contract.stage_table
            )
        finally:
            cursor.close()

    def apply_windows(self, _plan: PublicationPlan) -> None:
        return

    def publish(self, plan: PublicationPlan) -> object:
        q = quote_id
        cursor = self.conn.cursor()
        try:
            cursor.execute(
                "RENAME TABLE "
                f"{q(self.live_raw)}.{q(self.contract.raw_table)} TO "
                f"{q(self.live_raw)}.{q(self._backup_raw)}, "
                f"{q(self.build_raw)}.{q(self.contract.raw_table)} TO "
                f"{q(self.live_raw)}.{q(self.contract.raw_table)}, "
                f"{q(self.live_stage)}.{q(self.contract.stage_table)} TO "
                f"{q(self.live_stage)}.{q(self._backup_stage)}, "
                f"{q(self.build_stage)}.{q(self.contract.stage_table)} TO "
                f"{q(self.live_stage)}.{q(self.contract.stage_table)}"
            )
            self.conn.commit()
        finally:
            cursor.close()
        self.journal.mark(run_id=plan.run_id, phase="published")
        return (self._backup_raw, self._backup_stage)

    def record_provenance(
        self, plan: PublicationPlan, publication: PublicationRecord
    ) -> None:
        record(
            self.conn,
            live_stage=self.live_stage,
            plan=plan,
            publication=publication,
        )

    def verify_refresh(self, plan: PublicationPlan) -> None:
        if self._expected_raw_counts is None or self._expected_stage_counts is None:
            raise RuntimeError("CSD publication verification expectations are unavailable")
        cursor = self.conn.cursor()
        try:
            actual_raw = self._period_counts(
                cursor, schema=self.live_raw, table=self.contract.raw_table
            )
            actual_stage = self._period_counts(
                cursor, schema=self.live_stage, table=self.contract.stage_table
            )
            if actual_raw != self._expected_raw_counts:
                raise RuntimeError("CSD raw publication row counts differ from candidate")
            if actual_stage != self._expected_stage_counts:
                raise RuntimeError("CSD stage publication row counts differ from candidate")
            if not actual_stage:
                raise RuntimeError("CSD scoped refresh verification found no serving rows")
            periods = tuple(sorted(set(plan.incoming_periods)))
            missing_raw = tuple(period for period in periods if period not in actual_raw)
            if missing_raw:
                raise RuntimeError(
                    "CSD scoped refresh verification missed submitted periods in "
                    f"{self.contract.raw_table}: {missing_raw}"
                )
            stage_start, stage_end = min(actual_stage), max(actual_stage)
            displayed = tuple(
                period for period in periods if stage_start <= period <= stage_end
            )
            missing_stage = tuple(
                period for period in displayed if period not in actual_stage
            )
            if missing_stage:
                raise RuntimeError(
                    "CSD scoped refresh verification missed submitted periods in "
                    f"{self.contract.stage_table}: {missing_stage}"
                )
        finally:
            cursor.close()

    @staticmethod
    def _period_counts(
        cursor: Cursor,
        *,
        schema: str,
        table: str,
    ) -> dict[str, int]:
        cursor.execute(
            f"SELECT period_ym, COUNT(*) FROM {quote_id(schema)}."
            f"{quote_id(table)} GROUP BY period_ym ORDER BY period_ym"
        )
        return {str(row[0]): int(row[1]) for row in cursor.fetchall()}

    def complete(self, plan: PublicationPlan) -> None:
        self.journal.mark(run_id=plan.run_id, phase="complete")

    def live_count(self) -> int:
        cursor = self.conn.cursor()
        try:
            cursor.execute(
                f"SELECT COUNT(*) FROM {quote_id(self.live_raw)}."
                f"{quote_id(self.contract.raw_table)}"
            )
            return int(cursor.fetchone()[0])
        finally:
            cursor.close()

    def rollback(self, plan: PublicationPlan, _token: object) -> None:
        q = quote_id
        failed_raw = bounded_id(self.contract.raw_table, "failed", self.safe_run)
        failed_stage = bounded_id(self.contract.stage_table, "failed", self.safe_run)
        cursor = self.conn.cursor()
        try:
            cursor.execute(
                "RENAME TABLE "
                f"{q(self.live_raw)}.{q(self.contract.raw_table)} TO "
                f"{q(self.live_raw)}.{q(failed_raw)}, "
                f"{q(self.live_raw)}.{q(self._backup_raw)} TO "
                f"{q(self.live_raw)}.{q(self.contract.raw_table)}, "
                f"{q(self.live_stage)}.{q(self.contract.stage_table)} TO "
                f"{q(self.live_stage)}.{q(failed_stage)}, "
                f"{q(self.live_stage)}.{q(self._backup_stage)} TO "
                f"{q(self.live_stage)}.{q(self.contract.stage_table)}"
            )
            delete_if_present(
                cursor,
                live_stage=self.live_stage,
                category=plan.category,
                run_id=plan.run_id,
            )
            self.conn.commit()
        finally:
            cursor.close()
        self.journal.mark(run_id=plan.run_id, phase="rolled_back")

    def close(self) -> None:
        self.conn.close()
