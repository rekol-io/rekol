"""Tests for #27 offline drift detection: version stamp + hook wiring."""

from __future__ import annotations

import json
from pathlib import Path

from rekol import __version__
from rekol.update import (
    OPT_IN_HANDLERS,
    _hook_fallback_path,
    detect_drift,
    expected_handlers,
    load_settings,
    read_manifest,
    snippet_handlers,
    unrunnable_hooks,
    wired_handlers,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _manifest(home: Path, **kv: str) -> None:
    d = home / ".install-logs"
    d.mkdir(parents=True, exist_ok=True)
    lines = ["# rekol install manifest", "# whitelisted KEY=value only"]
    lines += [f"{k}={v}" for k, v in kv.items()]
    (d / "manifest.env").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _settings(handlers: list[str]) -> dict:
    return {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f'"$(command -v rekol || echo /x/rekol)" _hook {h} '
                            "2>/dev/null || true",
                        }
                    ],
                }
                for h in handlers
            ]
        }
    }


# ------------- the guard that keeps the two sources from diverging -------------


def test_cli_and_snippet_handler_sets_agree() -> None:
    """The CI guard for this whole feature.

    ``expected_handlers()`` reads the CLI's hook group (always available, even in
    a wheel install); ``snippet_handlers()`` reads what install.sh actually wires.
    If someone adds a handler and forgets either to wire it or to mark it opt-in,
    these diverge — and drift detection would either nag forever about a handler
    nothing wires, or silently never check a handler that should be wired. Failing
    here is the point.
    """
    from_cli = expected_handlers()
    from_snippets = snippet_handlers(REPO_ROOT / "hooks")
    assert from_snippets, "no snippets found — the cross-check would pass vacuously"
    assert from_cli == from_snippets, (
        f"handler sets disagree: only-in-CLI={sorted(from_cli - from_snippets)} "
        f"only-in-snippets={sorted(from_snippets - from_cli)}. "
        f"Either wire it in hooks/*-snippet.json or add it to OPT_IN_HANDLERS."
    )


def test_opt_in_handlers_are_real_handlers() -> None:
    """An opt-in name that no longer exists would silently exclude nothing."""
    from rekol.cli_hooks import hook_group

    known = set(hook_group.commands.keys())
    assert OPT_IN_HANDLERS <= known, f"unknown opt-in handler(s): {OPT_IN_HANDLERS - known}"


# ------------------------------- wiring drift ---------------------------------


def test_detects_a_handler_that_is_not_wired(tmp_path: Path) -> None:
    """The actual bug: handlers ship, the install never registers them, silence."""
    _manifest(tmp_path, VERSION=__version__, INSTALLED_AT="20260601-000000")
    all_handlers = sorted(expected_handlers())
    drift = detect_drift(tmp_path, settings=_settings(all_handlers[1:]))
    assert drift.missing_handlers == [all_handlers[0]]
    assert drift.has_drift


def test_no_wiring_drift_when_everything_is_registered(tmp_path: Path) -> None:
    _manifest(tmp_path, VERSION=__version__, INSTALLED_AT="20260601-000000")
    drift = detect_drift(tmp_path, settings=_settings(sorted(expected_handlers())))
    assert drift.missing_handlers == []
    assert not drift.has_drift


def test_opt_in_handlers_absence_is_not_drift(tmp_path: Path) -> None:
    """Nagging about a handler the user deliberately did not enable would train
    people to ignore this check, which costs more than the check gains."""
    _manifest(tmp_path, VERSION=__version__)
    drift = detect_drift(tmp_path, settings=_settings(sorted(expected_handlers())))
    assert not any(h in OPT_IN_HANDLERS for h in drift.missing_handlers)


# ------------------------------ version drift ---------------------------------


def test_version_drift_is_detected(tmp_path: Path) -> None:
    _manifest(tmp_path, VERSION="0.3.1", INSTALLED_AT="20260601-000000")
    drift = detect_drift(tmp_path, settings=_settings(sorted(expected_handlers())))
    assert drift.version_drifted
    assert drift.installed_version == "0.3.1"
    assert drift.running_version == __version__


