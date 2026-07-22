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
import time
from pathlib import Path

import click

# Claude Code session ids are UUID-like; restrict to a safe charset before
# using one in a filesystem path (prevents traversal from a malformed payload).
_SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")


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
