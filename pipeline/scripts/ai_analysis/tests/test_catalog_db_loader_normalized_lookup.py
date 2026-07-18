from __future__ import annotations

from bundle_builder.catalog_db_loader import load_brand_from_catalog


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.result = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=()):
        self.conn.calls.append((sql, params))
        normalized_sql = " ".join(sql.split())
        if normalized_sql.startswith("SHOW TABLES LIKE"):
            self.result = {"Tables_in_test": params[0]} if params[0] in self.conn.tables else None
            return
        if "FROM catalog_strategic_brand" in normalized_sql and "WHERE name = %s" in normalized_sql:
            self.result = self.conn.catalog_exact.get(params[0])
            return
        if "FROM catalog_strategic_brand" in normalized_sql and "REPLACE(LOWER(name)" in normalized_sql:
            self.result = self.conn.catalog_compact.get(params[0])
            return
        if "FROM mart_strategic_ml_brand_metric" in normalized_sql and "WHERE brand_name = %s" in normalized_sql:
            self.result = self.conn.mart_exact.get(params[0])
            return
        if "FROM mart_strategic_ml_brand_metric" in normalized_sql and "REPLACE(LOWER(brand_name)" in normalized_sql:
            self.result = self.conn.mart_compact.get(params[0])
            return
        if "FROM mart_strategic_ml_market_metric" in normalized_sql:
            self.result = {"ml_name": "시장", "computed_at": "2026-07-09"}
            return
        raise AssertionError(f"unexpected query: {normalized_sql}")

    def fetchone(self):
        return self.result


class FakeConn:
    def __init__(self):
        self.tables = {"catalog_strategic_brand"}
        self.calls = []
        self.catalog_exact = {}
        self.catalog_compact = {}
        self.mart_exact = {}
        self.mart_compact = {}

    def cursor(self):
        return FakeCursor(self)


def test_load_brand_exact_catalog_still_wins():
    conn = FakeConn()
    conn.catalog_exact["바스티난 엠알"] = {"name": "바스티난 엠알", "brand_id": "catalog-row"}
    conn.catalog_compact["바스티난엠알"] = {"name": "바스티난엠알", "brand_id": "normalized-row"}

    row = load_brand_from_catalog("바스티난 엠알", conn)

    assert row["brand_id"] == "catalog-row"


def test_load_brand_recovers_space_normalized_catalog_name():
    conn = FakeConn()
    conn.catalog_compact["바스티난엠알"] = {"name": "바스티난 엠알", "brand_id": "normalized-row"}

    row = load_brand_from_catalog("바스티난엠알", conn)

    assert row["brand_id"] == "normalized-row"


def test_load_brand_recovers_space_normalized_mart_name():
    conn = FakeConn()
    conn.mart_compact["페노릭스eh"] = {
        "ml_id": "ML001",
        "brand_id": "B001",
        "brand_key": "BK001",
        "brand_name": "페노릭스 EH",
        "is_jw": 0,
        "overlay_data": "{}",
        "computed_at": "2026-07-09",
    }

    row = load_brand_from_catalog("페노릭스EH", conn)

    assert row["name"] == "페노릭스 EH"
    assert row["derived_key"] == "BK001"


def test_load_brand_keeps_value_error_when_normalized_lookup_misses():
    conn = FakeConn()

    try:
        load_brand_from_catalog("없는브랜드", conn)
    except ValueError as exc:
        assert str(exc) == "brand not found in mart/catalog: 없는브랜드"
    else:
        raise AssertionError("expected ValueError")
