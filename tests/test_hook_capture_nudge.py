"""Tests for #122 part 2: context-watch recorder + one-time capture nudge."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from rekol.cli_hooks import capture_nudge, context_watch

SESSION = "sess-abc-123"


@pytest.fixture()
def sandbox_home(tmp_path: Path, monkeypatch) -> Path:
    """Point Path.home() at a sandbox so session-env state never touches ~."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return tmp_path


def _statusline_json(pct: int) -> str:
    return json.dumps({"session_id": SESSION, "context_window": {"used_percentage": pct}})


def _prompt_payload() -> str:
    return json.dumps({"session_id": SESSION, "prompt": "hello"})


def _pct_file(home: Path) -> Path:
    return home / ".claude" / "session-env" / f"context-pct-{SESSION}"


def test_context_watch_records_percentage_and_prints_nothing(sandbox_home: Path) -> None:
    result = CliRunner().invoke(context_watch, [], input=_statusline_json(42))
    assert result.exit_code == 0
    assert result.output == ""  # statusline stdout is the rendered line — must stay empty
    assert _pct_file(sandbox_home).read_text() == "42"


def test_context_watch_soft_fails_on_garbage(sandbox_home: Path) -> None:
    result = CliRunner().invoke(context_watch, [], input="not json {{{")
    assert result.exit_code == 0
    assert result.output == ""


def test_nudge_silent_when_recorder_not_wired(sandbox_home: Path) -> None:
    result = CliRunner().invoke(capture_nudge, [], input=_prompt_payload())
    assert result.exit_code == 0
    assert result.output == ""


def test_nudge_silent_below_threshold(sandbox_home: Path) -> None:
    CliRunner().invoke(context_watch, [], input=_statusline_json(45))
    result = CliRunner().invoke(capture_nudge, [], input=_prompt_payload())
    assert result.output == ""


def test_nudge_fires_once_at_threshold(sandbox_home: Path) -> None:
    CliRunner().invoke(context_watch, [], input=_statusline_json(63))
    runner = CliRunner()
    first = runner.invoke(capture_nudge, [], input=_prompt_payload())
    assert first.exit_code == 0
    assert "63%" in first.output and "capture" in first.output.lower()
    assert "rekol task" in first.output  # points at the working-set flush too
    # Second prompt: already nudged this session → silent.
    second = runner.invoke(capture_nudge, [], input=_prompt_payload())
    assert second.output == ""


def test_nudge_sessions_are_independent(sandbox_home: Path) -> None:
    CliRunner().invoke(context_watch, [], input=_statusline_json(80))
    CliRunner().invoke(capture_nudge, [], input=_prompt_payload())  # consumes sess-abc-123
    other = json.dumps({"session_id": "other-sess", "prompt": "hi"})
    # Different session, no recorded pct → silent, not crashed.
    result = CliRunner().invoke(capture_nudge, [], input=other)
    assert result.exit_code == 0
    assert result.output == ""


def test_nudge_rejects_unsafe_session_id(sandbox_home: Path) -> None:
    evil = json.dumps({"session_id": "../../etc/passwd", "prompt": "x"})
    result = CliRunner().invoke(capture_nudge, [], input=evil)
    assert result.exit_code == 0
    assert result.output == ""
