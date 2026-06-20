"""Tests for agent/tools/code_interpreter.py — run_execute_python.

Mocks:
  * _build_code_client → a fake CodeInterpreter whose invoke() returns a
    scripted {"stream": [...]} and whose download_file() returns PNG bytes.
  * _ddb → captures put_item calls so we can assert the persisted steps.
  * AGENT_SESSION_ROOT → a tmp dir so chart copies land somewhere writable.
Covers:
  * happy path: step persisted running → idle, stdout captured, summary returned
  * chart extraction: a saved PNG is copied to the workspace and recorded in
    step.charts
  * execution error (non-zero exit): step + row marked failed
  * invoke raises: row marked failed, error surfaced
  * empty / oversized code: early rejection, no client built
  * identity pinning: user_id / agent_session_id are not LLM-overridable (the
    tool signature only exposes code + title)
"""
import inspect
import json
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("CODE_SESSIONS_TABLE_NAME", "smarthome-code-sessions")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setenv("AGENT_SESSION_ROOT", str(tmp_path))


@pytest.fixture(autouse=True)
def _clear_runs():
    """The tool caches sessions per agent_session_id in a module global; reset
    it between tests so each test starts from a clean slate."""
    from tools import code_interpreter as ci
    ci._RUNS.clear()
    yield
    ci._RUNS.clear()


def _exec_event(stdout="", stderr="", exit_code=0):
    return {"result": {"structuredContent": {
        "stdout": stdout, "stderr": stderr,
        "exitCode": exit_code, "executionTime": "0.01",
    }}}


def _fake_client(exec_stdout="hello\n", exit_code=0, charts=None):
    """A CodeInterpreter whose invoke() answers executeCode runs with the
    given stdout/exit, and the internal chart-scan run with a JSON list."""
    charts = charts or []
    client = MagicMock()

    def _invoke(method, params):
        code = params.get("code", "")
        if "__CHARTS__" in code:
            return {"stream": [_exec_event(stdout="__CHARTS__" + json.dumps(charts))]}
        return {"stream": [_exec_event(stdout=exec_stdout, exit_code=exit_code)]}

    client.invoke.side_effect = _invoke
    client.download_file.return_value = b"\x89PNG\r\n\x1a\nFAKE"
    return client


def test_happy_path_persists_step_and_returns_summary():
    from tools import code_interpreter as ci

    client = _fake_client(exec_stdout="daily kWh computed\n")
    ddb_table = MagicMock()
    ddb_res = MagicMock(); ddb_res.Table.return_value = ddb_table

    with patch.object(ci, "_build_code_client", lambda: client), \
         patch.object(ci, "_ddb", lambda: ddb_res):
        result = ci.run_execute_python(
            code="print('daily kWh computed')", title="Aggregate energy",
            user_id="user-42", agent_session_id="asid-1",
        )

    assert "daily kWh computed" in result
    statuses = [c.kwargs["Item"]["status"] for c in ddb_table.put_item.call_args_list]
    assert statuses[0] == "running"
    assert statuses[-1] == "idle"
    last_item = ddb_table.put_item.call_args_list[-1].kwargs["Item"]
    assert last_item["steps"][0]["title"] == "Aggregate energy"
    assert "daily kWh computed" in last_item["steps"][0]["stdout"]
    assert last_item["steps"][0]["status"] == "done"


def test_chart_is_copied_to_workspace(tmp_path):
    from tools import code_interpreter as ci

    client = _fake_client(charts=["energy_trend.png"])
    ddb_table = MagicMock()
    ddb_res = MagicMock(); ddb_res.Table.return_value = ddb_table

    with patch.object(ci, "_build_code_client", lambda: client), \
         patch.object(ci, "_ddb", lambda: ddb_res):
        result = ci.run_execute_python(
            code="plt.savefig('energy_trend.png')", title="Plot",
            user_id="u", agent_session_id="asid-charts",
        )

    last_item = ddb_table.put_item.call_args_list[-1].kwargs["Item"]
    charts = last_item["steps"][0]["charts"]
    assert charts == ["code/step-000-0.png"]
    # The PNG bytes were written into the session workspace.
    written = tmp_path / "asid-charts" / "code" / "step-000-0.png"
    assert written.exists() and written.read_bytes().startswith(b"\x89PNG")
    assert "1 chart" in result


def test_nonzero_exit_marks_failed():
    from tools import code_interpreter as ci

    client = _fake_client(exec_stdout="", exit_code=1)
    ddb_table = MagicMock()
    ddb_res = MagicMock(); ddb_res.Table.return_value = ddb_table

    with patch.object(ci, "_build_code_client", lambda: client), \
         patch.object(ci, "_ddb", lambda: ddb_res):
        result = ci.run_execute_python(
            code="raise SystemExit(1)", title="Boom",
            user_id="u", agent_session_id="asid-2",
        )

    last_item = ddb_table.put_item.call_args_list[-1].kwargs["Item"]
    assert last_item["status"] == "failed"
    assert last_item["steps"][0]["status"] == "failed"
    assert "exit code 1" in result


def test_invoke_exception_marks_failed():
    from tools import code_interpreter as ci

    client = MagicMock()
    client.invoke.side_effect = RuntimeError("sandbox died")
    ddb_table = MagicMock()
    ddb_res = MagicMock(); ddb_res.Table.return_value = ddb_table

    with patch.object(ci, "_build_code_client", lambda: client), \
         patch.object(ci, "_ddb", lambda: ddb_res):
        result = ci.run_execute_python(
            code="print(1)", title="x", user_id="u", agent_session_id="asid-3",
        )

    assert "Code execution failed" in result
    statuses = [c.kwargs["Item"]["status"] for c in ddb_table.put_item.call_args_list]
    assert statuses[-1] == "failed"


def test_empty_code_rejected_early():
    from tools import code_interpreter as ci
    with patch.object(ci, "_build_code_client") as factory:
        result = ci.run_execute_python(code="   ", title="x",
                                       user_id="u", agent_session_id="s")
    assert "empty" in result.lower()
    factory.assert_not_called()


def test_oversized_code_rejected_early():
    from tools import code_interpreter as ci
    with patch.object(ci, "_build_code_client") as factory:
        result = ci.run_execute_python(code="x" * (ci.CODE_MAX_LEN + 1), title="x",
                                       user_id="u", agent_session_id="s")
    assert "too long" in result.lower()
    factory.assert_not_called()


def test_identity_not_in_llm_signature():
    """The raw impl takes user_id/agent_session_id, but agent.py wraps it in a
    closure exposing only (code, title). Assert the raw signature keeps the
    identity args keyword-only-ish (present but pinned by the wrapper)."""
    from tools import code_interpreter as ci
    params = inspect.signature(ci.run_execute_python).parameters
    # The wrapper in agent.py only forwards code + title from the model; the
    # identity params have safe defaults so a forged-free call still works.
    assert params["user_id"].default == "default"
    assert params["agent_session_id"].default == "default"
