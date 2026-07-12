import pytest

from pipeline.scripts.agent3.config import DEFAULT_WORKFLOW_REV, resolve_workflow_rev


def test_resolve_workflow_rev_prefers_explicit_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT3_WORKFLOW_REV", "6001")

    assert resolve_workflow_rev(6002) == 6002


def test_resolve_workflow_rev_uses_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT3_WORKFLOW_REV", "6001")

    assert resolve_workflow_rev() == 6001


def test_resolve_workflow_rev_rejects_invalid_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT3_WORKFLOW_REV", "not-an-int")

    with pytest.raises(ValueError, match="AGENT3_WORKFLOW_REV"):
        resolve_workflow_rev()


def test_resolve_workflow_rev_falls_back_to_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT3_WORKFLOW_REV", raising=False)

    assert resolve_workflow_rev() == DEFAULT_WORKFLOW_REV
