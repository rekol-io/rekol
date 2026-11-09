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


# ---------------------------------------------------------------------------
# Hook fallback parsing across BOTH renderings (Codex P2, 2026-11-06)
# ---------------------------------------------------------------------------
# `unrunnable_hooks` reported every newly rendered handler as "bare invocation
# with no absolute fallback — exits 127", because the parser only understood
# `command -v … || echo …`. Codex measured seven such findings against a real
# fixture install while the hooks themselves executed fine. That is precisely
# the false-positive shape this module exists to end: a working install reported
# broken. The old form is still parsed, because installs predating the change
# carry it and understanding only the new form would mis-report those instead.

_CURRENT_FORM = (
    "\"$(if [ -x '{path}' ]; then printf %s '{path}'; "
    "else command -v rekol || printf %s '{path}'; fi)\" _hook session-tasks"
)


def test_current_rendering_yields_the_recorded_binary() -> None:
    from rekol.update import _hook_fallback_path

    command = _CURRENT_FORM.format(path="/Users/leon/bin/rekol")
    assert _hook_fallback_path(command) == "/Users/leon/bin/rekol"


def test_an_apostrophe_in_the_recorded_path_survives_parsing() -> None:
    """The path is shell-single-quoted, so an apostrophe arrives as `'\\''`."""
    from rekol.update import _hook_fallback_path

    spliced = "/Users/o'\\''brien/bin/rekol"
    command = _CURRENT_FORM.format(path=spliced)
    assert _hook_fallback_path(command) == "/Users/o'brien/bin/rekol"


def test_an_ampersand_and_spaces_in_the_recorded_path_survive_parsing() -> None:
    """The two shapes #221 was actually about."""
    from rekol.update import _hook_fallback_path

    command = _CURRENT_FORM.format(path="/tmp/a b & c/rekol")
    assert _hook_fallback_path(command) == "/tmp/a b & c/rekol"


def test_the_previous_rendering_is_still_parsed() -> None:
    from rekol.update import _hook_fallback_path

    command = "\"$(command -v rekol || echo '/old/path/rekol')\" _hook session-tasks"
    assert _hook_fallback_path(command) == "/old/path/rekol"


def test_a_bare_invocation_still_reports_no_fallback() -> None:
    """The #159 bug itself must keep being detected."""
    from rekol.update import _hook_fallback_path

    assert _hook_fallback_path("rekol _hook session-tasks") is None


def test_the_memory_loaders_own_echo_is_not_mistaken_for_a_fallback() -> None:
    """The regression that made a working install look broken on a real machine."""
    from rekol.update import _hook_fallback_path

    command = "echo '[rekol] memory home not configured'; rekol _hook session-tasks"
    assert _hook_fallback_path(command) is None


def test_no_newly_rendered_handler_is_reported_unrunnable(tmp_path) -> None:
    """The end of Codex's seven false findings, asserted through the real API."""
    from rekol.update import unrunnable_hooks

    binary = tmp_path / "rekol"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    settings = {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": _CURRENT_FORM.format(path=str(binary)),
                        }
                    ],
                }
            ]
        }
    }
    assert unrunnable_hooks(settings, probe=False) == []


def test_an_unrelated_executable_preflight_is_not_taken_as_rekols_binary() -> None:
    """Codex's decoy, 2026-11-06. The false-health class, through a new token.

    With an unrelated `if [ -x '/usr/bin/true' ]` earlier in the command, the
    first version of the matcher returned `/usr/bin/true`, probed it, found it
    runnable, and reported a hook whose real fallback does not exist as healthy.
    The matcher now requires BOTH the `$(if [ -x …` opening of our own rendering
    and the `command -v rekol` only our substitution contains.
    """
    from rekol.update import _hook_fallback_path, unrunnable_hooks

    decoy = (
        "if [ -x '/usr/bin/true' ]; then :; fi; "
        "\"$(command -v rekol || echo '/missing/rekol')\" _hook session-tasks"
    )
    assert _hook_fallback_path(decoy) == "/missing/rekol"

    settings = {
        "hooks": {
            "SessionStart": [{"matcher": "", "hooks": [{"type": "command", "command": decoy}]}]
        }
    }
    found = unrunnable_hooks(settings, probe=False)
    assert found, "a hook whose fallback does not exist must be reported"
    assert "/missing/rekol" in found[0][1]


def test_a_recorded_path_containing_a_paren_is_parsed_and_health_agrees(tmp_path) -> None:
    """Codex P2: the parser scanned for a `)` and a path can contain one.

    `[^)]*?` bridged the `if [ -x … ]` preflight and `command -v rekol`, and it
    cannot cross a `)` — so a recorded path like `/Users/a)b/bin/rekol` made the
    match fail, the parser return None, and `doctor` report a perfectly working
    hook as "bare invocation … exits 127".

    The command is RENDERED the way install.sh renders it, then EXECUTED, so
    this asserts the thing that matters: the hook works, and health says so.
    Parsing it correctly while the shell could not run it would be no better
    than the bug.
    """
    import subprocess

    from rekol.update import _hook_fallback_path

    bin_dir = tmp_path / "a)b" / "bin"
    bin_dir.mkdir(parents=True)
    exe = bin_dir / "rekol"
    exe.write_text("#!/bin/sh\nprintf 'rekol 9.9.9\\n'\n")
    exe.chmod(0o755)

    # The renderer's own shape, including its single-quote escaping.
    quoted = str(exe).replace("'", "'\\''")
    invocation = (
        f"$(if [ -x '{quoted}' ]; then printf %s '{quoted}'; "
        f"else command -v rekol || printf %s '{quoted}'; fi)"
    )
    command = f'"{invocation}" --version'

    # 1. The shell really can run it, so the hook is not broken.
    completed = subprocess.run(
        ["/bin/sh", "-c", command],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "9.9.9" in completed.stdout, completed.stdout

    # 2. And the parser agrees about which binary that was.
    assert _hook_fallback_path(f'"{invocation}" _hook session-tasks 2>/dev/null || true') == str(
        exe
    )


def test_a_paren_in_the_path_does_not_make_a_hook_look_unrunnable(tmp_path, monkeypatch) -> None:
    """The end-to-end half: `unrunnable_hooks` must not flag it.

    This is the report a user actually sees, and it was saying "exits 127" about
    a hook that exits 0.
    """
    from rekol.update import unrunnable_hooks

    bin_dir = tmp_path / "a)b" / "bin"
    bin_dir.mkdir(parents=True)
    exe = bin_dir / "rekol"
    exe.write_text("#!/bin/sh\nprintf 'rekol 9.9.9\\n'\n")
    exe.chmod(0o755)
    quoted = str(exe).replace("'", "'\\''")
    invocation = (
        f"$(if [ -x '{quoted}' ]; then printf %s '{quoted}'; "
        f"else command -v rekol || printf %s '{quoted}'; fi)"
    )
    settings = {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "command": f'"{invocation}" _hook session-tasks 2>/dev/null || true',
                        }
                    ],
                }
            ]
        }
    }

    assert unrunnable_hooks(settings, probe=True) == []
