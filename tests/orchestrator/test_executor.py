import io
import json

from pipeline.orchestrator.executor import EventLog, execute_plan
from pipeline.orchestrator.planner import build_plan
from pipeline.orchestrator.stages import STAGE_ORDER
from pipeline.orchestrator.state import StateStore

from fakes import EPOCH, FakeProbe


def _log() -> tuple[EventLog, io.StringIO]:
    stream = io.StringIO()
    return EventLog("t", stream=stream), stream


def _events(stream: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_dry_run_executes_nothing_and_writes_nothing(tmp_path):
    state_path = tmp_path / "state.json"
    state = StateStore(state_path)
    plan = build_plan(mode="full", run_id="t", probe=FakeProbe(), state=state, dry_run=True)
    log, stream = _log()

    calls = []
    exit_code = execute_plan(plan, state, log, dry_run=True, runner=lambda command: calls.append(command) or 0)

    assert exit_code == 0
    assert calls == []
    assert not state_path.exists()
    events = _events(stream)
    assert events[0]["event"] == "plan"
    assert events[-1]["event"] == "dry_run_end"


def test_chain_aborts_on_first_failure_and_checkpoints(tmp_path):
    state_path = tmp_path / "state.json"
    state = StateStore(state_path)
    plan = build_plan(mode="full", run_id="t", probe=FakeProbe(), state=state)
    log, stream = _log()

    def runner(command):
        return 1 if "ops_forecast_builder" in " ".join(command.argv) else 0

    exit_code = execute_plan(plan, state, log, runner=runner)

    assert exit_code == 1
    record = json.loads(state_path.read_text())
    assert record["stages"]["cache"]["status"] == "completed"
    assert record["stages"]["cache"]["epoch"] == EPOCH
    assert record["stages"]["forecast"]["status"] == "failed"
    assert "strength" not in record["stages"]
    events = _events(stream)
    assert any(event["event"] == "chain_abort" and event["stage"] == "forecast" for event in events)


def test_resume_after_failure_skips_completed_stages(tmp_path):
    state = StateStore(tmp_path / "state.json")
    first = build_plan(mode="full", run_id="t", probe=FakeProbe(), state=state)
    log, _ = _log()
    execute_plan(first, state, log, runner=lambda c: 1 if "ops_forecast_builder" in " ".join(c.argv) else 0)

    resumed = build_plan(mode="full", run_id="t2", probe=FakeProbe(), state=state)
    by_key = {stage.key: stage for stage in resumed.stages}
    assert by_key["cache"].action == "skip_fresh"
    assert by_key["forecast"].action == "run"

    log2, stream2 = _log()
    exit_code = execute_plan(resumed, state, log2, runner=lambda c: 0)
    assert exit_code == 0
    events = _events(stream2)
    assert events[-1] == {**events[-1], "event": "run_end", "status": "completed"}
    for key in STAGE_ORDER:
        record = state.get(key)
        assert record is not None and record.status == "completed"


def test_blocked_plan_refuses_execution(tmp_path):
    state = StateStore(tmp_path / "state.json")
    plan = build_plan(mode="full", run_id="t", probe=FakeProbe(), state=state, stages_csv="strength")
    log, _ = _log()

    exit_code = execute_plan(plan, state, log, runner=lambda c: 0)

    assert exit_code == 2
    assert state.get("strength") is None


def test_event_log_lines_are_json_with_run_id(tmp_path):
    state = StateStore(tmp_path / "state.json")
    plan = build_plan(mode="full", run_id="rid42", probe=FakeProbe(), state=state)
    log_file = tmp_path / "log.jsonl"
    stream = io.StringIO()
    log = EventLog("rid42", stream=stream, log_file=log_file)

    execute_plan(plan, state, log, runner=lambda c: 0)

    lines = log_file.read_text().splitlines()
    assert lines
    for line in lines:
        event = json.loads(line)
        assert event["run_id"] == "rid42"
        assert "ts" in event and "event" in event
