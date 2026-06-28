from auto_review.checks import check_competitor_in_view_market


def test_competitor_in_view_market_passes_for_same_cd_market(db_conn):
    bundle = {
        "market_views": [
            {
                "view_id": "CD.UBIST.volume",
                "view": "competitive_dynamics",
                "source": "UBIST",
                "measure": "volume",
                "market_meta": {"market_id_internal": "cd_001"},
                "competitors_top5": [{"brand_name": "라베"}],
            }
        ]
    }

    result = check_competitor_in_view_market(bundle, db_conn)

    assert result["passed"]
    assert result["failures"] == []


def test_competitor_in_view_market_detects_cross_market_competitor(db_conn):
    bundle = {
        "market_views": [
            {
                "view_id": "CD.UBIST.volume",
                "view": "competitive_dynamics",
                "source": "UBIST",
                "measure": "volume",
                "market_meta": {"market_id_internal": "cd_001"},
                "competitors_top5": [{"brand_name": "헴리브라"}],
            }
        ]
    }

    result = check_competitor_in_view_market(bundle, db_conn)

    assert not result["passed"]
    assert result["failures"][0]["brand_name"] == "헴리브라"
    assert result["failures"][0]["market_id"] == "cd_001"
