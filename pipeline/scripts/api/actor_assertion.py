from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Final

import jwt
from fastapi import Depends, FastAPI, Request
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from pipeline.scripts.api.config import APIConfig

LOGGER = logging.getLogger(__name__)
ASSERTION_HEADER: Final = "X-Actor-Assertion"
ALGORITHM: Final = "ES256"
MAX_TTL_SECONDS: Final = 60
CLOCK_SKEW_SECONDS: Final = 10
USER_SUBJECT_RE: Final = re.compile(r"^genos-user:([1-9][0-9]*)$")


@dataclass(frozen=True)
class ActorContext:
    actor_type: str
    actor_uid: str | None
    jti: str | None


@dataclass(frozen=True)
class ActorAssertionConfig:
    public_key_pem: str | None
    allowed_kid: str | None
    issuer: str | None
    audience: str | None
    environment: str | None

    @classmethod
    def from_api_config(cls, api_config: APIConfig) -> ActorAssertionConfig:
        public_key_pem = _read_public_key(api_config.actor_assertion_public_key_file)
        return cls(
            public_key_pem=public_key_pem,
            allowed_kid=api_config.actor_assertion_allowed_kid,
            issuer=api_config.actor_assertion_issuer,
            audience=api_config.actor_assertion_audience,
            environment=api_config.actor_assertion_environment,
        )

    def is_ready(self) -> bool:
        return self.environment in {"dev", "stage"} and all(
            (
                self.public_key_pem,
                self.allowed_kid,
                self.issuer,
                self.audience,
                self.environment,
            )
        )


class ActorAssertionRejected(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ActorAssertionVerifier:
    def __init__(self, config: ActorAssertionConfig) -> None:
        self._config = config

    def verify(self, assertion: str) -> ActorContext:
        if not self._config.is_ready():
            raise ActorAssertionRejected("config_unavailable")
        config = self._config
        try:
            header = jwt.get_unverified_header(assertion)
        except jwt.InvalidTokenError as exc:
            raise ActorAssertionRejected("invalid_header") from exc

        if header.get("alg") != ALGORITHM:
            raise ActorAssertionRejected("wrong_alg")
        if header.get("kid") != config.allowed_kid:
            raise ActorAssertionRejected("wrong_kid")
        if "crit" in header:
            raise ActorAssertionRejected("unsupported_crit")

        try:
            payload = jwt.decode(
                assertion,
                config.public_key_pem,
                algorithms=[ALGORITHM],
                issuer=config.issuer,
                audience=config.audience,
                leeway=CLOCK_SKEW_SECONDS,
                options={
                    "require": [
                        "iss",
                        "aud",
                        "env",
                        "sub",
                        "jti",
                        "iat",
                        "exp",
                        "actor_type",
                    ]
                },
            )
        except jwt.InvalidTokenError as exc:
            raise ActorAssertionRejected("invalid_claims") from exc

        if payload.get("env") != config.environment:
            raise ActorAssertionRejected("wrong_env")

        iat = _integer_claim(payload.get("iat"), "iat")
        exp = _integer_claim(payload.get("exp"), "exp")
        if exp <= iat or exp - iat > MAX_TTL_SECONDS:
            raise ActorAssertionRejected("ttl_exceeded")

        if payload.get("actor_type") != "user":
            raise ActorAssertionRejected("wrong_actor_type")

        sub = payload.get("sub")
        if not isinstance(sub, str):
            raise ActorAssertionRejected("wrong_sub")
        if USER_SUBJECT_RE.fullmatch(sub) is None:
            raise ActorAssertionRejected("wrong_sub")

        jti = payload.get("jti")
        if not isinstance(jti, str) or not jti:
            raise ActorAssertionRejected("wrong_jti")

        return ActorContext(actor_type="user", actor_uid=sub, jti=jti)


class ActorAssertionMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, verifier: ActorAssertionVerifier) -> None:
        super().__init__(app)
        self._verifier = verifier

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        assertion = request.headers.get(ASSERTION_HEADER)
        if assertion is None:
            _set_actor_context(
                request,
                ActorContext(actor_type="unknown", actor_uid=None, jti=None),
            )
            return await call_next(request)

        try:
            actor = self._verifier.verify(assertion)
        except ActorAssertionRejected as exc:
            LOGGER.warning("actor assertion rejected: reason=%s", exc.reason)
            return JSONResponse({"detail": "invalid actor assertion"}, status_code=401)

        _set_actor_context(request, actor)
        return await call_next(request)


def install_actor_assertion_middleware(app: FastAPI, config: ActorAssertionConfig) -> None:
    app.add_middleware(ActorAssertionMiddleware, verifier=ActorAssertionVerifier(config))


def actor_from_request(request: Request) -> ActorContext:
    actor_type = getattr(request.state, "actor_type", "unknown")
    actor_uid = getattr(request.state, "actor_uid", None)
    jti = getattr(request.state, "jti", None)
    if not isinstance(actor_type, str):
        return ActorContext(actor_type="unknown", actor_uid=None, jti=None)
    if actor_uid is not None and not isinstance(actor_uid, str):
        return ActorContext(actor_type="unknown", actor_uid=None, jti=None)
    if jti is not None and not isinstance(jti, str):
        return ActorContext(actor_type="unknown", actor_uid=None, jti=None)
    return ActorContext(actor_type=actor_type, actor_uid=actor_uid, jti=jti)


ActorDep = Annotated[ActorContext, Depends(actor_from_request)]


def _set_actor_context(request: Request, actor: ActorContext) -> None:
    request.state.actor_type = actor.actor_type
    request.state.actor_uid = actor.actor_uid
    request.state.jti = actor.jti


def _integer_claim(value, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ActorAssertionRejected(f"wrong_{name}")
    return value


def _read_public_key(path_value: str | None) -> str | None:
    if path_value is None or path_value == "":
        return None
    return Path(path_value).read_text(encoding="utf-8")
