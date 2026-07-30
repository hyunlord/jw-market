from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from pipeline.scripts.api.actor_assertion import (
    ActorAssertionConfig,
    install_actor_assertion_middleware,
)

ISSUER = "jw-portal-api"
AUDIENCE = "jw-market-backend-api"
ENVIRONMENT = "dev"
KEY_ID = "portal-actor-dev-v1"


@pytest.fixture()
def signing_keys() -> tuple[str, str]:
    return _make_signing_keys()


def _make_signing_keys() -> tuple[str, str]:
    key = ec.generate_private_key(ec.SECP256R1())
    private_key = key.private_bytes(
        Encoding.PEM,
        PrivateFormat.PKCS8,
        NoEncryption(),
    ).decode("ascii")
    public_key = key.public_key().public_bytes(
        Encoding.PEM,
        PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")
    return private_key, public_key


def _client(public_key: str) -> TestClient:
    app = FastAPI()
    install_actor_assertion_middleware(
        app,
        ActorAssertionConfig(
            public_key_pem=public_key,
            allowed_kid=KEY_ID,
            issuer=ISSUER,
            audience=AUDIENCE,
            environment=ENVIRONMENT,
        ),
    )

    @app.get("/whoami")
    def whoami(request: Request) -> dict[str, str | None]:
        return {
            "actor_type": request.state.actor_type,
            "actor_uid": request.state.actor_uid,
            "jti": request.state.jti,
        }

    return TestClient(app)


def _assertion(
    private_key: str,
    *,
    algorithm: str = "ES256",
    kid: str = KEY_ID,
    issuer: str = ISSUER,
    audience: str = AUDIENCE,
    environment: str = ENVIRONMENT,
    subject: str = "genos-user:123",
    jwt_id: str = "jti-123",
    actor_type: str = "user",
    issued_at: datetime | None = None,
    expires_at: datetime | None = None,
    extra_headers: dict[str, object] | None = None,
) -> str:
    now = datetime.now(UTC)
    iat = issued_at or now
    exp = expires_at or iat + timedelta(seconds=60)
    signing_key = "shared-secret-with-at-least-thirty-two-bytes" if algorithm == "HS256" else private_key
    return jwt.encode(
        {
            "iss": issuer,
            "aud": audience,
            "env": environment,
            "sub": subject,
            "jti": jwt_id,
            "actor_type": actor_type,
            "iat": int(iat.timestamp()),
            "exp": int(exp.timestamp()),
        },
        signing_key,
        algorithm=algorithm,
        headers={"kid": kid, **(extra_headers or {})},
    )


def test_actor_state_is_user_when_assertion_is_valid(signing_keys: tuple[str, str]) -> None:
    """Given a valid ES256 assertion, When a request is handled, Then actor state is populated."""
    private_key, public_key = signing_keys

    response = _client(public_key).get(
        "/whoami",
        headers={"X-Actor-Assertion": _assertion(private_key)},
    )

    assert response.status_code == 200
    assert response.json() == {
        "actor_type": "user",
        "actor_uid": "genos-user:123",
        "jti": "jti-123",
    }


def test_actor_state_is_unknown_when_assertion_is_missing(signing_keys: tuple[str, str]) -> None:
    """Given observer mode, When the assertion header is absent, Then the request is allowed."""
    _, public_key = signing_keys

    response = _client(public_key).get("/whoami")

    assert response.status_code == 200
    assert response.json() == {
        "actor_type": "unknown",
        "actor_uid": None,
        "jti": None,
    }


def test_request_is_rejected_when_assertion_signature_is_forged(signing_keys: tuple[str, str]) -> None:
    """Given a signed token from another key, When presented, Then the response is 401."""
    _, public_key = signing_keys
    forged_private_key, _ = _make_signing_keys()

    response = _client(public_key).get(
        "/whoami",
        headers={"X-Actor-Assertion": _assertion(forged_private_key)},
    )

    assert response.status_code == 401


def test_request_is_rejected_when_assertion_is_expired(signing_keys: tuple[str, str]) -> None:
    """Given an assertion older than skew, When presented, Then the response is 401."""
    private_key, public_key = signing_keys
    issued_at = datetime.now(UTC) - timedelta(seconds=80)

    response = _client(public_key).get(
        "/whoami",
        headers={
            "X-Actor-Assertion": _assertion(
                private_key,
                issued_at=issued_at,
                expires_at=issued_at + timedelta(seconds=60),
            )
        },
    )

    assert response.status_code == 401


def test_request_is_rejected_when_key_id_is_not_allowed(signing_keys: tuple[str, str]) -> None:
    """Given a disallowed kid, When the assertion is present, Then the response is 401."""
    private_key, public_key = signing_keys

    response = _client(public_key).get(
        "/whoami",
        headers={"X-Actor-Assertion": _assertion(private_key, kid="other-key")},
    )

    assert response.status_code == 401


def test_request_is_rejected_when_algorithm_is_not_es256(signing_keys: tuple[str, str]) -> None:
    """Given a non-ES256 assertion, When presented, Then the response is 401."""
    private_key, public_key = signing_keys

    response = _client(public_key).get(
        "/whoami",
        headers={"X-Actor-Assertion": _assertion(private_key, algorithm="HS256")},
    )

    assert response.status_code == 401


def test_request_is_rejected_when_critical_header_is_present(
    signing_keys: tuple[str, str],
) -> None:
    """Given an unsupported critical extension, When presented, Then the response is 401."""
    private_key, public_key = signing_keys

    response = _client(public_key).get(
        "/whoami",
        headers={
            "X-Actor-Assertion": _assertion(
                private_key,
                extra_headers={"crit": ["custom"], "custom": True},
            )
        },
    )

    assert response.status_code == 401


def test_ttl_boundary_accepts_sixty_seconds(signing_keys: tuple[str, str]) -> None:
    """Given a 60 second TTL, When presented, Then the response is accepted."""
    private_key, public_key = signing_keys
    issued_at = datetime.now(UTC)

    response = _client(public_key).get(
        "/whoami",
        headers={
            "X-Actor-Assertion": _assertion(
                private_key,
                issued_at=issued_at,
                expires_at=issued_at + timedelta(seconds=60),
            )
        },
    )

    assert response.status_code == 200


def test_ttl_boundary_rejects_sixty_one_seconds(signing_keys: tuple[str, str]) -> None:
    """Given a 61 second TTL, When presented, Then the response is rejected."""
    private_key, public_key = signing_keys
    issued_at = datetime.now(UTC)

    response = _client(public_key).get(
        "/whoami",
        headers={
            "X-Actor-Assertion": _assertion(
                private_key,
                issued_at=issued_at,
                expires_at=issued_at + timedelta(seconds=61),
            )
        },
    )

    assert response.status_code == 401


def test_request_is_rejected_when_actor_type_is_not_user(signing_keys: tuple[str, str]) -> None:
    private_key, public_key = signing_keys

    response = _client(public_key).get(
        "/whoami",
        headers={
            "X-Actor-Assertion": _assertion(
                private_key,
                actor_type="service",
            )
        },
    )

    assert response.status_code == 401


@pytest.mark.parametrize(
    ("overrides"),
    [
        {"environment": "stage"},
        {"subject": "genos-user:0"},
        {"subject": "portal.user@example.test"},
    ],
)
def test_request_is_rejected_when_identity_contract_is_wrong(
    signing_keys: tuple[str, str],
    overrides: dict[str, str],
) -> None:
    private_key, public_key = signing_keys

    response = _client(public_key).get(
        "/whoami",
        headers={
            "X-Actor-Assertion": _assertion(
                private_key,
                **overrides,
            )
        },
    )

    assert response.status_code == 401


def test_request_is_rejected_when_expiration_precedes_issued_at(
    signing_keys: tuple[str, str],
) -> None:
    private_key, public_key = signing_keys
    issued_at = datetime.now(UTC)

    response = _client(public_key).get(
        "/whoami",
        headers={
            "X-Actor-Assertion": _assertion(
                private_key,
                issued_at=issued_at,
                expires_at=issued_at - timedelta(seconds=1),
            )
        },
    )

    assert response.status_code == 401


def test_present_assertion_is_rejected_when_verifier_is_not_configured(
    signing_keys: tuple[str, str],
) -> None:
    private_key, _ = signing_keys
    app = FastAPI()
    install_actor_assertion_middleware(
        app,
        ActorAssertionConfig(
            public_key_pem=None,
            allowed_kid=None,
            issuer=None,
            audience=None,
            environment=None,
        ),
    )

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    response = TestClient(app).get(
        "/health",
        headers={"X-Actor-Assertion": _assertion(private_key)},
    )

    assert response.status_code == 401


def test_raw_assertion_is_not_logged(
    signing_keys: tuple[str, str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Given an invalid assertion, When logging occurs, Then raw token bytes are absent."""
    private_key, public_key = signing_keys
    assertion = _assertion(private_key, kid="wrong-kid")

    with caplog.at_level(logging.WARNING):
        response = _client(public_key).get(
            "/whoami",
            headers={"X-Actor-Assertion": assertion},
        )

    assert response.status_code == 401
    assert assertion not in caplog.text
