from __future__ import annotations

import duckdb
import pandas as pd

from pipeline.etl.io.enrich.ubist_bridge import write_ubist_ml
from pipeline.etl.io.mart import strategic_dimensions


def test_ubist_producer_drops_only_aggregate_specialty_before_mapping(
    tmp_path,
    monkeypatch,
) -> None:
    raw_path = tmp_path / "raw.parquet"
    pd.DataFrame(
        [
            {
                "제품": "테스트정",
                "종별": "의원",
                "진료과": specialty,
                "period_yyyymm": "2026-05",
                "rx_amt": amount,
                "rx_cnt": amount,
                "rx_qty": amount,
                "source_file": "fixture.xlsx",
                "source_sheet": "Sheet1",
                "source_row_no": row_no,
                "약품코드": "DRUG1",
            }
            for row_no, (specialty, amount) in enumerate(
                [
                    ("내과(IM)", 100.0),
                    ("내분비(Endocrinology IM)", 10.0),
                    ("분리되지 않은 내과", 40.0),
                    ("가정의학과(FM)", 20.0),
                    ("일반의(GP)", 30.0),
                ],
                start=1,
            )
        ]
    ).to_parquet(raw_path, index=False)

    products = pd.DataFrame(
        [
            {
                "product_id": "product-1",
                "ml_id": "ml_test",
                "ubist_product_key": "테스트정",
                "ubist_product_title": "테스트정",
                "brand_key": "brand-1",
                "strength_bracket_code": None,
                "molecule": "M1",
            }
        ]
    )
    customer_dict = {
        "ubist_channel": {"의원": "CL"},
        "ubist_specialty": {
            "가정의학과": "IGF",
            "내과": "IGF",
            "일반의": "IGF",
            "내분비": "Endo",
        },
    }
    enriched_root = tmp_path / "enriched"
    enriched_path = enriched_root / "ml_id=ml_test" / "data.parquet"

    rows, products_written = write_ubist_ml(
        products,
        customer_dict,
        enriched_path,
        ubist_glob=str(raw_path),
        ingested_at="2026-07-15T00:00:00+09:00",
    )

    assert rows == 4
    assert products_written == 1
    with duckdb.connect() as connection:
        enriched = connection.execute(
            "SELECT specialty, SUM(raw_rx_amt) AS amount "
            "FROM read_parquet(?) GROUP BY specialty ORDER BY specialty",
            [str(enriched_path)],
        ).fetchall()
        preserved_rows = connection.execute(
            "SELECT raw_rx_amt, source_row_id FROM read_parquet(?) ORDER BY raw_rx_amt",
            [str(enriched_path)],
        ).fetchall()
    assert enriched == [("Endo", 10.0), ("IGF", 90.0)]
    assert [amount for amount, _ in preserved_rows] == [10.0, 20.0, 30.0, 40.0]
    assert all("::1::" not in source_row_id for _, source_row_id in preserved_rows)

    monkeypatch.setattr(strategic_dimensions, "ENRICHED_DIR", enriched_root)
    context = strategic_dimensions.load_ubist_dimension_context("ml_test", products)
    specialty_history = context["code_specialty_history"]["sales"]["DRUG1"]["molecule"]["M1"]

    assert specialty_history == {
        "의원 IGF": {"2026-05": {"raw_value": 90.0}},
        "의원 내분비": {"2026-05": {"raw_value": 10.0}},
    }
    corrected_total = sum(periods["2026-05"]["raw_value"] for periods in specialty_history.values())
    expected_total_without_parent = 10.0 + 40.0 + 20.0 + 30.0
    assert corrected_total / expected_total_without_parent == 1.0
