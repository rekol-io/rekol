"""Tests for #143 Phase A: freeze journal, reset parsing, tick, enable/disable."""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import replace
from pathlib import Path

from click.testing import CliRunner

from rekol.cli_resume import main as resume_cli
from rekol.resume import (
    enabled_marker_path,
    freeze_journal_path,
    is_enabled,
    ledger_path,
    parse_reset_time,
    record_stop_failure,
    tick,
)
from rekol.tasks import Task, create_task, update_task

NOW = dt.datetime(2026, 7, 30, 18, 0, 0)


def _enable(index_dir: Path) -> None:
    """Mark the feature opted-in, as `rekol resume enable` does."""
    index_dir.mkdir(parents=True, exist_ok=True)
    enabled_marker_path(index_dir).write_text("2026-07-30T00:00:00\n")


def _freeze(
    index_dir: Path,
    session_id: str,
    *,
    ts: str,
    error_type: str = "rate_limit",
    message: str = "You've hit your session limit · resets 3:45pm",
) -> None:
    index_dir.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": ts,
        "payload": {"session_id": session_id, "error_type": error_type, "message": message},
    }
    with freeze_journal_path(index_dir).open("a") as handle:
        handle.write(json.dumps(entry) + "\n")


def _claim(home: Path, task_id: str, session_id: str) -> None:
    create_task(home, Task(id=task_id, title=task_id))
    update_task(home, task_id, lambda t: replace(t, status="in_progress", session_id=session_id))


# ------------------------------ journal --------------------------------------


def test_record_stop_failure_appends_verbatim_payload(tmp_path: Path) -> None:
    record_stop_failure(tmp_path, {"session_id": "s1", "error_type": "rate_limit", "extra": 42})
    lines = freeze_journal_path(tmp_path).read_text().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["payload"]["extra"] == 42  # instrumentation keeps EVERYTHING
    assert "ts" in entry


# ---------------------------- reset parsing ----------------------------------


def test_parse_reset_pm_same_day() -> None:
    base = dt.datetime(2026, 7, 30, 12, 0)
    assert parse_reset_time("resets 3:45pm", base) == dt.datetime(2026, 7, 30, 15, 45)


def test_parse_reset_rolls_to_next_day_when_past() -> None:
    base = dt.datetime(2026, 7, 30, 16, 0)
    assert parse_reset_time("resets 3:45pm", base) == dt.datetime(2026, 7, 31, 15, 45)


def test_parse_reset_weekday_form() -> None:
    base = dt.datetime(2026, 7, 30, 16, 0)  # a Thursday
    parsed = parse_reset_time("You've hit your weekly limit · resets Mon 12:00am", base)
    assert parsed == dt.datetime(2026, 8, 3, 0, 0)  # next Monday, midnight


def test_parse_reset_absent_returns_none() -> None:
    assert parse_reset_time("some other failure", dt.datetime(2026, 7, 30)) is None


# -------------------------------- tick ---------------------------------------


def test_tick_resumes_claimed_session_after_reset(tmp_path: Path) -> None:
    index, home = tmp_path / "idx", tmp_path / "home"
    _enable(index)
    _claim(home, "big-refactor", "sess-1")
    _freeze(index, "sess-1", ts="2026-07-30T12:10:00")  # resets 3:45pm < NOW 18:00
    launched: list[str] = []
    actions = tick(index, home, now=NOW, launcher=lambda sid, log: launched.append(sid) or True)
    assert [a.session_id for a in actions] == ["sess-1"]
    assert actions[0].task_id == "big-refactor"
    assert launched == ["sess-1"]
    # Ledger written → idempotent: a second tick does nothing.
    assert tick(index, home, now=NOW, launcher=lambda sid, log: True) == []


def test_tick_skips_unclaimed_session(tmp_path: Path) -> None:
    index, home = tmp_path / "idx", tmp_path / "home"
    _enable(index)
    _freeze(index, "sess-idle", ts="2026-07-30T12:10:00")
    assert tick(index, home, now=NOW, launcher=lambda sid, log: True) == []


def test_tick_waits_for_reset_time(tmp_path: Path) -> None:
    index, home = tmp_path / "idx", tmp_path / "home"
    _enable(index)
    _claim(home, "t", "sess-1")
    _freeze(index, "sess-1", ts="2026-07-30T12:10:00")
    early = dt.datetime(2026, 7, 30, 14, 0)  # before the 3:45pm reset
    assert tick(index, home, now=early, launcher=lambda sid, log: True) == []


