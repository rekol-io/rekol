"""Hidden Claude Code hook subcommands: time-context, record-stop, nudge.

Soft-fail by design — any error degrades and exits 0 so a hook problem never
blocks a prompt or session start. Per-session time state lives at
``~/.claude/session-env/time-context-<session_id>.json``; the one-time
SessionStart ingest nudge marker lives under ``$REKOL_HOME/.index/``.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import time
from pathlib import Path

import click

from rekol.onboarding import count_claude_transcripts, count_curated_memory_files

# Claude Code session ids are UUID-like; restrict to a safe charset before
# using one in a filesystem path (prevents traversal from a malformed payload).
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")

# A store with at most this many curated markdown files counts as "near-empty"
# for the SessionStart ingest nudge. A fresh install seeds the template's couple
# of starter layer files, so a strict zero would never fire; this small slack
# lets the nudge reach users who have an essentially blank store but suppresses
# it once they have accumulated real memories.
_NEAR_EMPTY_MAX_FILES = 3

# Marker filename written under $REKOL_HOME/.index/ after the nudge fires once.
# .index/ is machine-local, excluded from sync (.dropboxignore) and from the
# curated-emptiness count, and is not walked by the markdown indexer — so the
# marker never pollutes curated memory, gets indexed, or syncs across machines.
_NUDGE_MARKER_NAME = ".session-start-nudge-shown"


def _state_path(session_id: str) -> Path:
    """Return (and ensure the dir of) the per-session state file path."""
    directory = Path.home() / ".claude" / "session-env"
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass  # soft-fail: a later read/write degrades gracefully and exits 0
    return directory / f"time-context-{session_id}.json"


def _read_payload() -> dict:
    """Read and parse the hook JSON payload from stdin; return {} on any error."""
    try:
        raw = click.get_text_stream("stdin").read()
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


@hook_group.command(name="session-start-nudge")
def session_start_nudge() -> None:
    """SessionStart hook: one-time offer to ingest history into an empty store.

    Fires at most once. Emits a context line telling the assistant to OFFER to
    index past Claude Code sessions and import notes, but only when (a) REKOL is
    configured, (b) the curated store is near-empty, (c) past transcripts exist,
    and (d) the nudge has not already fired. Soft-fails: any error exits 0 and
    prints nothing, so a hook problem never blocks session start.
    """
    # Read the payload to mirror the other hooks' stdin contract, even though the
    # nudge does not key off the session id (the marker is store-global).
    _read_payload()
    try:
        from rekol.config import load_config

        try:
            cfg = load_config()
        except RuntimeError:
            return  # REKOL not configured → soft no-op

        marker = cfg.memory_home / ".index" / _NUDGE_MARKER_NAME
        if marker.exists():
            return  # already offered → no-op

        if count_curated_memory_files(cfg.memory_home) > _NEAR_EMPTY_MAX_FILES:
            return  # store has real content → nothing to nudge about

        n_transcripts = count_claude_transcripts(cfg.claude_projects_dir)
        if n_transcripts <= 0:
            return  # no history to offer → no-op

        click.echo(
            f"[rekol] Your memory store looks empty, but {n_transcripts} past "
            "Claude Code sessions exist on this machine. Offer to bootstrap the "
            "user's memory: run `rekol session-index --incremental` to make their "
            "past sessions searchable, and `rekol import <dir>` to pull in an "
            "existing notes/docs folder (e.g. an Obsidian vault). Ask first; do "
            "not run anything without confirmation."
        )

        # Write the marker only AFTER a successful emit so a crash mid-emit does
        # not silently consume the one-time nudge.
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("", encoding="utf-8")
    except Exception:
        # Soft-fail: never let a nudge error break session start.
        return
