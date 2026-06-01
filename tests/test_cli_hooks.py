"""Tests for the hidden rekol _hook time-context / record-stop subcommands."""

import json
from pathlib import Path

from click.testing import CliRunner

from rekol.cli_hooks import hook_group


def _run(args, stdin):
    return CliRunner().invoke(hook_group, args, input=stdin)


def test_time_context_emits_env_time_block(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    res = _run(["time-context"], json.dumps({"session_id": "abc-123"}))
    assert res.exit_code == 0
    assert "<env-time>" in res.output and "local_time" in res.output


def test_record_stop_updates_state_only(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    _run(["time-context"], json.dumps({"session_id": "s1"}))
    res = _run(["record-stop"], json.dumps({"session_id": "s1"}))
    assert res.exit_code == 0 and res.output.strip() == ""
    state = json.loads((tmp_path / ".claude" / "session-env" / "time-context-s1.json").read_text())
    assert state["last_assistant_epoch"] is not None


def test_soft_fail_on_bad_payload(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    res = _run(["time-context"], "not json")
    assert res.exit_code == 0  # never blocks the prompt


def test_soft_fail_on_path_traversal_session_id(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    res = _run(["time-context"], json.dumps({"session_id": "../../evil"}))
    assert res.exit_code == 0
    assert not (tmp_path / "evil.json").exists()


def test_hook_snippets_call_rekol_hook():
    repo = Path(__file__).resolve().parents[1]
    ups = json.loads((repo / "hooks" / "userpromptsubmit-snippet.json").read_text())
    assert "rekol _hook time-context" in ups["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
    stop = json.loads((repo / "hooks" / "stop-snippet.json").read_text())
    assert "rekol _hook record-stop" in stop["hooks"]["Stop"][0]["hooks"][0]["command"]
