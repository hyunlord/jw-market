from __future__ import annotations


def test_cause_requires_view_source_measure(client):
    response = client.get("/api/cause/리바로")
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["error"] == "missing_required_query"
    assert detail["missing"] == ["view", "source", "measure"]


def test_cause_rejects_invalid_view(client):
    response = client.get("/api/cause/리바로?view=strategic_ml&source=UBIST&measure=sales")
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "invalid_view"


def test_cause_rejects_invalid_source(client):
    response = client.get("/api/cause/리바로?view=market_landscape&source=ubist&measure=sales")
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "invalid_source"


def test_cause_rejects_invalid_measure_for_source(client):
    response = client.get("/api/cause/리바로?view=market_landscape&source=UBIST&measure=counting_unit")
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "invalid_measure_for_source"
