from __future__ import annotations

import pytest

from pipeline.scripts.etl import cache_build_common


BUILD_ID_ENV_NAMES = ("GIT_COMMIT", "APP_COMMIT_SHA", "APP_VERSION")


def _clear_build_id_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in BUILD_ID_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)


def test_current_build_sha_uses_app_version_when_git_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_build_id_environment(monkeypatch)
    monkeypatch.setenv("APP_VERSION", "a" * 40)
    monkeypatch.setattr(
        cache_build_common.subprocess,
        "check_output",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError("git")),
    )

    assert cache_build_common.current_build_sha() == "a" * 40


def test_current_build_sha_fails_closed_without_any_build_identifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_build_id_environment(monkeypatch)
    monkeypatch.setattr(
        cache_build_common.subprocess,
        "check_output",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError("git")),
    )

    with pytest.raises(
        RuntimeError,
        match="no build identifier available: set APP_VERSION or run with git available",
    ):
        cache_build_common.current_build_sha()


def test_current_build_sha_keeps_git_commit_as_highest_priority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_build_id_environment(monkeypatch)
    monkeypatch.setenv("GIT_COMMIT", "b" * 40)
    monkeypatch.setenv("APP_COMMIT_SHA", "c" * 40)
    monkeypatch.setenv("APP_VERSION", "d" * 40)

    def unexpected_git_call(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("git fallback must not run when GIT_COMMIT is configured")

    monkeypatch.setattr(cache_build_common.subprocess, "check_output", unexpected_git_call)

    assert cache_build_common.current_build_sha() == "b" * 40
