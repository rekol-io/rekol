"""Hidden Claude Code hook subcommands: time-context + record-stop + session-confidence.

Soft-fail by design — any error degrades and exits 0 so a hook problem never blocks
a prompt (or, for ``session-confidence``, never breaks the SessionStart injection it
rides on). Per-session state lives at
``~/.claude/session-env/time-context-<session_id>.json``. ``session-confidence``
additionally reads the curated memory config (#87) to flag unverified always-on facts.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import sys
import time
from pathlib import Path

import click

from rekol.config import resolve_claude_config_dir

# Claude Code session ids are UUID-like; restrict to a safe charset before
# using one in a filesystem path (prevents traversal from a malformed payload).
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")


def _state_path(session_id: str) -> Path:
    """Return (and ensure the dir of) the per-session state file path."""
    directory = resolve_claude_config_dir() / "session-env"
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass  # soft-fail: a later read/write degrades gracefully and exits 0
    return directory / f"time-context-{session_id}.json"


def _read_payload() -> dict:
    """Read and parse the hook JSON payload from stdin; return {} on any error."""
    try:
        # `sys.stdin` rather than `click.get_text_stream("stdin")`: newer mypy
        # resolves click's lazy stream helper to `object`, which is not callable,
        # and this is a hook — it must not be the thing that breaks a session.
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except (ValueError, OSError):
        return {}


def _safe_session_id(payload: dict) -> str | None:
    """Extract a path-safe session id from the payload, or None."""
    session_id = str(payload.get("session_id", "")).strip()
    return session_id if _SAFE_ID.match(session_id) else None


def _emit_env_time(since_user: int | None, since_assistant: int | None) -> None:
    """Print the <env-time> block Claude Code injects as context for the turn."""
    now_local = dt.datetime.now().astimezone()
    now_utc = dt.datetime.now(dt.UTC)

    def _fmt(seconds: int | None) -> str:
        if seconds is None:
            return "unknown"
        minutes, secs = divmod(max(0, seconds), 60)
        return f"{minutes}m {secs}s"

    click.echo(
        "<env-time>\n"
        f"  local_time: {now_local.strftime('%Y-%m-%d %H:%M:%S %Z')} "
        f"(offset {now_local.strftime('%z')})\n"
        f"  utc: {now_utc.strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
        f"  time_since_last_user_message: {_fmt(since_user)}\n"
        f"  time_since_last_assistant_message: {_fmt(since_assistant)}\n"
        "</env-time>"
    )


@click.group(name="_hook", hidden=True)
def hook_group() -> None:
    """Internal Claude Code hook entrypoints (not for direct use)."""


@hook_group.command(name="time-context")
def time_context() -> None:
    """UserPromptSubmit hook: emit <env-time> and update per-session state."""
    payload = _read_payload()
    session_id = _safe_session_id(payload)
    if session_id is None:
        _emit_env_time(None, None)
        return
    now = int(time.time())
    prev: dict = {}
    path = _state_path(session_id)
    try:
        if path.exists():
            loaded = json.loads(path.read_text())
            prev = loaded if isinstance(loaded, dict) else {}
    except (ValueError, OSError):
        prev = {}
    last_user = prev.get("last_user_epoch")
    last_assistant = prev.get("last_assistant_epoch")
    _emit_env_time(
        now - last_user if isinstance(last_user, int) else None,
        now - last_assistant if isinstance(last_assistant, int) else None,
    )
    try:
        path.write_text(
            json.dumps({"last_user_epoch": now, "last_assistant_epoch": last_assistant})
        )
    except OSError as exc:
        click.echo(f"time-context: state write failed: {exc}", err=True)


@hook_group.command(name="record-stop")
def record_stop() -> None:
    """Stop hook: record the assistant-completion epoch (no stdout)."""
    payload = _read_payload()
    session_id = _safe_session_id(payload)
    if session_id is None:
        return
    path = _state_path(session_id)
    prev: dict = {}
    try:
        if path.exists():
            loaded = json.loads(path.read_text())
            prev = loaded if isinstance(loaded, dict) else {}
    except (ValueError, OSError):
        prev = {}
    prev["last_assistant_epoch"] = int(time.time())
    try:
        path.write_text(json.dumps(prev))
    except OSError as exc:
        click.echo(f"record-stop: state write failed: {exc}", err=True)


# Cap the always-on confidence footer so it stays a glanceable nudge, not a wall
# (a fresh store has every always-on file "never confirmed" — show a few, count the rest).
_CONFIDENCE_FOOTER_MAX = 6


def _parse_iso_date(value: object) -> dt.date | None:
    """Parse a YYYY-MM-DD(-ish) frontmatter value to a date, or None."""
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except (ValueError, TypeError):
        return None


def _always_confidence_lines() -> list[str]:
    """Confidence flags for the always-on layer, severity-ordered (#87, item 4).

    The always-on memories are the ones the agent volunteers *proactively,
    unprompted* — so a stale one is the most dangerous. Returns compact lines for
    suspect → overdue → never-confirmed always-layer files. Empty when all are
    confirmed-current (stay quiet). Best-effort: unreadable files are skipped.
    """
    from rekol.config import load_config
    from rekol.model import ValidationError, parse_file

    cfg = load_config()
    always_dir = cfg.memory_home / "always"
    if not always_dir.is_dir():
        return []
    interval = cfg.temporal_confirm_interval_days
    today = dt.date.today()

    suspect: list[str] = []
    overdue: list[str] = []
    never: list[str] = []
    for path in sorted(always_dir.glob("*.md")):
        try:
            mf = parse_file(path)
        except (ValidationError, OSError):
            continue  # invalid/unreadable frontmatter — skip silently
        if mf.invalidated_at:
            continue  # already retired; not volunteered
        name = f"always/{path.name}"
        if mf.suspected_at:
            reason = f" — {mf.suspect_reason}" if mf.suspect_reason else ""
            suspect.append(f"  · {name}: ⚠ suspected (since {mf.suspected_at}{reason})")
        elif mf.last_confirmed is None:
            never.append(f"  · {name}: never confirmed")
        else:
            ref = _parse_iso_date(mf.last_confirmed)
            if ref is not None and (today - ref).days > interval:
                overdue.append(f"  · {name}: confirmed {mf.last_confirmed} (overdue)")
    return suspect + overdue + never


@hook_group.command(name="session-confidence")
def session_confidence() -> None:
    """Append a confidence footer for always-on memories to the SessionStart injection.

    Rides on the SessionStart ``cat REKOL.md`` (appended with ``|| true``), so it
    must NEVER fail the injection: ANY error prints nothing and exits 0. When some
    always-on facts are suspect / overdue / never-confirmed, it prints a compact
    ⚠ footer so the agent hedges (or runs ``rekol confirm``) before asserting them
    unprompted — surface only, the agent decides.
    """
    try:
        lines = _always_confidence_lines()
    except Exception:  # noqa: BLE001 — a hook must never break the session injection
        return
    if not lines:
        return
    shown, extra = lines[:_CONFIDENCE_FOOTER_MAX], len(lines) - _CONFIDENCE_FOOTER_MAX
    click.echo("")
    click.echo(
        "⚠ rekol confidence — these always-on facts are unverified; confirm "
        "(`rekol confirm <file>`) or hedge before asserting them unprompted:"
    )
    for line in shown:
        click.echo(line)
    if extra > 0:
        click.echo(f"  …and {extra} more — run `rekol review`.")


@hook_group.command(name="session-coverage")
def session_coverage() -> None:
    """Warn at SessionStart when memory files are invisible to search (#123 part 2).

    A file rejected at index time (invalid frontmatter) stays readable on disk but
    never enters the index, so the user has NO signal it is unsearchable. The
    indexer persists the current invisible-file count to the cache; this reads it
    and prints one line when non-zero — push, don't wait for the user to pull.

    Rides on the SessionStart injection (appended with ``|| true``), so it must
    NEVER break it: any error prints nothing and exits 0. Silent when the count is
    zero or the manifest is absent (nothing indexed yet).
    """
    try:
        from rekol.config import SKIP_MANIFEST_NAME, load_config

        manifest = load_config().index_dir / SKIP_MANIFEST_NAME
        count = int(json.loads(manifest.read_text()).get("count", 0))
    except Exception:  # noqa: BLE001 — a hook must never break the session injection
        return
    if count <= 0:
        return
    noun = "file" if count == 1 else "files"
    click.echo("")
    click.echo(f"[rekol] ⚠ {count} memory {noun} invisible to search — run `rekol doctor`")


# Cap the injected task list so a large backlog can't crowd the context window;
# list_tasks() is oldest-first, so what shows is the longest-waiting work.
_TASKS_FOOTER_MAX = 10


def _blocked_reason(body: str) -> str:
    """Pull the reason out of a blocked task's body (`rekol task block --reason`)."""
    for line in reversed(body.splitlines()):
        if line.strip().startswith("Blocked:"):
            return line.strip()[len("Blocked:") :].strip()[:120]
    return ""


@hook_group.command(name="session-tasks")
def session_tasks() -> None:
    """Surface open/in_progress cross-session tasks at SessionStart (#113).

    The durable task layer only helps if a fresh session actually SEES it —
    this prints the open + in_progress tasks from ``$REKOL_HOME/tasks/`` so a
    new session starts already aware of unfinished work (and #143's resume flow
    lands in a session that knows what it was doing).

    Same contract as ``session-confidence``/``session-coverage``: rides the
    SessionStart injection, so it must NEVER break it — any error prints
    nothing and exits 0; silent when there are no tasks.
    """
    try:
        from rekol.config import load_config
        from rekol.tasks import list_tasks

        live = [
            t
            for t in list_tasks(load_config().memory_home)
            if t.status in ("open", "in_progress", "blocked")
        ]
    except Exception:  # noqa: BLE001 — a hook must never break the session injection
        return
    if not live:
        return
    # Blocked first, always shown: a blocked task is the one thing the user must
    # see — it means work stopped and something (often a decision from them) is
    # needed. Burying it under a long open list, or capping it away, would defeat
    # the point of a durable "I'm stuck" signal.
    blocked = [t for t in live if t.status == "blocked"]
    others = [t for t in live if t.status != "blocked"]
    shown, extra = others[:_TASKS_FOOTER_MAX], len(others) - _TASKS_FOOTER_MAX

    click.echo("")
    if blocked:
        click.echo("[rekol] ⚠ BLOCKED — work stopped, needs a decision:")
        for task in blocked:
            claim = f" [{task.owner_role}]" if task.owner_role else ""
            click.echo(f"  ⊘ {task.id}: {task.title}{claim}")
            reason = _blocked_reason(task.body)
            if reason:
                click.echo(f"      → {reason}")
    if shown:
        click.echo("[rekol] open tasks (durable, cross-session — manage with `rekol task`):")
        for task in shown:
            marker = "◐" if task.status == "in_progress" else "○"
            claim = f" [{task.owner_role}]" if task.owner_role else ""
            click.echo(f"  {marker} {task.id}: {task.title}{claim}")
        if extra > 0:
            click.echo(f"  …and {extra} more — run `rekol task list`.")


# Nudge a capture pass when context crosses this fill level (#122). Early on
# purpose: selection quality degrades as context fills — a flush at the brink is
# judged by a model at its worst moment, with tokens at their scarcest.
_CAPTURE_NUDGE_THRESHOLD_PCT = 60


def _session_env_path(name: str, session_id: str) -> Path:
    """Per-session state file under ~/.claude/session-env (shared soft-fail dir)."""
    directory = resolve_claude_config_dir() / "session-env"
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return directory / f"{name}-{session_id}"


@hook_group.command(name="context-watch")
def context_watch() -> None:
    """Record context usage from the statusline JSON (#122, opt-in wiring).

    The statusline input is the ONLY documented surface exposing context usage
    (``context_window.used_percentage``) — hook payloads carry no token data.
    Users pipe their statusline JSON through this (see ``docs/compaction.md``);
    it records the percentage for ``capture-nudge`` to act on and prints
    NOTHING (statusline stdout is the rendered line — ours must stay empty).
    Soft-fail: any error exits 0 silently.
    """
    try:
        payload = _read_payload()
        session_id = _safe_session_id(payload)
        if session_id is None:
            return
        pct = (payload.get("context_window") or {}).get("used_percentage")
        if not isinstance(pct, (int, float)):
            return
        _session_env_path("context-pct", session_id).write_text(str(int(pct)))
    except Exception:  # noqa: BLE001 — a hook must never break the statusline
        return


@hook_group.command(name="capture-nudge")
def capture_nudge() -> None:
    """One-time capture nudge when context crosses the threshold (#122).

    Runs on UserPromptSubmit (stdout reaches the model's context — documented).
    Reads the percentage ``context-watch`` recorded; at >=60% it injects, ONCE
    per session, an instruction to flush durable state to the store while
    selection quality is still good. Compaction preferentially destroys
    decisions/rationale/conventions — anything captured before it fires
    survives perfectly. Silent when unwired, below threshold, or already
    nudged; soft-fail always.
    """
    try:
        payload = _read_payload()
        session_id = _safe_session_id(payload)
        if session_id is None:
            return
        pct_file = _session_env_path("context-pct", session_id)
        if not pct_file.is_file():
            return  # statusline recorder not wired — stay silent
        pct = int(pct_file.read_text().strip() or "0")
        if pct < _CAPTURE_NUDGE_THRESHOLD_PCT:
            return
        marker = _session_env_path("capture-nudged", session_id)
        if marker.exists():
            return  # one nudge per session
        marker.touch()
    except Exception:  # noqa: BLE001 — a hook must never block a prompt
        return
    click.echo(
        f"[rekol] context is {pct}% full — compaction will preferentially drop "
        "decisions, rationale, and conventions. Run a capture pass NOW while "
        "judgment is still sharp: persist durable decisions + why (`rekol "
        "capture`), update your claimed task's next-action note (`rekol task`), "
        "then continue."
    )


@hook_group.command(name="stop-failure-record")
def stop_failure_record() -> None:
    """Record a StopFailure event to the freeze journal (#143 Phase A).

    Registered by ``rekol resume enable`` (opt-in). Captures the payload
    VERBATIM — the docs don't confirm which error type an account usage limit
    produces, so Phase A instruments everything and the first real freeze
    supplies ground truth. Hook contract: NEVER fail — any error exits 0
    silently (a recording problem must not worsen an already-failing turn).
    """
    try:
        from rekol.config import load_config
        from rekol.resume import record_stop_failure

        record_stop_failure(load_config().index_dir, _read_payload())
    except Exception:  # noqa: BLE001 — a hook must never break the harness
        return


@hook_group.command(name="update-check")
def update_check() -> None:
    """Announce a newer rekol at SessionStart, but only when it matters (#27).

    Silence is the default and it is what keeps the loud tier meaningful: an
    unmarked release says nothing at all, and a dismissed version never speaks
    again. Only `high` and `critical` reach the session.

    Same soft-fail contract as every other handler — any error prints nothing and
    exits 0. It is also throttled (24h) and hard-bounded on the network call, so
    a captive portal costs a few seconds once a day and produces no output. An
    offline machine must be SILENT, not slow.
    """
    try:
        import datetime as _dt

        from rekol import __version__
        from rekol.config import load_config
        from rekol.release import SEVERITY_CRITICAL, check_for_update, parse_version

        cfg = load_config()
        if not getattr(cfg, "update_check", True):
            return
        current = parse_version(__version__)
        if current is None:
            return
        status = check_for_update(current, cfg.index_dir, now=_dt.datetime.now())
        if not status.should_announce or status.latest is None:
            return
        if status.severity == SEVERITY_CRITICAL:
            click.echo("")
            click.echo(
                f"[rekol] ⚠ {status.latest.text} is available and marked CRITICAL "
                f"(you have {__version__}). Update: cd <rekol clone> && git pull && ./install.sh"
            )
        else:
            click.echo("")
            click.echo(
                f"[rekol] {status.latest.text} available (you have {__version__}) — "
                "`rekol update` for details, `rekol update --dismiss` to silence it"
            )
    except Exception:  # noqa: BLE001 — a hook must never break the session injection
        return
