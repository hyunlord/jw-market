from __future__ import annotations


EXPECTED_CAUSE_DATA_KEYS = {
    "kpi",
    "market_size_series",
    "hhi_series_5y",
    "hhi_recent",
    "brand_ranking",
    "company_ranking",
    "ei_ms_matrix",
    "growth_contribution_ms_matrix",
    "growth_contribution",
    "analysis_levels",
    "target_customer_competition",
    "level_top5_trend",
    "company_concentration_trend",
}

EXPECTED_ANALYSIS_LEVELS = ["Class", "Molecule", "Brand", "제형/투여경로", "용량", "비/급여", "Ox/Gx"]
SEGMENT_KEYS = {"name", "rank", "recent_share_pct", "series_pct", "value_series"}
FORBIDDEN_SEGMENT_ALIASES = {
    "recent_share_volume_pct",
    "recent_share_unit_pct",
    "recent_share_dosage_unit_pct",
    "recent_share_counting_unit_pct",
    "series_volume_pct",
    "series_unit_pct",
    "series_dosage_unit_pct",
    "series_counting_unit_pct",
}


def test_cause_defaults_to_market_landscape_ubist_sales(client):
    response = client.get("/api/cause/리바로")

    assert response.status_code == 200
    payload = response.json()
    assert payload["view"] == "market_landscape"
    assert payload["source"] == "UBIST"
    assert payload["measure"] == "sales"
    assert payload["unit_label"] == "KRW"
    assert payload["data"] is not None


def test_cause_data_contains_v091_core_fields(client):
    response = client.get("/api/cause/가드메트?view=market_landscape&source=UBIST&measure=sales")

    assert response.status_code == 200
    data = response.json()["data"]
    assert EXPECTED_CAUSE_DATA_KEYS <= set(data)
    assert data["market_size_series"]
    assert data["hhi_series_5y"]
    assert data["hhi_recent"] is not None
    assert data["brand_ranking"]
    assert data["company_ranking"]


def test_cause_analysis_levels_always_expose_v091_level_keys(client):
    response = client.get("/api/cause/가드메트?view=market_landscape&source=UBIST&measure=sales")

    assert response.status_code == 200
    analysis_levels = response.json()["data"]["analysis_levels"]
    assert analysis_levels["levels"] == EXPECTED_ANALYSIS_LEVELS

    for level in EXPECTED_ANALYSIS_LEVELS:
        level_data = analysis_levels["data"][level]
        assert "segments" in level_data
        assert "by_channel" in level_data
        for channel in analysis_levels["channels"]:
            assert channel in level_data["by_channel"]


def test_cause_segment_rows_are_measure_neutral(client):
    response = client.get("/api/cause/가드메트?view=market_landscape&source=IQVIA&measure=dosage_unit")

    assert response.status_code == 200
    analysis_levels = response.json()["data"]["analysis_levels"]
    first_level = analysis_levels["levels"][0]
    first_channel = analysis_levels["channels"][0]
    segment = analysis_levels["data"][first_level]["by_channel"][first_channel][0]

    assert set(segment) == SEGMENT_KEYS
    assert not (FORBIDDEN_SEGMENT_ALIASES & set(segment))
    assert isinstance(segment["series_pct"], list)
    assert isinstance(segment["value_series"], list)
    assert len(segment["series_pct"]) == len(analysis_levels["periods_quarterly"])
    assert len(segment["value_series"]) == len(analysis_levels["periods_quarterly"])


def test_cause_invalid_measure_reports_valid_measures(client):
    response = client.get("/api/cause/리바로?view=market_landscape&source=UBIST&measure=counting_unit")

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["error"] == "invalid_measure_for_source"
    assert detail["valid_measures"] == ["sales", "volume"]
