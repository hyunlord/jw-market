import pytest

from pipeline.scripts.agent3.config import (
    WorkflowRevNotPinnedError,
    resolve_workflow_rev,
)
from pipeline.scripts.agent3.run_source import (
    ExecutionContractError,
    _validate_execution_contract,
)


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


def test_resolve_workflow_rev_fails_closed_without_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENT3_WORKFLOW_REV", raising=False)

    with pytest.raises(WorkflowRevNotPinnedError, match="AGENT3_WORKFLOW_REV"):
        resolve_workflow_rev()


def test_resolve_workflow_rev_fails_closed_on_empty_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT3_WORKFLOW_REV", "")

    with pytest.raises(WorkflowRevNotPinnedError, match="AGENT3_WORKFLOW_REV"):
        resolve_workflow_rev()


def test_execution_contract_aborts_on_rev_mismatch() -> None:
    with pytest.raises(ExecutionContractError, match="revision mismatch"):
        _validate_execution_contract(
            workflow_rev=5365,
            expected_workflow_rev=5692,
            cli_mode="dry-run",
            environment_mode=None,
        )
