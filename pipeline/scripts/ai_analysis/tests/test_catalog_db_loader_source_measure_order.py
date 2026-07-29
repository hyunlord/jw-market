from __future__ import annotations

from bundle_builder.catalog_db_loader import load_brand_from_catalog


FULL_ORDER = (
    "ORDER BY ml_id ASC, brand_id ASC, source ASC, measure ASC, "
    "computed_at DESC"
)


class DuplicateMetricCursor:
    def __init__(self, connection):
        self.connection = connection
        self.result = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def execute(self, sql, params=()):
        normalized = " ".join(sql.split())
        self.connection.queries.append(normalized)
        if normalized.startswith("SHOW TABLES LIKE"):
            self.result = None
            return
        if "FROM mart_strategic_ml_market_metric" in normalized:
            self.result = {
                "ml_id": "ml_001",
                "ml_name": "테스트 시장",
                "computed_at": "2026-07-29 00:00:00",
            }
            return
        if "FROM mart_strategic_ml_brand_metric" not in normalized:
            raise AssertionError(f"unexpected query: {normalized}")

        if "WHERE brand_name = %s" in normalized:
            rows = self.connection.exact_rows
        elif "REPLACE(LOWER(brand_name)" in normalized:
            rows = self.connection.normalized_rows
        else:
            raise AssertionError(f"unexpected brand query: {normalized}")

        selected = list(rows)
        if FULL_ORDER in normalized:
            selected.sort(
                key=lambda row: (
                    row["ml_id"],
                    row["brand_id"],
                    row["source"],
                    row["measure"],
                )
            )
        else:
            self.connection.unresolved_tie_calls += 1
            if self.connection.unresolved_tie_calls % 2:
                selected.reverse()
        self.result = selected[0] if selected else None

    def fetchone(self):
        return self.result


class DuplicateMetricConnection:
    def __init__(self):
        self.queries = []
        self.unresolved_tie_calls = 0
        self.exact_rows = self._rows("동일브랜드")
        self.normalized_rows = []

    @staticmethod
    def _rows(brand_name):
        common = {
            "ml_id": "ml_001",
            "brand_id": "brand_001",
            "brand_name": brand_name,
            "is_jw": 0,
            "overlay_data": "{}",
            "computed_at": "2026-07-29 00:00:00",
        }
        return [
            {
                **common,
                "brand_key": "selected-by-full-key",
                "source": "iqvia_nsa",
                "measure": "sales",
            },
            {
                **common,
                "brand_key": "other-source-measure",
                "source": "ubist",
                "measure": "volume",
            },
        ]

    def cursor(self):
        return DuplicateMetricCursor(self)


def test_exact_mart_fallback_orders_by_the_complete_unique_key():
    connection = DuplicateMetricConnection()

    results = [
        load_brand_from_catalog("동일브랜드", connection)["derived_key"]
        for _ in range(6)
    ]

    assert results == ["selected-by-full-key"] * 6
    assert any(FULL_ORDER in query for query in connection.queries)


def test_normalized_mart_fallback_orders_by_the_complete_unique_key():
    connection = DuplicateMetricConnection()
    connection.exact_rows = []
    connection.normalized_rows = connection._rows("동일 브랜드")

    results = [
        load_brand_from_catalog("동일브랜드", connection)["derived_key"]
        for _ in range(6)
    ]

    assert results == ["selected-by-full-key"] * 6
    assert any(FULL_ORDER in query for query in connection.queries)
