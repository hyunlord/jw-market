import json
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "pipeline" / "scripts" / "etl"))

from pipeline.scripts.etl import build_cache_cause


def test_rows_for_channel_adds_th_and_gh_without_hospital() -> None:
    periods = ["2026-01", "2026-02"]
    rows = [
        {
            "brand_name": "테스트브랜드",
            "channel_data": json.dumps(
                {
                    "상급종합병원": {"2026-01": {"raw_value": 10.0}, "2026-02": {"raw_value": 20.0}},
                    "종합병원": {"2026-01": {"raw_value": 30.0}, "2026-02": {"raw_value": 40.0}},
                    "병원": {"2026-01": {"raw_value": 500.0}, "2026-02": {"raw_value": 600.0}},
                },
                ensure_ascii=False,
            ),
        }
    ]

    tgh_rows = build_cache_cause._rows_for_channel(rows, "UBIST", "(상급종병 + 종병)", periods)
    th_rows = build_cache_cause._rows_for_channel(rows, "UBIST", "상급종병", periods)
    gh_rows = build_cache_cause._rows_for_channel(rows, "UBIST", "종병", periods)
    hospital_rows = build_cache_cause._rows_for_channel(rows, "UBIST", "병원", periods)

    assert tgh_rows[0]["metric_history"] == {"2026-01": 40.0, "2026-02": 60.0}
    assert th_rows[0]["metric_history"] == {"2026-01": 10.0, "2026-02": 20.0}
    assert gh_rows[0]["metric_history"] == {"2026-01": 30.0, "2026-02": 40.0}
    assert hospital_rows[0]["metric_history"] == {"2026-01": 500.0, "2026-02": 600.0}
