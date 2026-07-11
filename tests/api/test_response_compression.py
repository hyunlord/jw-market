from __future__ import annotations

from fastapi.testclient import TestClient
from starlette.middleware.gzip import GZipMiddleware

from pipeline.scripts.api.main import app


def test_api_uses_level_one_gzip_for_large_responses() -> None:
    middleware = next(
        (item for item in app.user_middleware if item.cls is GZipMiddleware),
        None,
    )

    assert middleware is not None
    assert middleware.kwargs == {"minimum_size": 1024, "compresslevel": 1}

    response = TestClient(app).get(
        "/openapi.json",
        headers={"Accept-Encoding": "gzip"},
    )

    assert response.status_code == 200
    assert response.headers["content-encoding"] == "gzip"
    assert response.json()["info"]["title"] == "JW Market Analysis API"
