from __future__ import annotations

from bundle_builder.catalog_db_loader import MKT_TEAM_FALLBACK


MI_MASTER_20260518_MKT_TEAM_BY_ML_ID = {
    "ml_001": "MKT 1팀",
    "ml_002": "MKT 1팀",
    "ml_003": "MKT 1팀",
    "ml_004": "MKT 1팀",
    "ml_005": "MKT 1팀",
    "ml_006": "MKT 1팀",
    "ml_007": "MKT 1팀",
    "ml_008": "MKT 1팀",
    "ml_009": "MKT 1팀",
    "ml_010": "MKT 1팀",
    "ml_011": "MKT 1팀",
    "ml_012": "MKT 2팀",
    "ml_013": "MKT 2팀",
    "ml_014": "MKT 3팀",
    "ml_015": "MKT 2팀",
    "ml_016": "MKT 3팀",
}


def test_mkt_team_fallback_matches_mi_master_20260518():
    assert MKT_TEAM_FALLBACK == MI_MASTER_20260518_MKT_TEAM_BY_ML_ID
