from __future__ import annotations

import os
import re
from typing import Final


GENOS_BASE_URL_ENV: Final[str] = "GENOS_BASE_URL"
GENOS_SERVING_ID_ENV: Final[str] = "GENOS_SERVING_ID"
GENOS_FINAL_SERVING_ID_ENV: Final[str] = "GENOS_FINAL_SERVING_ID"
GENOS_PLANNER_SERVING_ID_ENV: Final[str] = "GENOS_PLANNER_SERVING_ID"
GENOS_BEARER_TOKEN_ENV: Final[str] = "GENOS_BEARER_TOKEN"
GENOS_TOKEN_ENV: Final[str] = "GENOS_TOKEN"
GENOS_FINAL_BEARER_TOKEN_ENV: Final[str] = "GENOS_FINAL_BEARER_TOKEN"
GENOS_PLANNER_BEARER_TOKEN_ENV: Final[str] = "GENOS_PLANNER_BEARER_TOKEN"
DEFAULT_GENOS_SERVING_ID: Final[str] = "517"
DEFAULT_GENOS_FINAL_SERVING_ID: Final[str] = "514"
DEFAULT_GENOS_PLANNER_SERVING_ID: Final[str] = "508"
DEFAULT_GENOS_BASE_URL: Final[str] = (
    "https://jwai-dev.jwhealthcare.com/api/gateway/rep/"
    f"serving/{DEFAULT_GENOS_SERVING_ID}"
)

_SERVING_PATH_RE: Final[re.Pattern[str]] = re.compile(r"/serving/\d+(?=/|$)")


def resolve_genos_base_url(
    configured_url: str | None = None,
    *,
    serving_id_env: str = GENOS_SERVING_ID_ENV,
    default_serving_id: str = DEFAULT_GENOS_SERVING_ID,
) -> str:
    base_url = configured_url or os.environ.get(GENOS_BASE_URL_ENV) or DEFAULT_GENOS_BASE_URL
    serving_id = os.environ.get(serving_id_env) or os.environ.get(GENOS_SERVING_ID_ENV) or default_serving_id
    if _SERVING_PATH_RE.search(base_url):
        return _SERVING_PATH_RE.sub(f"/serving/{serving_id}", base_url.rstrip("/"))
    return base_url.rstrip("/")


def resolve_final_genos_base_url(configured_url: str | None = None) -> str:
    """Resolve the final-answer model endpoint, defaulting to Flash."""

    return resolve_genos_base_url(
        configured_url,
        serving_id_env=GENOS_FINAL_SERVING_ID_ENV,
        default_serving_id=DEFAULT_GENOS_FINAL_SERVING_ID,
    )


def resolve_planner_genos_base_url(configured_url: str | None = None) -> str:
    """Resolve the tool-planning model endpoint, defaulting to Flash."""

    return resolve_genos_base_url(
        configured_url,
        serving_id_env=GENOS_PLANNER_SERVING_ID_ENV,
        default_serving_id=DEFAULT_GENOS_PLANNER_SERVING_ID,
    )


def resolve_genos_token(*, scoped_env: str | None = None) -> str | None:
    """Resolve a scoped GenOS token while preserving the existing common fallback."""

    if scoped_env:
        scoped = os.environ.get(scoped_env)
        if scoped:
            return scoped
    return os.environ.get(GENOS_BEARER_TOKEN_ENV) or os.environ.get(GENOS_TOKEN_ENV)


def resolve_final_genos_token() -> str | None:
    """Resolve the token for final answer generation."""

    return resolve_genos_token(scoped_env=GENOS_FINAL_BEARER_TOKEN_ENV)


def resolve_planner_genos_token() -> str | None:
    """Resolve the token for tool-planning requests."""

    return resolve_genos_token(scoped_env=GENOS_PLANNER_BEARER_TOKEN_ENV)
