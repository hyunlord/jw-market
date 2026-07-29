from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bundle_builder import kpi_provider
from pipeline.scripts.api.dynamic_market.types import BrandRef


class _Cursor:
    def __init__(self, rows):
        self._rows = rows
        self._offset = 0
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=()):
        self.executed.append((sql, tuple(params or ())))
        return len(self._rows)

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchmany(self, batch_size):
        rows = self._rows[self._offset : self._offset + batch_size]
        self._offset += len(rows)
        return rows


class _Conn:
    def __init__(self, rows):
        self.cursor_obj = _Cursor(rows)
        self.cursor_args = []

    def cursor(self, *args):
        self.cursor_args.append(args)
        return self.cursor_obj


def test_connection_bound_dynamic_market_db_restores_api_fetchers():
    original_fetch_all = kpi_provider.api_db.fetch_all
    original_fetch_one = kpi_provider.api_db.fetch_one
    conn = _Conn([{"value": 1}])

    with kpi_provider.connection_bound_dynamic_market_db(conn):
        assert kpi_provider.api_db.fetch_all("SELECT 1", ["x"]) == [{"value": 1}]
        assert kpi_provider.api_db.fetch_one("SELECT 2", ["y"]) == {"value": 1}

    assert kpi_provider.api_db.fetch_all is original_fetch_all
    assert kpi_provider.api_db.fetch_one is original_fetch_one
    assert conn.cursor_obj.executed == [
        ("SELECT 1", ("x",)),
        ("SELECT 2", ("y",)),
    ]


def test_connection_bound_dynamic_market_db_binds_and_restores_iter_rows():
    original_iter_rows = kpi_provider.api_db.iter_rows
    conn = _Conn([{"value": 1}])

    with kpi_provider.connection_bound_dynamic_market_db(conn):
        assert list(
            kpi_provider.api_db.iter_rows("SELECT 3", ["z"], batch_size=1)
        ) == [{"value": 1}]

    assert kpi_provider.api_db.iter_rows is original_iter_rows
    assert conn.cursor_obj.executed == [("SELECT 3", ("z",))]
    assert conn.cursor_args == [(kpi_provider.api_db.pymysql.cursors.SSDictCursor,)]


def test_metric_aggregator_iter_rows_reuses_bound_connection_without_db_password(
    monkeypatch,
):
    conn = _Conn([])
    monkeypatch.delenv("DB_PASSWORD", raising=False)

    def forbidden_connect():
        raise AssertionError("dynamic-market must not open a hidden connection")

    monkeypatch.setattr(kpi_provider.api_db, "connect", forbidden_connect)
    aggregator = kpi_provider.MetricAggregator(mart_db="jw_mart")

    with kpi_provider.connection_bound_dynamic_market_db(conn):
        rows = tuple(
            aggregator._iter_metric_rows(
                brands=(BrandRef("target", "타겟", "C10A1"),),
                source="ubist",
                measure="sales",
                channel_axis=None,
            )
        )

    assert rows == ()
    assert len(conn.cursor_obj.executed) == 1


def test_strategic_ml_provider_wraps_existing_cache_free_calculator(monkeypatch):
    conn = _Conn([{"brand_name": "리바로"}])
    sentinel_rows = object()

    def fake_fetch(brand_name, ml_id, source, measure, db_conn):
        assert (brand_name, ml_id, source, measure, db_conn) == ("리바로", "ml_006", "UBIST", "sales", conn)
        return sentinel_rows

    def fake_calculate(rows):
        assert rows is sentinel_rows
        return {"market_size_recent": 100.0}

    monkeypatch.setattr(kpi_provider, "fetch_ml_metric_rows", fake_fetch)
    monkeypatch.setattr(kpi_provider, "calculate_ml_kpi_extras", fake_calculate)

    provider = kpi_provider.StrategicMlKpiProvider(db_conn=conn, ml_id="ml_006")

    assert provider.get_kpi("sb_006_livalo") == {
        "view_kind": "market_landscape",
        "market_scope": "strategic_ml",
        "market_id": "ml_006",
        "source": "UBIST",
        "measure": "sales",
        "market_size_recent": 100.0,
    }


@dataclass(frozen=True)
class _Metric:
    brand_key: str
    brand_name: str
    atc4_code: str
    total_value: float
    market_share_pct: float
    rank: int
    latest_period: str
    latest_value: float
    monthly_series: tuple


