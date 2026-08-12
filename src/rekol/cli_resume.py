"""``rekol resume``: opt-in auto-resume across usage-limit freezes (#143, Phase A).

OFF by default — ``enable`` is the single deliberate opt-in that (a) registers
the ``StopFailure`` freeze-recorder hook in ``~/.claude/settings.json`` and
(b) installs a launchd agent running ``rekol resume tick`` periodically.
``disable`` reverses both. Everything state-ful lives in the local cache dir.

macOS-only for the watchdog install (launchd); ``tick`` itself is portable and
can be driven by cron elsewhere.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

import click

from rekol.config import load_config
from rekol.resume import enabled_marker_path, freeze_journal_path, is_enabled, ledger_path
from rekol.resume import tick as run_tick


def _hook_command() -> str:
    """The StopFailure hook command, PATH-independent (#159).

    A bare `rekol` fails with exit 127 in any hook whose shell did not inherit an
    interactive PATH — hooks read .zshenv/.zprofile but not .zshrc, and that is
    where BIN_DIR is added. This hook is registered by `resume enable` rather than
    by install.sh, so it needs the same guard the snippets get: try PATH first,
    fall back to the absolute path of the rekol we are currently running as.
    """
    resolved = shutil.which("rekol") or str(Path(sys.executable).with_name("rekol"))
    return f'"$(command -v rekol || echo {resolved})" _hook stop-failure-record 2>/dev/null || true'


_HOOK_MARKER = "_hook stop-failure-record"
_PLIST_LABEL = "io.rekol.resume-watchdog"
_TICK_INTERVAL_SECONDS = 300


def _settings_path() -> Path:
    """Resolve Claude Code's settings.json, honouring the test/override env var.

    Must branch on the RAW STRING, not on the Path: ``Path("")`` is
    ``PosixPath(".")``, and Path defines no ``__bool__``, so a truthiness-based
    ``or`` fallback silently never fires. That bug made ``enable`` crash for
    every user with the env var unset (``PosixPath('.') has an empty name``) and
    — far worse — made ``status``/``disable`` read an empty settings dict, so
    they reported the feature OFF while the hook was still wired.
    """
    raw = os.environ.get("CLAUDE_SETTINGS_PATH", "")
    return Path(raw) if raw else (Path.home() / ".claude" / "settings.json")


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{_PLIST_LABEL}.plist"


def _load_settings(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"error: {path} is not valid JSON ({exc}) — fix it before enabling"
        ) from exc


def _hook_registered(settings: dict) -> bool:
    for block in settings.get("hooks", {}).get("StopFailure", []) or []:
        for hook in block.get("hooks", []) or []:
            if _HOOK_MARKER in str(hook.get("command", "")):
                return True
    return False


def _write_settings(path: Path, settings: dict) -> None:
    """Atomic write with a timestamped backup — settings.json is shared state."""
    backup = path.with_name(
        path.name + ".bak-resume-" + _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    )
    if path.is_file():
        shutil.copy2(path, backup)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


@click.group(name="resume")
def main() -> None:
    """Opt-in auto-resume of usage-limit-frozen sessions (OFF by default)."""


@main.command(name="enable")
@click.option(
    "--no-launchd",
    is_flag=True,
    help="Register the freeze-recorder hook only; skip the launchd watchdog "
    "(drive `rekol resume tick` yourself, e.g. via cron).",
)
def enable(no_launchd: bool) -> None:
    """Opt in: register the freeze-recorder hook + install the tick watchdog."""
    # The explicit opt-in marker `tick` gates on. Written first so a crash later
    # in enable can only leave the feature LESS armed than the user asked for,
    # never more (the hook alone records; it never resumes).
    cfg = load_config()
    cfg.index_dir.mkdir(parents=True, exist_ok=True)
    enabled_marker_path(cfg.index_dir).write_text(
        _dt.datetime.now().isoformat(timespec="seconds") + "\n"
    )
    settings_file = _settings_path()
    settings_file.parent.mkdir(parents=True, exist_ok=True)
    settings = _load_settings(settings_file)
    if _hook_registered(settings):
        click.echo("freeze-recorder hook: already registered")
    else:
        hooks = settings.setdefault("hooks", {})
        hooks.setdefault("StopFailure", []).append(
            {"matcher": "", "hooks": [{"type": "command", "command": _hook_command()}]}
        )
        _write_settings(settings_file, settings)
        click.echo(f"freeze-recorder hook: registered in {settings_file} (backup written)")

    if no_launchd:
        click.echo(
            "watchdog: skipped (--no-launchd) — run `rekol resume tick` on your own schedule"
        )
        return
    if sys.platform != "darwin":
        click.echo("watchdog: launchd is macOS-only — drive `rekol resume tick` via cron instead")
        return
    rekol_bin = shutil.which("rekol")
    if rekol_bin is None:
        raise SystemExit("error: `rekol` not found on PATH — cannot install the watchdog")
    plist = {
        "Label": _PLIST_LABEL,
        "ProgramArguments": [rekol_bin, "resume", "tick"],
        "StartInterval": _TICK_INTERVAL_SECONDS,
        "RunAtLoad": False,
        "EnvironmentVariables": {
            k: v for k, v in os.environ.items() if k in ("REKOL_HOME", "MEMORY_HOME", "PATH")
        },
    }
    plist_file = _plist_path()
    plist_file.parent.mkdir(parents=True, exist_ok=True)
    plist_file.write_bytes(plistlib.dumps(plist))
    subprocess.run(["launchctl", "unload", str(plist_file)], capture_output=True, check=False)
    subprocess.run(["launchctl", "load", str(plist_file)], capture_output=True, check=True)
    click.echo(f"watchdog: launchd agent loaded ({plist_file}, every {_TICK_INTERVAL_SECONDS}s)")


@main.command(name="disable")
def disable() -> None:
    """Opt out: remove the freeze-recorder hook and the watchdog."""
    # Drop the kill-switch marker FIRST: from this instant `tick` refuses, so a
    # user-scheduled cron tick stops resuming even if the steps below fail. Also
    # clear the journal — a stale freeze recorded before disabling must not be
    # able to fire if the feature is ever re-enabled.
    cfg = load_config()
    enabled_marker_path(cfg.index_dir).unlink(missing_ok=True)
    journal = freeze_journal_path(cfg.index_dir)
    if journal.exists():
        journal.unlink()
        click.echo("freeze journal: cleared")
    click.echo("auto-resume: disabled (tick will not resume)")

    settings_file = _settings_path()
    settings = _load_settings(settings_file)
    blocks = settings.get("hooks", {}).get("StopFailure", []) or []
    kept = [
        b
        for b in blocks
        if not any(_HOOK_MARKER in str(h.get("command", "")) for h in b.get("hooks", []))
    ]
    if len(kept) != len(blocks):
        settings["hooks"]["StopFailure"] = kept
        if not kept:
            del settings["hooks"]["StopFailure"]
        _write_settings(settings_file, settings)
        click.echo("freeze-recorder hook: removed (backup written)")
    else:
        click.echo("freeze-recorder hook: not registered")

    plist_file = _plist_path()
    if plist_file.is_file():
        subprocess.run(["launchctl", "unload", str(plist_file)], capture_output=True, check=False)
        plist_file.unlink()
        click.echo("watchdog: launchd agent removed")
    else:
        click.echo("watchdog: not installed")


@main.command(name="status")
def status() -> None:
    """Show opt-in state, recent freezes, and past resumes."""
    cfg = load_config()
    settings_file = _settings_path()
    settings = _load_settings(settings_file)
    click.echo(f"auto-resume:      {'ENABLED' if is_enabled(cfg.index_dir) else 'disabled'}")
    registered = "yes" if _hook_registered(settings) else "no"
    click.echo(f"hook registered:  {registered}  ({settings_file})")
    click.echo(f"watchdog plist:   {'yes' if _plist_path().is_file() else 'no'}")
    journal = freeze_journal_path(cfg.index_dir)
    entries = journal.read_text(encoding="utf-8").splitlines() if journal.is_file() else []
    click.echo(f"freezes recorded: {len(entries)}")
    for line in entries[-3:]:
        click.echo(f"  {line[:120]}")
    ledger = ledger_path(cfg.index_dir)
    resumed = ledger.read_text(encoding="utf-8").splitlines() if ledger.is_file() else []
    click.echo(f"resumes launched: {len(resumed)}")
    for line in resumed[-3:]:
        click.echo(f"  {line[:120]}")


@main.command(name="tick")
@click.option("--dry-run", is_flag=True, help="Decide but do not launch or write the ledger.")
def tick_cmd(dry_run: bool) -> None:
    """One watchdog pass (normally driven by launchd; safe to run by hand)."""
    cfg = load_config()
    actions = run_tick(cfg.index_dir, cfg.memory_home, dry_run=dry_run)
    if not actions:
        click.echo("nothing to resume")
        return
    for action in actions:
        verb = (
            "would resume" if dry_run else ("resumed" if action.launched else "LAUNCH FAILED for")
        )
        click.echo(
            f"{verb} session {action.session_id} (task {action.task_id}, "
            f"frozen {action.entry_ts}, reset {action.resume_at:%Y-%m-%d %H:%M})"
        )
