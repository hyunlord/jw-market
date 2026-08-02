from fastapi.testclient import TestClient

from src.main import app


def test_openapi_documentation_is_disabled_by_default() -> None:
    # Given: the production bridge application
    # When: FastAPI documentation routes are inspected
    # Then: no unauthenticated documentation endpoint is registered
    assert app.docs_url is None
    assert app.openapi_url is None
    assert app.redoc_url is None


def test_health_response_exposes_only_liveness() -> None:
    # Given: an unauthenticated health request
    client = TestClient(app)

    # When: the health endpoint is called
    response = client.get("/health")

    # Then: internal topology and write-mode settings are not disclosed
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