def test_absent_version_is_unknown_not_drifted(tmp_path: Path) -> None:
    """A pre-#27 manifest must not read as a mismatch — a warning that cannot be
    cleared is worse than no warning."""
    _manifest(tmp_path, INSTALLED_AT="20260601-000000")
    drift = detect_drift(tmp_path, settings=_settings(sorted(expected_handlers())))
    assert drift.version_unknown
    assert not drift.version_drifted
    assert not drift.has_drift


def test_matching_version_is_neither(tmp_path: Path) -> None:
    _manifest(tmp_path, VERSION=__version__)
    drift = detect_drift(tmp_path, settings=_settings(sorted(expected_handlers())))
    assert not drift.version_drifted and not drift.version_unknown


# --------------------------- manifest parsing safety --------------------------


def test_manifest_is_parsed_not_sourced(tmp_path: Path) -> None:
    """The manifest lives in a synced tree, so it must never be able to execute.

    Values are read as literal strings and unknown keys are dropped.
    """
    d = tmp_path / ".install-logs"
    d.mkdir(parents=True)
    (d / "manifest.env").write_text(
        "# comment\n"
        "VERSION=0.3.1\n"
        "EVIL=$(touch /tmp/rekol-should-not-exist)\n"
        "malformed line with no equals\n"
        "COMMIT=abc1234\n",
        encoding="utf-8",
    )
    values = read_manifest(tmp_path)
    assert values["VERSION"] == "0.3.1"
    assert values["COMMIT"] == "abc1234"
    assert "EVIL" not in values  # not whitelisted
    assert not Path("/tmp/rekol-should-not-exist").exists()


def test_missing_manifest_reads_as_empty(tmp_path: Path) -> None:
    assert read_manifest(tmp_path) == {}


def test_unparseable_settings_reads_as_empty_not_as_no_drift(tmp_path: Path) -> None:
    """A settings.json we cannot parse must not be reported as 'all wired'."""
    bad = tmp_path / "settings.json"
    bad.write_text("{not json", encoding="utf-8")
    assert load_settings(bad) == {}
    # And the resulting drift names every shipped handler as missing, rather than
    # silently claiming health.
    _manifest(tmp_path, VERSION=__version__)
    drift = detect_drift(tmp_path, settings=load_settings(bad))
    assert sorted(drift.missing_handlers) == sorted(expected_handlers())


def test_wired_handlers_reads_every_event(tmp_path: Path) -> None:
    """Handlers are spread across SessionStart/UserPromptSubmit/Stop/SessionEnd —
    scanning only one event would under-report and manufacture false drift."""
    settings = {
        "hooks": {
            "SessionStart": [{"hooks": [{"command": "rekol _hook session-tasks"}]}],
            "UserPromptSubmit": [{"hooks": [{"command": "rekol _hook time-context"}]}],
            "Stop": [{"hooks": [{"command": "rekol _hook record-stop"}]}],
        }
    }
    assert wired_handlers(settings) == {"session-tasks", "time-context", "record-stop"}


def test_wired_handlers_survives_malformed_blocks() -> None:
    """settings.json is user-editable; a null or odd block must not raise."""
    settings = {"hooks": {"SessionStart": None, "Stop": [{}, {"hooks": None}]}}
    assert wired_handlers(settings) == set()


def test_json_settings_roundtrip_from_disk(tmp_path: Path) -> None:
    p = tmp_path / "settings.json"
    p.write_text(json.dumps(_settings(["session-tasks"])), encoding="utf-8")
    assert wired_handlers(load_settings(p)) == {"session-tasks"}


# ---- registered is not runnable: the string-vs-execution gap (found by review) ----
# `✓ hook wiring: all 6 shipped handlers registered` + `index is healthy` (exit 0)
# was reproducibly printed for a settings.json where every command pointed at a
# path that does not exist. The evidence was text; the property is execution.


def _cmd(handler: str, fallback: str) -> str:
    return f"\"$(command -v rekol || echo '{fallback}')\" _hook {handler} 2>/dev/null || true"


