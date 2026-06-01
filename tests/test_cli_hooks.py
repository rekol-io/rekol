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


def test_soft_fail_on_non_dict_state_file(tmp_path, monkeypatch):
    # A state file that is valid JSON but not an object must not crash the hook.
    monkeypatch.setenv("HOME", str(tmp_path))
    d = tmp_path / ".claude" / "session-env"
    d.mkdir(parents=True)
    (d / "time-context-s2.json").write_text("[1, 2, 3]")
    res = _run(["time-context"], json.dumps({"session_id": "s2"}))
    assert res.exit_code == 0
    res2 = _run(["record-stop"], json.dumps({"session_id": "s2"}))
    assert res2.exit_code == 0


def test_sessionend_snippet_includes_review_nudge():
    repo = Path(__file__).resolve().parents[1]
    snip = json.loads((repo / "hooks" / "sessionend-snippet.json").read_text())
    cmds = [h["command"] for h in snip["hooks"]["SessionEnd"][0]["hooks"]]
    assert any("rekol review --nudge" in c for c in cmds)


# --- session-start-nudge ---------------------------------------------------

NUDGE_MARKER = ".index/.session-start-nudge-shown"


def _configure_rekol(monkeypatch, tmp_path: Path, transcripts: int) -> Path:
    """Point REKOL_HOME at a near-empty store and seed N fake transcripts.

    Returns the REKOL home path. The store has only the always-on index file
    (REKOL.md), so it counts as near-empty.
    """
    home = tmp_path / "mem"
    home.mkdir()
    (home / "REKOL.md").write_text("# index\n", encoding="utf-8")
    projects = tmp_path / "projects"
    projects.mkdir()
    for i in range(transcripts):
        proj = projects / f"proj{i}"
        proj.mkdir()
        (proj / "session.jsonl").write_text("{}\n", encoding="utf-8")
    (home / "rekol.config.yaml").write_text(
        f"embedding_model: test-hashing\nclaude_projects_dir: {projects}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("REKOL_HOME", str(home))
    monkeypatch.delenv("MEMORY_HOME", raising=False)
    return home


def test_session_start_nudge_emits_and_writes_marker(tmp_path, monkeypatch):
    home = _configure_rekol(monkeypatch, tmp_path, transcripts=3)
    res = _run(["session-start-nudge"], json.dumps({"session_id": "abc-123"}))
    assert res.exit_code == 0
    # Offers BOTH bringing in past sessions and importing notes.
    assert "session-index" in res.output
    assert "rekol import" in res.output
    assert "3" in res.output  # surfaces the transcript count
    assert (home / NUDGE_MARKER).exists()


def test_session_start_nudge_second_run_is_noop(tmp_path, monkeypatch):
    home = _configure_rekol(monkeypatch, tmp_path, transcripts=3)
    first = _run(["session-start-nudge"], json.dumps({"session_id": "s1"}))
    assert first.output.strip() != ""
    assert (home / NUDGE_MARKER).exists()
    second = _run(["session-start-nudge"], json.dumps({"session_id": "s1"}))
    assert second.exit_code == 0
    assert second.output.strip() == ""  # marker present → silent


def test_session_start_nudge_noop_when_store_non_empty(tmp_path, monkeypatch):
    home = _configure_rekol(monkeypatch, tmp_path, transcripts=3)
    # Add real user content so the store is no longer near-empty.
    (home / "topics").mkdir()
    for i in range(10):
        (home / "topics" / f"t{i}.md").write_text("content", encoding="utf-8")
    res = _run(["session-start-nudge"], json.dumps({"session_id": "s1"}))
    assert res.exit_code == 0
    assert res.output.strip() == ""
    assert not (home / NUDGE_MARKER).exists()  # nothing emitted → no marker


def test_session_start_nudge_noop_when_no_transcripts(tmp_path, monkeypatch):
    home = _configure_rekol(monkeypatch, tmp_path, transcripts=0)
    res = _run(["session-start-nudge"], json.dumps({"session_id": "s1"}))
    assert res.exit_code == 0
    assert res.output.strip() == ""
    assert not (home / NUDGE_MARKER).exists()


def test_session_start_nudge_soft_noop_when_not_configured(tmp_path, monkeypatch):
    monkeypatch.delenv("REKOL_HOME", raising=False)
    monkeypatch.delenv("MEMORY_HOME", raising=False)
    res = _run(["session-start-nudge"], json.dumps({"session_id": "s1"}))
    assert res.exit_code == 0
    assert res.output.strip() == ""


def test_session_start_nudge_soft_fails_on_forced_exception(tmp_path, monkeypatch):
    _configure_rekol(monkeypatch, tmp_path, transcripts=3)

    def _boom(*_args, **_kwargs):
        raise RuntimeError("forced")

    # Force an error deep in the nudge path; the hook must still exit 0 silently.
    monkeypatch.setattr("rekol.cli_hooks.count_claude_transcripts", _boom)
    res = _run(["session-start-nudge"], json.dumps({"session_id": "s1"}))
    assert res.exit_code == 0
    assert res.output.strip() == ""


def test_sessionstart_snippet_includes_nudge_command():
    repo = Path(__file__).resolve().parents[1]
    snip = json.loads((repo / "hooks" / "sessionstart-snippet.json").read_text())
    cmds = [h["command"] for h in snip["hooks"]["SessionStart"][0]["hooks"]]
    assert any("rekol _hook session-start-nudge" in c for c in cmds)
