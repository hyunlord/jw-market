from __future__ import annotations

from fastapi.routing import APIRoute

from src.main import app


PROTECTED_ROUTES = {
    ("/dry-run", "POST"),
    ("/commit", "POST"),
    ("/upload", "POST"),
    ("/upload/status", "GET"),
    ("/documents", "GET"),
    ("/documents/delete", "POST"),
    ("/documents/delete", "DELETE"),
    ("/quota/check", "GET"),
    ("/file-sql/schema", "POST"),
    ("/file-sql/query", "POST"),
    ("/search", "POST"),
}


def test_every_session_scoped_route_requires_portal_actor_header() -> None:
    observed: set[tuple[str, str]] = set()
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods:
            key = (route.path, method)
            if key not in PROTECTED_ROUTES:
                continue
            observed.add(key)
            aliases = {field.alias for field in route.dependant.header_params}
            assert "X-Portal-User-Id" in aliases, key

    assert observed == PROTECTED_ROUTES


def test_health_remains_probe_safe_without_actor_header() -> None:
    health_route = next(
        route
        for route in app.routes
        if isinstance(route, APIRoute) and route.path == "/health"
    )
    assert {field.alias for field in health_route.dependant.header_params} == set()