def _settings_with(commands: list[str]) -> dict:
    return {
        "hooks": {
            "SessionStart": [
                {"matcher": "", "hooks": [{"type": "command", "command": c}]} for c in commands
            ]
        }
    }


def test_unrunnable_when_fallback_path_does_not_exist() -> None:
    settings = _settings_with([_cmd("session-tasks", "/nonexistent/path/rekol")])
    broken = unrunnable_hooks(settings)
    assert [name for name, _ in broken] == ["session-tasks"]
    assert "does not exist" in broken[0][1]


def test_unrunnable_when_command_is_bare() -> None:
    """A bare `rekol …` has no absolute fallback at all — the #159 bug itself."""
    settings = _settings_with(["rekol _hook session-tasks 2>/dev/null || true"])
    broken = unrunnable_hooks(settings)
    assert [name for name, _ in broken] == ["session-tasks"]
    assert "bare invocation" in broken[0][1]


def test_unrunnable_when_fallback_is_not_executable(tmp_path: Path) -> None:
    notexe = tmp_path / "rekol"
    notexe.write_text("#!/bin/sh\necho hi\n")
    notexe.chmod(0o644)  # readable, NOT executable
    broken = unrunnable_hooks(_settings_with([_cmd("session-tasks", str(notexe))]))
    assert "not executable" in broken[0][1]


def test_unrunnable_when_executable_cannot_actually_run(tmp_path: Path) -> None:
    """A console script whose interpreter was replaced stays executable but cannot
    import — existence and the x-bit are both insufficient."""
    broken_exe = tmp_path / "rekol"
    broken_exe.write_text("#!/nonexistent/python\nprint('never runs')\n")
    broken_exe.chmod(0o755)
    broken = unrunnable_hooks(_settings_with([_cmd("session-tasks", str(broken_exe))]))
    assert broken, "an unrunnable interpreter must be caught by the probe"
    assert "cannot run" in broken[0][1] or "not executable" in broken[0][1]


def test_runnable_hook_is_not_flagged() -> None:
    """The probe must not cry wolf: a fallback that genuinely runs is fine."""
    import sys

    assert unrunnable_hooks(_settings_with([_cmd("session-tasks", sys.executable)])) == []


def test_probe_ignores_non_rekol_hooks() -> None:
    """Other tools' hooks in the user's settings.json are none of our business."""
    settings = _settings_with(["some-other-tool --do-a-thing"])
    assert unrunnable_hooks(settings) == []


def test_fallback_is_anchored_to_the_substitution_not_the_first_echo() -> None:
    """Regression for a false positive I shipped into review and had to fix.

    The SessionStart memory-loader is a multi-statement shell command with several
    `echo '…'` calls BEFORE the rekol invocation. An unanchored search for
    `echo '…'` picked up `echo '[rekol] memory home not configured'` and reported
    it as a missing binary — flagging a perfectly working install as broken on a
    real machine. Same false-positive shape as calling a working hook "BROKEN" by
    string comparison, which is the bug this probe replaced.
    """
    import sys

    real = sys.executable
    command = (
        'HOME_DIR="${REKOL_HOME:-$MEMORY_HOME}"; '
        "if [ -n \"$HOME_DIR\" ]; then echo '[rekol] loaded'; "
        "elif [ -z \"$HOME_DIR\" ]; then echo '[rekol] memory home not configured'; "
        "else echo '[rekol] no REKOL.md found'; fi; "
        f"\"$(command -v rekol || echo '{real}')\" _hook session-confidence 2>/dev/null || true"
    )
    assert _hook_fallback_path(command) == real
    assert unrunnable_hooks(_settings_with([command])) == []


def test_decoy_echo_does_not_hide_a_genuinely_broken_fallback() -> None:
    """The anchoring must not become a way to miss a real failure."""
    command = (
        "echo '[rekol] a banner'; "
        "\"$(command -v nope-not-rekol || echo '/nonexistent/rekol')\" "
        "_hook session-tasks 2>/dev/null || true"
    )
    broken = unrunnable_hooks(_settings_with([command]))
    assert [name for name, _ in broken] == ["session-tasks"]
    assert "/nonexistent/rekol" in broken[0][1]
