from __future__ import annotations

from argparse import Namespace

from pipeline.etl.io.mart.filter_dimension_promote import promote_filter_dimension_rows
from pipeline.etl.io.mart.filter_dimension_promote import promote_filter_dimension_slice
from pipeline.scripts.etl.build_filter_dimension_metric import _serving_guard_schema
from pipeline.scripts.etl.build_filter_dimension_metric import _source_epoch


class _Cursor:
    def __init__(self, expected_rows: int = 1) -> None:
        self.expected_rows = expected_rows
        self.calls: list[tuple[str, object]] = []
        self.rows: list[dict[str, object]] = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if "SELECT COUNT(*) AS n" in sql:
            self.rows = [{"n": self.expected_rows}]
        elif "SELECT id," in sql:
            self.rows = [
                {
                    "id": 7,
                    "source": "ubist",
                    "measure": "sales",
                    "atc4_code": "A10N1",
                    "brand_key": "brand-a",
                    "brand_name": "브랜드A",
                    "product_code": "p1",
                    "dimension_type": "molecule",
                    "dimension_value": "A / B",
                    "dimension_value_norm": "A / B",
                    "dimension_value_hash": "a" * 64,
                    "raw_value_history": '{"202601":1}',
                }
            ] if params and params[-1] == 0 else []
        elif "DELETE FROM" in sql:
            self.rowcount = 0

    def executemany(self, sql, params):
        payloads = tuple(params)
        self.calls.append((sql, payloads))
        self.rowcount = len(payloads)

    def fetchone(self):
        return self.rows[0]

    def fetchall(self):
        return self.rows


class _Connection:
    def __init__(self, expected_rows: int = 1) -> None:
        self.cursor_instance = _Cursor(expected_rows)
        self.commits = 0

    def cursor(self):
        return self.cursor_instance

    def commit(self):
        self.commits += 1


def test_shared_promotion_checks_the_approved_serving_schema() -> None:
    assert (
        _serving_guard_schema(Namespace(promote_to="jw_mart_d2_stage_20260630_r2"))
        == "jw_mart_d2_stage_20260630_r2"
    )
    assert _serving_guard_schema(Namespace(promote_to=None)) == "jw_mart"


def test_promote_filter_dimension_slice_is_bounded_to_ubist_molecule() -> None:
    conn = _Connection()

    result = promote_filter_dimension_slice(
        conn,
        source_db="jw_mart_dim_stage_f046",
        target_db="jw_mart_d2_stage_20260630_r2",
        source="ubist",
        dimension_type="molecule",
        build_marker="2026-07-13 22:30:00",
        batch_size=10,
        allow_shared_serving_target=True,
    )

    sql = "\n".join(call[0] for call in conn.cursor_instance.calls)
    assert "ON DUPLICATE KEY UPDATE" in sql
    assert "source=%s AND dimension_type=%s" in sql
    assert result["expected_rows"] == 1
    assert result["promoted_rows"] == 1
    assert result["stale_rows_deleted"] == 0


def test_promote_filter_dimension_slice_rejects_any_other_slice() -> None:
    conn = _Connection()

    try:
        promote_filter_dimension_slice(
            conn,
            source_db="jw_mart_dim_stage_f046",
            target_db="jw_mart_d2_stage_20260630_r2",
            source="ubist",
            dimension_type="form",
            build_marker="2026-07-13 22:30:00",
            allow_shared_serving_target=True,
        )
    except ValueError as exc:
        assert "ubist/molecule" in str(exc)
    else:
        raise AssertionError("non-molecule sidecar promotion must be rejected")


def test_promote_filter_dimension_slice_refuses_empty_stage_before_delete() -> None:
    conn = _Connection(expected_rows=0)

    try:
        promote_filter_dimension_slice(
            conn,
            source_db="jw_mart_dim_stage_f046",
            target_db="jw_mart_d2_stage_20260630_r2",
            source="ubist",
            dimension_type="molecule",
            build_marker="2026-07-13 22:30:00",
            allow_shared_serving_target=True,
        )
    except RuntimeError as exc:
        assert "empty staged slice" in str(exc)
    else:
        raise AssertionError("empty stage must not replace the serving molecule slice")

    assert all("DELETE FROM" not in sql for sql, _params in conn.cursor_instance.calls)


def test_promote_filter_dimension_rows_writes_only_approved_slice() -> None:
    conn = _Connection()
    rows = [
        {
            "source": "ubist",
            "measure": "sales",
            "atc4_code": "A10N1",
            "brand_key": "brand-a",
            "brand_name": "브랜드A",
            "product_code": "p1",
            "dimension_type": "molecule",
            "dimension_value": "A / B",
            "dimension_value_norm": "A / B",
            "raw_value_history": {"202601": 1},
        }
    ]

    result = promote_filter_dimension_rows(
        conn,
        rows,
        target_db="jw_mart_d2_stage_20260630_r2",
        source="ubist",
        dimension_type="molecule",
        build_marker="2026-07-13 22:30:00",
        batch_size=10,
        allow_shared_serving_target=True,
    )

    sql = "\n".join(call[0] for call in conn.cursor_instance.calls)
    assert "CREATE DATABASE" not in sql
    assert "ON DUPLICATE KEY UPDATE" in sql
    assert "source=%s AND dimension_type=%s" in sql
    assert result["expected_rows"] == 1
    assert result["promoted_rows"] == 1


def test_promote_filter_dimension_rows_refuses_mixed_slice_before_write() -> None:
    conn = _Connection()
    rows = [
        {
            "source": "ubist",
            "measure": "sales",
            "atc4_code": "A10N1",
            "brand_key": "brand-a",
            "brand_name": "브랜드A",
            "product_code": "p1",
            "dimension_type": "form",
            "dimension_value": "tablet",
            "dimension_value_norm": "tablet",
            "raw_value_history": {"202601": 1},
        }
    ]

    try:
        promote_filter_dimension_rows(
            conn,
            rows,
            target_db="jw_mart_d2_stage_20260630_r2",
            source="ubist",
            dimension_type="molecule",
            build_marker="2026-07-13 22:30:00",
            allow_shared_serving_target=True,
        )
    except ValueError as exc:
        assert "mixed or out-of-scope" in str(exc)
    else:
        raise AssertionError("mixed sidecar rows must be rejected")

    assert conn.cursor_instance.calls == []


def test_source_epoch_uses_configured_runtime_cache_store(monkeypatch) -> None:
    from pipeline.scripts.api.dynamic_market import runtime_cache

    class _Store:
        @staticmethod
        def source_epoch() -> str:
            return "epoch-from-runtime-store"

    monkeypatch.setattr(runtime_cache.dynamic_response_cache, "_store", _Store())

    assert _source_epoch() == "epoch-from-runtime-store"
