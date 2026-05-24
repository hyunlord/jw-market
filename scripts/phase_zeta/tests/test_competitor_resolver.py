from bundle_builder.competitor_resolver import resolve_market_top5_competitors


def test_resolve_market_top5_riva_ubist(db_conn):
    result = resolve_market_top5_competitors(
        brand_name="리바로",
        ml_id="ml_006",
        cd_id="cd_006",
        source="UBIST",
        db_conn=db_conn,
    )
    assert len(result["top_competitors"]) == 5
    names = [c["brand_name"] for c in result["top_competitors"]]
    assert "리바로" not in names


def test_resolve_dual_brand_per_source(db_conn):
    ubist = resolve_market_top5_competitors("가드메트", "ml_003", "cd_003", "UBIST", db_conn)
    iqvia = resolve_market_top5_competitors("가드메트", "ml_003", "cd_003", "IQVIA", db_conn)
    assert len({c["brand_name"] for c in ubist["top_competitors"]}) == 5
    assert len({c["brand_name"] for c in iqvia["top_competitors"]}) == 5
    assert "가드메트" not in {c["brand_name"] for c in ubist["top_competitors"]}
    assert "가드메트" not in {c["brand_name"] for c in iqvia["top_competitors"]}
