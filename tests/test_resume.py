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
    actions = tick(
        index, home, now=NOW, launcher=lambda sid, log, cwd=None: launched.append(sid) or True
    )
    assert [a.session_id for a in actions] == ["sess-1"]
    assert actions[0].task_id == "big-refactor"
    assert launched == ["sess-1"]
    # Ledger written → idempotent: a second tick does nothing.
    assert tick(index, home, now=NOW, launcher=lambda sid, log, cwd=None: True) == []


def test_tick_skips_unclaimed_session(tmp_path: Path) -> None:
    index, home = tmp_path / "idx", tmp_path / "home"
    _enable(index)
    _freeze(index, "sess-idle", ts="2026-07-30T12:10:00")
    assert tick(index, home, now=NOW, launcher=lambda sid, log, cwd=None: True) == []


def test_tick_waits_for_reset_time(tmp_path: Path) -> None:
    index, home = tmp_path / "idx", tmp_path / "home"
    _enable(index)
    _claim(home, "t", "sess-1")
    _freeze(index, "sess-1", ts="2026-07-30T12:10:00")
    early = dt.datetime(2026, 7, 30, 14, 0)  # before the 3:45pm reset
    assert tick(index, home, now=early, launcher=lambda sid, log, cwd=None: True) == []


def test_tick_fallback_delay_when_no_reset_in_message(tmp_path: Path) -> None:
    index, home = tmp_path / "idx", tmp_path / "home"
    _enable(index)
    _claim(home, "t", "sess-1")
    _freeze(index, "sess-1", ts="2026-07-30T17:30:00", message="opaque failure")
    # 30 min after freeze: fallback (60m) not yet elapsed.
    assert tick(index, home, now=NOW, launcher=lambda sid, log, cwd=None: True) == []
    # 61 min after: eligible.
    later = dt.datetime(2026, 7, 30, 18, 31)
    assert len(tick(index, home, now=later, launcher=lambda sid, log, cwd=None: True)) == 1


def test_tick_ignores_non_limit_error_types(tmp_path: Path) -> None:
    # The message must ALSO be non-limit-shaped. This test used to override only
    # error_type while keeping the default message ("...session limit · resets
    # 3:45pm"), so the payload contradicted itself and the test passed only
    # because the message was ignored entirely — which was the bug.
    index, home = tmp_path / "idx", tmp_path / "home"
    _enable(index)
    _claim(home, "t", "sess-1")
    _freeze(
        index,
        "sess-1",
        ts="2026-07-30T12:10:00",
        error_type="server_error",
        message="Internal server error — please try again",
    )
    assert tick(index, home, now=NOW, launcher=lambda sid, log, cwd=None: True) == []


def test_tick_fires_on_the_real_payload_shape(tmp_path: Path) -> None:
    """Regression: the shape four CAPTURED freezes actually had.

    No ``error_type``/``errorType`` key at all — only ``error`` — so the old
    ``error_type not in LIMIT_ERROR_TYPES`` gate skipped every entry and tick
    could never fire, while `status` reported the feature ENABLED.
    """
    index, home = tmp_path / "idx", tmp_path / "home"
    _enable(index)
    _claim(home, "t", "sess-1")
    index.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": "2026-07-30T12:10:00",
        "payload": {
            "session_id": "sess-1",
            "error": "You've reached your Fable 5 limit · resets 3:45pm",
        },
    }
    with freeze_journal_path(index).open("a") as handle:
        handle.write(json.dumps(entry) + "\n")

    actions = tick(index, home, now=NOW, launcher=lambda sid, log, cwd=None: True)
    assert len(actions) == 1
    assert actions[0].session_id == "sess-1"


def test_ledger_records_a_failed_launch_distinctly(tmp_path: Path) -> None:
    """A launch that did not happen must not read as a resume that did."""
    index, home = tmp_path / "idx", tmp_path / "home"
    _enable(index)
    _claim(home, "t", "sess-1")
    _freeze(index, "sess-1", ts="2026-07-30T12:10:00")

    actions = tick(index, home, now=NOW, launcher=lambda sid, log, cwd=None: False)
    assert len(actions) == 1 and actions[0].launched is False

    records = [json.loads(line) for line in ledger_path(index).read_text().splitlines()]
    outcomes = [r["outcome"] for r in records if "outcome" in r]
    assert outcomes == ["launch_failed"]
    # The claim is still recorded, so the failed attempt is never silently retried.
    assert any("outcome" not in r for r in records)


def test_tick_ignores_stale_freezes(tmp_path: Path) -> None:
    index, home = tmp_path / "idx", tmp_path / "home"
    _enable(index)
    _claim(home, "t", "sess-1")
    _freeze(index, "sess-1", ts="2026-07-20T12:10:00")  # 10 days old
    assert tick(index, home, now=NOW, launcher=lambda sid, log, cwd=None: True) == []


