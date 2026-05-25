from bundle_builder.competitor_resolver import resolve_market_top5_competitors, resolve_view_top5_competitors


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


def test_resolve_view_top5_uses_cd_market_and_measure(db_conn):
    result = resolve_view_top5_competitors(
        brand_name="라베칸",
        ml_id="ml_001",
        cd_id="cd_001",
        view="competitive_dynamics",
        source="UBIST",
        measure="volume",
        db_conn=db_conn,
    )

    names = [c["brand_name"] for c in result["top_competitors"]]
    assert len(names) == 5
    assert "라베칸" not in names
    assert result["market_id_for_ranking"] == "cd_001"
    assert result["ranking_basis"] == "volume"

    with db_conn.cursor() as cur:
        cur.execute(
            """
            SELECT brand_name, metric_history
            FROM mart_strategic_cd_brand_metric
            WHERE cd_market_id = %s
              AND source = %s
              AND measure = %s
              AND brand_name <> %s
            """,
            ("cd_001", "ubist", "volume", "라베칸"),
        )
        rows = cur.fetchall()
    expected = []
    from bundle_builder.competitor_resolver import _json_load, _latest_metric_point

    for row in rows:
        period, point = _latest_metric_point(_json_load(row["metric_history"]))
        raw_value = point.get("raw_value")
        if raw_value is not None:
            expected.append((row["brand_name"], float(raw_value)))
    expected.sort(key=lambda item: (-item[1], item[0]))
    assert names == [item[0] for item in expected[:5]]