def test_tick_fallback_delay_when_no_reset_in_message(tmp_path: Path) -> None:
    index, home = tmp_path / "idx", tmp_path / "home"
    _enable(index)
    _claim(home, "t", "sess-1")
    _freeze(index, "sess-1", ts="2026-07-30T17:30:00", message="opaque failure")
    # 30 min after freeze: fallback (60m) not yet elapsed.
    assert tick(index, home, now=NOW, launcher=lambda sid, log: True) == []
    # 61 min after: eligible.
    later = dt.datetime(2026, 7, 30, 18, 31)
    assert len(tick(index, home, now=later, launcher=lambda sid, log: True)) == 1


def test_tick_ignores_non_limit_error_types(tmp_path: Path) -> None:
    index, home = tmp_path / "idx", tmp_path / "home"
    _enable(index)
    _claim(home, "t", "sess-1")
    _freeze(index, "sess-1", ts="2026-07-30T12:10:00", error_type="server_error")
    assert tick(index, home, now=NOW, launcher=lambda sid, log: True) == []


def test_tick_ignores_stale_freezes(tmp_path: Path) -> None:
    index, home = tmp_path / "idx", tmp_path / "home"
    _enable(index)
    _claim(home, "t", "sess-1")
    _freeze(index, "sess-1", ts="2026-07-20T12:10:00")  # 10 days old
    assert tick(index, home, now=NOW, launcher=lambda sid, log: True) == []


def test_tick_caps_at_one_resume(tmp_path: Path) -> None:
    index, home = tmp_path / "idx", tmp_path / "home"
    _enable(index)
    _claim(home, "t1", "sess-1")
    _claim(home, "t2", "sess-2")
    _freeze(index, "sess-1", ts="2026-07-30T12:00:00")
    _freeze(index, "sess-2", ts="2026-07-30T12:05:00")
    launched: list[str] = []
    actions = tick(index, home, now=NOW, launcher=lambda sid, log: launched.append(sid) or True)
    assert len(actions) == 1 and len(launched) == 1
    # Next tick picks up the other one.
    actions2 = tick(index, home, now=NOW, launcher=lambda sid, log: launched.append(sid) or True)
    assert len(actions2) == 1
    assert set(launched) == {"sess-1", "sess-2"}


def test_tick_dry_run_writes_nothing(tmp_path: Path) -> None:
    index, home = tmp_path / "idx", tmp_path / "home"
    _enable(index)
    _claim(home, "t", "sess-1")
    _freeze(index, "sess-1", ts="2026-07-30T12:10:00")
    actions = tick(index, home, now=NOW, dry_run=True, launcher=lambda sid, log: True)
    assert len(actions) == 1 and actions[0].launched is False
    assert not ledger_path(index).exists()
    # Real tick still fires afterwards (dry-run left no ledger mark).
    assert len(tick(index, home, now=NOW, launcher=lambda sid, log: True)) == 1


# --------------------------- enable / disable --------------------------------


def _rekol_home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    (home / "rekol.config.yaml").write_text("embedding_model: test-hashing\n")
    monkeypatch.setenv("REKOL_HOME", str(home))
    return home


def test_enable_then_disable_settings_roundtrip(tmp_path: Path, monkeypatch) -> None:
    _rekol_home(tmp_path, monkeypatch)
    settings = tmp_path / "settings.json"
    settings.write_text('{"hooks": {"SessionStart": [{"matcher": "", "hooks": []}]}}')
    monkeypatch.setenv("CLAUDE_SETTINGS_PATH", str(settings))
    runner = CliRunner()

    # Enable twice: hook registered exactly once (idempotent).
    for _ in range(2):
        result = runner.invoke(resume_cli, ["enable", "--no-launchd"])
        assert result.exit_code == 0, result.output
    data = json.loads(settings.read_text())
    commands = [h["command"] for b in data["hooks"]["StopFailure"] for h in b["hooks"]]
    assert len([c for c in commands if "stop-failure-record" in c]) == 1
    assert data["hooks"]["SessionStart"]  # untouched

    # Disable removes it and leaves other hooks alone.
    result = runner.invoke(resume_cli, ["disable"])
    assert result.exit_code == 0, result.output
    data = json.loads(settings.read_text())
    assert "StopFailure" not in data["hooks"]
    assert data["hooks"]["SessionStart"]
    # Backups were written.
    assert list(tmp_path.glob("settings.json.bak-resume-*"))