def test_tick_caps_at_one_resume(tmp_path: Path) -> None:
    index, home = tmp_path / "idx", tmp_path / "home"
    _enable(index)
    _claim(home, "t1", "sess-1")
    _claim(home, "t2", "sess-2")
    _freeze(index, "sess-1", ts="2026-07-30T12:00:00")
    _freeze(index, "sess-2", ts="2026-07-30T12:05:00")
    launched: list[str] = []
    actions = tick(
        index, home, now=NOW, launcher=lambda sid, log, cwd=None: launched.append(sid) or True
    )
    assert len(actions) == 1 and len(launched) == 1
    # Next tick picks up the other one.
    actions2 = tick(
        index, home, now=NOW, launcher=lambda sid, log, cwd=None: launched.append(sid) or True
    )
    assert len(actions2) == 1
    assert set(launched) == {"sess-1", "sess-2"}


def test_tick_dry_run_writes_nothing(tmp_path: Path) -> None:
    index, home = tmp_path / "idx", tmp_path / "home"
    _enable(index)
    _claim(home, "t", "sess-1")
    _freeze(index, "sess-1", ts="2026-07-30T12:10:00")
    actions = tick(index, home, now=NOW, dry_run=True, launcher=lambda sid, log, cwd=None: True)
    assert len(actions) == 1 and actions[0].launched is False
    assert not ledger_path(index).exists()
    # Real tick still fires afterwards (dry-run left no ledger mark).
    assert len(tick(index, home, now=NOW, launcher=lambda sid, log, cwd=None: True)) == 1


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
    # This test is ABOUT the ~/.claude fallback, so it must clear the override
    # conftest sets — otherwise it would assert the override branch and the
    # default branch (the one every user hits) would go unexercised again.
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
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
    assert tick(index, home, now=NOW, launcher=lambda sid, log, cwd=None: True) == []
    # Same inputs, opted in → resumes. Proves the marker is the only difference.
    _enable(index)
    assert len(tick(index, home, now=NOW, launcher=lambda sid, log, cwd=None: True)) == 1


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
    assert tick(index_dir, home, now=NOW, launcher=lambda sid, log, cwd=None: True) == []


# --------------- The four defects that made this feature inert -----------------
# All four presented identically: `status` reported the feature ENABLED while the
# mechanism could not work. Each test asserts the mechanism, never the report.


def test_hook_command_is_path_independent() -> None:
    """Defect 1 (#159 sibling): a bare `rekol` exits 127 in the non-interactive
    shell hooks run in, and the hook's own `|| true` swallows it — so the freeze
    journal stayed empty forever."""
    import re

    from rekol.cli_resume import _hook_command

    command = _hook_command()
    assert not command.startswith("rekol ")
    assert "command -v rekol" in command
    fallback = re.search(r"echo '([^']+)'", command)
    assert fallback is not None, command
    assert Path(fallback.group(1)).is_absolute()


def test_enable_repairs_a_stale_bare_invocation(tmp_path: Path, monkeypatch) -> None:
    """Registration is not health. Every install that ran `enable` before this
    fix carries a command that cannot execute; `enable` used to see the marker,
    print "already registered", and leave it broken — the untested upgrade path."""
    _rekol_home(tmp_path, monkeypatch)
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "StopFailure": [
                        {
                            "matcher": "",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "rekol _hook stop-failure-record "
                                    "2>/dev/null || true",
                                }
                            ],
                        }
                    ]
                }
            }
        )
    )
    monkeypatch.setenv("CLAUDE_SETTINGS_PATH", str(settings))
    runner = CliRunner()

    result = runner.invoke(resume_cli, ["enable", "--no-launchd"])
    assert result.exit_code == 0, result.output
    assert "REPAIRED" in result.output
    command = json.loads(settings.read_text())["hooks"]["StopFailure"][0]["hooks"][0]["command"]
    assert "command -v rekol" in command

    # Repair is idempotent: a second enable must not rewrite or duplicate.
    result = runner.invoke(resume_cli, ["enable", "--no-launchd"])
    assert "already registered" in result.output
    data = json.loads(settings.read_text())
    commands = [h["command"] for b in data["hooks"]["StopFailure"] for h in b["hooks"]]
    assert len(commands) == 1


def test_status_reports_a_stale_hook_as_broken(tmp_path: Path, monkeypatch) -> None:
    _rekol_home(tmp_path, monkeypatch)
    settings = tmp_path / "settings.json"
    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "StopFailure": [
                        {
                            "matcher": "",
                            "hooks": [
                                {"type": "command", "command": "rekol _hook stop-failure-record"}
                            ],
                        }
                    ]
                }
            }
        )
    )
    monkeypatch.setenv("CLAUDE_SETTINGS_PATH", str(settings))
    result = CliRunner().invoke(resume_cli, ["status"])
    assert result.exit_code == 0, result.output
    assert "BROKEN" in result.output


