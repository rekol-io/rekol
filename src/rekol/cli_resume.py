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

from rekol.config import load_config, resolve_claude_config_dir
from rekol.resume import (
    WATCHDOG_LOG_NAME,
    enabled_marker_path,
    freeze_journal_path,
    is_enabled,
    ledger_path,
)
from rekol.resume import tick as run_tick

# Substring that identifies OUR hook regardless of how the invocation is
# spelled. Detection must key on this, never on the full command text: the
# command has already changed once (bare `rekol` → PATH-independent) and
# exact-match detection would have re-registered a duplicate on every enable.
_HOOK_MARKER = "_hook stop-failure-record"
_PLIST_LABEL = "io.rekol.resume-watchdog"
_TICK_INTERVAL_SECONDS = 300


def _rekol_executable() -> str:
    """Absolute path to this rekol's console script.

    Derived from the running interpreter rather than ``PATH``: ``sys.executable``
    is the venv python that is executing right now, so its sibling ``rekol`` is
    guaranteed to be the one the user just invoked. ``shutil.which`` is only a
    fallback for an unusual layout (e.g. an entry point outside the venv bin).
    """
    candidate = Path(sys.executable).parent / "rekol"
    if candidate.exists():
        return str(candidate)
    return shutil.which("rekol") or str(candidate)


def _watchdog_path() -> str:
    """PATH for the launchd job, guaranteeing ``claude`` stays resolvable.

    ``_launch_detached`` needs ``claude`` on PATH; if it is not, the resume is
    consumed from the ledger and silently never happens. Pin the directory we
    can see right now (the enabling shell HAS the user's real PATH) ahead of a
    conservative default, so a leaner launchd environment cannot lose it.
    """
    parts: list[str] = []
    for tool in ("claude", "rekol"):
        found = shutil.which(tool)
        if found:
            parent = str(Path(found).parent)
            if parent not in parts:
                parts.append(parent)
    for fallback in (os.environ.get("PATH", ""), "/usr/local/bin:/usr/bin:/bin"):
        if fallback and fallback not in parts:
            parts.append(fallback)
    return ":".join(parts)


def _hook_command() -> str:
    """The StopFailure hook command, PATH-independent (#159).

    Claude Code runs hooks in a NON-INTERACTIVE shell, which reads ``.zshenv``
    but not ``.zshrc`` — so a bare ``rekol`` exits 127. The hook then swallowed
    that with its own ``2>/dev/null || true`` and the freeze journal stayed
    empty forever while ``status`` reported the feature ENABLED.

    Same rendered shape ``install.sh`` uses: prefer whatever is on PATH when a
    login shell *is* present, else fall back to the absolute path recorded at
    enable time (so a moved venv degrades to PATH rather than breaking).
    """
    return (
        f"\"$(command -v rekol || echo '{_rekol_executable()}')\" "
        f"{_HOOK_MARKER} 2>/dev/null || true"
    )


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
    return Path(raw) if raw else resolve_claude_config_dir() / "settings.json"


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


def _our_hooks(settings: dict) -> list[dict]:
    """Every hook entry in StopFailure that is ours, matched by marker."""
    found = []
    for block in settings.get("hooks", {}).get("StopFailure", []) or []:
        for hook in block.get("hooks", []) or []:
            if _HOOK_MARKER in str(hook.get("command", "")):
                found.append(hook)
    return found


def _hook_registered(settings: dict) -> bool:
    return bool(_our_hooks(settings))