# ------------------- B1/B2 regression: the DEFAULT settings path ---------------
# The original suite set CLAUDE_SETTINGS_PATH in every enable/disable test, so it
# only ever exercised the override branch — and the default branch shipped broken
# (`Path("")` is `PosixPath(".")`, which is truthy, so the `or` fallback never
# fired). These tests deliberately run with the env var UNSET.


def test_settings_path_defaults_to_home_when_env_unset(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("CLAUDE_SETTINGS_PATH", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    from rekol.cli_resume import _settings_path

    assert _settings_path() == tmp_path / ".claude" / "settings.json"


def test_settings_path_honours_env_override(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CLAUDE_SETTINGS_PATH", str(tmp_path / "custom.json"))
    from rekol.cli_resume import _settings_path

    assert _settings_path() == tmp_path / "custom.json"


def test_enable_status_disable_agree_with_no_env_override(tmp_path: Path, monkeypatch) -> None:
    """B2: with CLAUDE_SETTINGS_PATH unset, `status`/`disable` must SEE the hook
    that `enable` wrote. Previously both read an empty settings dict and reported
    the feature off while it was still wired — the worst failure mode for
    something that launches work autonomously."""
    _rekol_home(tmp_path, monkeypatch)
    monkeypatch.delenv("CLAUDE_SETTINGS_PATH", raising=False)
    fake_home = tmp_path / "fakehome"
    (fake_home / ".claude").mkdir(parents=True)
    (fake_home / ".claude" / "settings.json").write_text(
        '{"hooks": {"SessionStart": [{"matcher": "", "hooks": []}]}}'
    )
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))
    runner = CliRunner()

    result = runner.invoke(resume_cli, ["enable", "--no-launchd"])
    assert result.exit_code == 0, result.output  # B1: used to die on a traceback
    assert "registered" in result.output

    # status must report it ON (it previously said "no").
    result = runner.invoke(resume_cli, ["status"])
    assert result.exit_code == 0, result.output
    assert "hook registered:  yes" in result.output
    assert "auto-resume:      ENABLED" in result.output

    # disable must actually find and remove it (it previously said "not registered").
    result = runner.invoke(resume_cli, ["disable"])
    assert result.exit_code == 0, result.output
    assert "not registered" not in result.output
    data = json.loads((fake_home / ".claude" / "settings.json").read_text())
    assert "StopFailure" not in data.get("hooks", {})
    assert data["hooks"]["SessionStart"]  # untouched

    result = runner.invoke(resume_cli, ["status"])
    assert "hook registered:  no" in result.output


# --------------------------- M1: the kill-switch ------------------------------


def test_tick_refuses_when_not_enabled(tmp_path: Path) -> None:
    """A leftover journal must not keep resuming after `disable` — the exact
    Linux workflow we recommend (`enable --no-launchd` + a cron tick)."""
    index, home = tmp_path / "idx", tmp_path / "home"
    _claim(home, "t", "sess-1")
    _freeze(index, "sess-1", ts="2026-07-30T12:10:00")  # eligible in every other way
    assert tick(index, home, now=NOW, launcher=lambda sid, log: True) == []
    # Same inputs, opted in → resumes. Proves the marker is the only difference.
    _enable(index)
    assert len(tick(index, home, now=NOW, launcher=lambda sid, log: True)) == 1


def test_disable_clears_marker_and_journal(tmp_path: Path, monkeypatch) -> None:
    home = _rekol_home(tmp_path, monkeypatch)
    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    monkeypatch.setenv("CLAUDE_SETTINGS_PATH", str(settings))
    runner = CliRunner()
    assert runner.invoke(resume_cli, ["enable", "--no-launchd"]).exit_code == 0

    from rekol.config import load_config

    index_dir = load_config().index_dir
    assert is_enabled(index_dir)
    _freeze(index_dir, "sess-1", ts="2026-07-30T12:10:00")

    assert runner.invoke(resume_cli, ["disable"]).exit_code == 0
    assert not is_enabled(index_dir)
    # Stale freezes are gone, so re-enabling later can't fire an old one.
    assert not freeze_journal_path(index_dir).exists()
    _claim(home, "t", "sess-1")
    assert tick(index_dir, home, now=NOW, launcher=lambda sid, log: True) == []