def test_settings_path_honours_claude_config_dir(tmp_path: Path, monkeypatch) -> None:
    """Defect 3: Claude Code relocates its tree via CLAUDE_CONFIG_DIR. Writing to
    ~/.claude anyway put the hook in a settings.json Claude Code never reads."""
    monkeypatch.delenv("CLAUDE_SETTINGS_PATH", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "relocated"))
    from rekol.cli_resume import _settings_path

    assert _settings_path() == tmp_path / "relocated" / "settings.json"


def test_empty_claude_config_dir_falls_back(tmp_path: Path, monkeypatch) -> None:
    """`Path("")` is `PosixPath(".")` and truthy — the same trap as B1."""
    monkeypatch.delenv("CLAUDE_SETTINGS_PATH", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "   ")
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    from rekol.cli_resume import _settings_path

    assert _settings_path() == tmp_path / ".claude" / "settings.json"


def test_plist_pins_the_resolved_index_dir(tmp_path: Path, monkeypatch) -> None:
    """Defect 2: the plist copied an ALLOWLIST of variable names that omitted
    REKOL_INDEX_DIR and XDG_CACHE_HOME. With XDG_CACHE_HOME set, `enable` wrote
    the opt-in marker to one directory and the launchd tick looked in another,
    found no marker, and did nothing forever — silently, since launchd discarded
    stderr. Assert the plist points at the directory the marker is actually in."""
    import plistlib

    import rekol.cli_resume as mod
    from rekol.config import load_config
    from rekol.resume import RESUME_ENABLED_NAME

    monkeypatch.setenv("REKOL_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.delenv("REKOL_INDEX_DIR", raising=False)
    monkeypatch.setenv("CLAUDE_SETTINGS_PATH", str(tmp_path / "settings.json"))
    plist_file = tmp_path / "agent.plist"
    monkeypatch.setattr(mod, "_plist_path", lambda: plist_file)
    monkeypatch.setattr(mod.sys, "platform", "darwin")
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: None)

    result = CliRunner().invoke(resume_cli, ["enable"])
    assert result.exit_code == 0, result.output

    plist = plistlib.loads(plist_file.read_bytes())
    env = plist["EnvironmentVariables"]
    resolved = load_config().index_dir
    assert env["REKOL_INDEX_DIR"] == str(resolved)
    assert (resolved / RESUME_ENABLED_NAME).is_file()
    assert str(tmp_path / "cache") in env["REKOL_INDEX_DIR"]
    # Defect 4's diagnostic half: launchd must not discard the failure message.
    assert plist["StandardErrorPath"].endswith("resume-watchdog.log")


def test_resume_launches_in_the_project_dir_from_the_payload(tmp_path: Path) -> None:
    """Claude Code stores transcripts per project
    (``<config>/projects/<escaped-cwd>/<session-id>.jsonl``), so a resume run from
    the wrong directory cannot find the session. The launchd job's cwd is not the
    project, and every captured freeze payload carries `cwd` — so pass it."""
    index, home = tmp_path / "idx", tmp_path / "home"
    project = tmp_path / "someproject"
    project.mkdir()
    _enable(index)
    _claim(home, "t", "sess-1")
    index.mkdir(parents=True, exist_ok=True)
    # The REAL payload shape: `error` code, `cwd`, no error_type, no message.
    entry = {
        "ts": "2026-07-30T12:10:00",
        "payload": {"session_id": "sess-1", "error": "rate_limit", "cwd": str(project)},
    }
    with freeze_journal_path(index).open("a") as handle:
        handle.write(json.dumps(entry) + "\n")

    seen: list[str | None] = []
    actions = tick(
        index,
        home,
        now=dt.datetime(2026, 7, 30, 14, 0),  # past the 60-min fallback
        launcher=lambda sid, log, cwd=None: (seen.append(cwd), True)[1],
    )
    assert len(actions) == 1
    assert seen == [str(project)]


def test_launcher_tolerates_a_cwd_that_no_longer_exists(tmp_path: Path) -> None:
    """A recorded project dir can be deleted or moved between freeze and resume.
    Popen would raise FileNotFoundError and take the whole tick down with it."""
    from rekol.resume import _launch_detached

    log = tmp_path / "launch.log"
    # `claude` is absent in CI, so this exercises the guard, not a real launch.
    assert _launch_detached("sess-1", log, cwd=str(tmp_path / "gone")) in (True, False)