@dataclass(frozen=True)
class _Metrics:
    source: str = "ubist"
    measure: str = "sales"
    unit_label: str = "원"
    market_size: float = 300.0
    hhi: float = 5000.0
    cagr: float = 7.5
    monthly_series: tuple = ({"period": "2026-04", "market_size": 300.0},)
    brands: tuple = ()
    all_brands: tuple = ()


class _Definition:
    source = "ubist"
    measure = "sales"
    brands = ()

    def __init__(self):
        self.view = "general"
        self.filter_echo = {"view": "general", "atc4": ["C10A1"], "molecule": []}
        self.normalized_molecules = ()


def test_general_view_provider_reuses_dynamic_contract_and_targets_brand(monkeypatch):
    conn = _Conn(
        [
            {
                "brand_key": "target",
                "brand_name": "타겟",
                "atc4_code": "C10A1",
            }
        ]
    )
    target = _Metric("target", "타겟", "C10A1", 90.0, 30.0, 2, "2026-04", 90.0, ())
    leader = _Metric("leader", "리더", "C10A1", 210.0, 70.0, 1, "2026-04", 210.0, ())
    metrics = _Metrics(brands=(leader, target), all_brands=(leader, target))

    class FakeResolver:
        def __init__(self, mart_db, bridge_db):
            assert (mart_db, bridge_db) == ("jw_mart", "jw_mart")

        def resolve(self, *, atc4, molecule, source, measure):
            assert (atc4, molecule, source, measure) == (["C10A1"], [], "ubist", "sales")
            return _Definition()

    class FakeAggregator:
        def __init__(self, mart_db):
            assert mart_db == "jw_mart"

        def aggregate(self, *, brands, source, measure, period_range, top_n):
            assert (source, measure, top_n) == ("ubist", "sales", 100)
            return metrics

    class FakeComposer:
        def compose(self, *, definition, metrics):
            return {
                "data": {
                    "kpi": {"market_size_recent": 300.0, "hhi_recent": 5000.0},
                    "market_size_series": [{"period": "2026-04", "market_size": 300.0}],
                    "brand_ranking": {"brands": []},
                    "company_ranking": {"companies": []},
                    "hhi_series_5y": [{"year": 2026, "hhi": 5000.0}],
                    "ei_ms_matrix": {"data": []},
                },
                "market_meta": {"direct_competition_count": 2},
                "markets": [{"market_id": "dynamic_general_x", "is_primary": True}],
            }

    monkeypatch.setattr(kpi_provider, "GeneralViewResolver", FakeResolver)
    monkeypatch.setattr(kpi_provider, "MetricAggregator", FakeAggregator)
    monkeypatch.setattr(kpi_provider, "ResponseComposer", FakeComposer)

    provider = kpi_provider.GeneralViewKpiProvider(db_conn=conn, mart_db="jw_mart", bridge_db="jw_mart")
    result = provider.get_kpi("target")

    assert result["view_kind"] == "general"
    assert result["market_scope"] == "atc4"
    assert result["atc4_codes"] == ["C10A1"]
    assert result["target_brand"] == "타겟"
    assert result["target_rank"] == 2
    assert result["brand_value_recent"] == 90.0
    assert result["brand_share_pct"] == 30.0
    assert result["market_size_recent"] == 300.0
    assert result["dynamic_payload"]["markets"] == [{"market_id": "dynamic_general_x", "is_primary": True}]


def test_build_kpi_provider_switches_by_view_kind():
    conn = _Conn([])

    strategic = kpi_provider.build_kpi_provider(
        "strategic_ml",
        db_conn=conn,
        ml_id="ml_006",
        source="UBIST",
        measure="sales",
    )
    general = kpi_provider.build_kpi_provider(
        "general",
        db_conn=conn,
        mart_db="jw_mart",
        bridge_db="jw_mart",
        source="ubist",
        measure="sales",
    )

    assert isinstance(strategic, kpi_provider.StrategicMlKpiProvider)
    assert isinstance(general, kpi_provider.GeneralViewKpiProvider)


def test_general_provider_maps_public_iqvia_source_to_mart_source():
    conn = _Conn([{"brand_key": "target", "brand_name": "타겟", "atc4_code": "C10A1"}])
    provider = kpi_provider.GeneralViewKpiProvider(
        db_conn=conn,
        mart_db="jw_mart",
        bridge_db="jw_mart",
        source="iqvia",
    )

    rows = provider._brand_rows_for_key("target")

    assert rows[0]["brand_key"] == "target"
    assert conn.cursor_obj.executed[-1][1] == ("target", "iqvia_nsa", "sales")
