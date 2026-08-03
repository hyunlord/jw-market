from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator


_ACTOR_USER_ID: ContextVar[int | None] = ContextVar("actor_user_id", default=None)


@contextmanager
def actor_user_scope(user_id: int | None) -> Iterator[None]:
    token = _ACTOR_USER_ID.set(user_id)
    try:
        yield
    finally:
        _ACTOR_USER_ID.reset(token)


def code_serving_actor_headers() -> dict[str, str]:
    user_id = _ACTOR_USER_ID.get()
    return {"X-Portal-User-Id": str(user_id)} if user_id is not None else {}