def _repair_hooks(settings: dict, desired: str) -> int:
    """Rewrite any of our hook entries whose command is stale. Returns the count.

    Registration alone is not health: every install that ran ``resume enable``
    before #159 carries a bare ``rekol`` command that cannot execute. Without
    this, ``enable`` saw the marker, printed "already registered", and left the
    broken command in place forever — the upgrade path nobody tested.
    """
    repaired = 0
    for hook in _our_hooks(settings):
        if str(hook.get("command", "")) != desired:
            hook["command"] = desired
            repaired += 1
    return repaired


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
    desired = _hook_command()
    if _hook_registered(settings):
        repaired = _repair_hooks(settings, desired)
        if repaired:
            _write_settings(settings_file, settings)
            click.echo(
                f"freeze-recorder hook: REPAIRED {repaired} stale invocation(s) in "
                f"{settings_file} — the previous command could not run in a "
                "non-interactive shell, so no freeze was ever recorded (backup written)"
            )
        else:
            click.echo("freeze-recorder hook: already registered")
    else:
        hooks = settings.setdefault("hooks", {})
        hooks.setdefault("StopFailure", []).append(
            {"matcher": "", "hooks": [{"type": "command", "command": desired}]}
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
    log_file = cfg.index_dir / WATCHDOG_LOG_NAME
    plist = {
        "Label": _PLIST_LABEL,
        "ProgramArguments": [_rekol_executable(), "resume", "tick"],
        "StartInterval": _TICK_INTERVAL_SECONDS,
        "RunAtLoad": False,
        # Pin the ALREADY-RESOLVED locations rather than copying a subset of the
        # environment. An allowlist of variable names silently omitted
        # REKOL_INDEX_DIR and XDG_CACHE_HOME, so a user with XDG_CACHE_HOME set
        # had `enable` write the opt-in marker to one directory while the
        # launchd tick looked in another, found no marker, and did nothing
        # forever — while `status`, run in the user's shell, printed ENABLED.
        # Passing resolved absolute paths removes the class: there is nothing
        # left for the tick to resolve differently, and the next env override
        # someone adds cannot reintroduce the bug.
        "EnvironmentVariables": {
            "REKOL_HOME": str(cfg.memory_home),
            "REKOL_INDEX_DIR": str(cfg.index_dir),
            "CLAUDE_CONFIG_DIR": str(resolve_claude_config_dir()),
            "PATH": _watchdog_path(),
        },
        # Without these, launchd discards stdout/stderr — including the one
        # "LAUNCH FAILED" line that says the feature is broken. A mechanism that
        # fails must fail somewhere a human can look.
        "StandardOutPath": str(log_file),
        "StandardErrorPath": str(log_file),
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
    # "Registered" is not "works". Every pre-#159 install has a bare `rekol`
    # command that exits 127 in the non-interactive shell hooks run in, so
    # reporting only registration is how this feature claimed to be armed for
    # weeks while recording nothing. Say which one it is.
    hooks = _our_hooks(settings)
    if not hooks:
        click.echo(f"hook registered:  no  ({settings_file})")
    else:
        stale = [h for h in hooks if str(h.get("command", "")) != _hook_command()]
        if stale:
            click.echo(
                f"hook registered:  yes but BROKEN — {len(stale)} stale invocation(s) "
                f"({settings_file})"
            )
            click.echo("                  run `rekol resume enable` to repair")
        else:
            click.echo(f"hook registered:  yes  ({settings_file})")
    click.echo(f"watchdog plist:   {'yes' if _plist_path().is_file() else 'no'}")
    journal = freeze_journal_path(cfg.index_dir)
    entries = journal.read_text(encoding="utf-8").splitlines() if journal.is_file() else []
    click.echo(f"freezes recorded: {len(entries)}")
    for line in entries[-3:]:
        click.echo(f"  {line[:120]}")
    ledger = ledger_path(cfg.index_dir)
    resumed = ledger.read_text(encoding="utf-8").splitlines() if ledger.is_file() else []
    # Count CLAIMS and OUTCOMES separately. The ledger is claim-first by design,
    # so counting raw lines reported a resume that may never have launched.
    claims, launched, failed = 0, 0, 0
    for line in resumed:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue  # a torn append must not break status
        outcome = record.get("outcome")
        if outcome is None:
            claims += 1
        elif outcome == "launched":
            launched += 1
        else:
            failed += 1
    click.echo(f"resumes claimed:  {claims}")
    click.echo(f"  launched:       {launched}")
    if failed:
        click.echo(f"  LAUNCH FAILED:  {failed}  (see {cfg.index_dir / WATCHDOG_LOG_NAME})")
    unknown = claims - launched - failed
    if unknown > 0:
        click.echo(f"  outcome unknown:{unknown}  (claimed before this version recorded outcomes)")
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
