"""Opt-in auto-resume across usage-limit freezes (#143, Phase A).

When an Anthropic usage limit freezes a Claude Code session mid-task, the
session cannot rescue itself — it has no tokens to detect the reset or
continue. The resumer must live OUTSIDE the frozen session. This module is
that machinery:

* **Detection (push, instrumented):** a Claude Code ``StopFailure`` hook
  (``rekol _hook stop-failure-record``) appends each API-error turn-end to a
  local **freeze journal**. The docs do not confirm which error type an
  *account* usage limit produces (vs an API 429) — so Phase A records EVERY
  StopFailure payload verbatim as instrumentation; the first real freeze gives
  ground truth and Phase B tightens the filter.
* **Intent (the semaphore):** a frozen session is only worth resuming if it
  was mid-task. That is exactly the #113 task layer's claim state: an
  ``in_progress`` task whose ``session_id`` matches the frozen session. No
  claim → the session was idle or interactive → never auto-resume it.
* **Action:** at reset time (parsed from the limit message when present, else
  a conservative fallback delay), relaunch the session headlessly —
  ``claude -p --resume <session-id> <nudge>`` appends a turn to the SAME
  session transcript — detached, so the periodic tick never blocks on a long
  turn.

Safety posture (product constraints, #143): the whole feature is **opt-in and
OFF by default**; at most ONE resume per tick (a burst of resumes at reset
would instantly re-hit the limit); an idempotency **ledger** guarantees one
resume per (session, freeze) even across overlapping ticks or a manual resume
race. Journals/ledgers live in the local cache dir — never in the synced tree.
"""

from __future__ import annotations

import datetime as _dt
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

FREEZE_JOURNAL_NAME = "freeze-journal.jsonl"
RESUME_LEDGER_NAME = "resume-ledger.jsonl"
RESUME_LOG_NAME = "resume-launches.log"
RESUME_ENABLED_NAME = "resume-enabled"

# Error types we treat as limit-shaped. Docs are explicit that StopFailure
# matchers include rate_limit and billing_error but silent on which one an
# ACCOUNT usage limit produces — the journal records everything either way, so
# a real freeze can only widen (never lose) evidence for Phase B.
LIMIT_ERROR_TYPES = frozenset({"rate_limit", "billing_error", "usage_limit"})

# When the limit message carries no parseable "resets HH:MM", wait this long
# after the freeze before attempting a resume. Deliberately conservative: a
# too-early resume burns a request while still frozen; too late merely delays.
FALLBACK_DELAY_MINUTES = 60

# Ignore journal entries older than this — a stale freeze from days ago must
# never trigger a surprise resume long after the user moved on.
MAX_ENTRY_AGE_DAYS = 2

_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_RESET_RE = re.compile(
    r"resets\s+(?:(mon|tue|wed|thu|fri|sat|sun)[a-z]*\s+)?(\d{1,2}):(\d{2})\s*(am|pm)?",
    re.IGNORECASE,
)

_RESUME_NUDGE = (
    "A usage limit interrupted this session and has now reset. "
    "Run `rekol task list` to see your in_progress task and continue it from "
    "where you left off; mark it done with `rekol task done <id>` when finished."
)


@dataclass
class ResumeAction:
    """One decided (or dry-run) resume: which session, why, and when it unfroze."""

    session_id: str
    task_id: str
    entry_ts: str
    resume_at: _dt.datetime
    launched: bool


def freeze_journal_path(index_dir: Path) -> Path:
    """Where the StopFailure instrumentation journal lives (local cache only)."""
    return index_dir / FREEZE_JOURNAL_NAME


def ledger_path(index_dir: Path) -> Path:
    """Where the resumed-(session,freeze) idempotency ledger lives."""
    return index_dir / RESUME_LEDGER_NAME


def enabled_marker_path(index_dir: Path) -> Path:
    """Where the explicit opt-in marker lives (written by ``enable``)."""
    return index_dir / RESUME_ENABLED_NAME


def is_enabled(index_dir: Path) -> bool:
    """True when the user has opted in and not since opted out.

    The kill-switch ``tick`` consults. It is an explicit marker rather than "is
    the hook registered in settings.json?" for two reasons: the settings path is
    the exact surface that already shipped one silent-lie bug, and on Linux
    there is no plist to check either — so the marker is the only signal that
    means the same thing on both platforms.
    """
    return enabled_marker_path(index_dir).exists()


def record_stop_failure(index_dir: Path, payload: dict) -> None:
    """Append one StopFailure payload to the freeze journal (instrumentation).

    Stores the payload VERBATIM plus a timestamp: until a real account-limit
    freeze confirms which fields/types Claude Code emits, dropping anything
    would risk discarding the one piece of evidence Phase B needs.
    """
    index_dir.mkdir(parents=True, exist_ok=True)
    entry = {"ts": _dt.datetime.now().isoformat(timespec="seconds"), "payload": payload}
    with freeze_journal_path(index_dir).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry) + "\n")


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    entries = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # a torn write must not kill the tick
    return entries


def parse_reset_time(message: str, base: _dt.datetime) -> _dt.datetime | None:
    """Extract "resets 3:45pm" / "resets Mon 12:00am" as the next matching time after *base*.

    Returns ``None`` when the message carries no parseable reset — the caller
    falls back to a fixed delay. Times are local (the TUI prints local time).
    """
    match = _RESET_RE.search(message or "")
    if not match:
        return None
    weekday_raw, hour_raw, minute_raw, meridiem = match.groups()
    hour, minute = int(hour_raw), int(minute_raw)
    if meridiem:
        meridiem = meridiem.lower()
        if meridiem == "pm" and hour != 12:
            hour += 12
        if meridiem == "am" and hour == 12:
            hour = 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    candidate = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if weekday_raw:
        target = _WEEKDAYS.index(weekday_raw.lower()[:3])
        days_ahead = (target - candidate.weekday()) % 7
        candidate += _dt.timedelta(days=days_ahead)
    if candidate <= base:
        candidate += _dt.timedelta(days=7 if weekday_raw else 1)
    return candidate


def _entry_fields(entry: dict) -> tuple[str, str, str]:
    """Best-effort (session_id, error_type, message) from a journal entry.

    Field names are defensive: the StopFailure payload schema is documented
    loosely, and instrumentation must degrade rather than crash.
    """
    payload = entry.get("payload") or {}
    session_id = str(payload.get("session_id") or payload.get("sessionId") or "")
    error_type = str(payload.get("error_type") or payload.get("errorType") or "").lower()
    message = str(payload.get("message") or payload.get("error") or "")
    return session_id, error_type, message


def _launch_detached(session_id: str, log_path: Path) -> bool:
    """Fire `claude -p --resume` fully detached; the tick never waits on the turn."""
    claude = shutil.which("claude")
    if claude is None:
        return False
    with log_path.open("a", encoding="utf-8") as log:
        subprocess.Popen(  # noqa: S603 — fixed argv, no shell
            [claude, "-p", "--resume", session_id, _RESUME_NUDGE],
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
            start_new_session=True,  # survives the tick process exiting
        )
    return True


def tick(
    index_dir: Path,
    memory_home: Path,
    *,
    now: _dt.datetime | None = None,
    dry_run: bool = False,
    launcher=None,
) -> list[ResumeAction]:
    """One watchdog pass: decide (and unless dry-run, launch) at most ONE resume.

    Resume iff ALL of: a journal entry is limit-shaped (`LIMIT_ERROR_TYPES`),
    its reset time (parsed, else freeze+`FALLBACK_DELAY_MINUTES`) has passed,
    the #113 task layer holds an ``in_progress`` task claimed by that session
    (the intent semaphore), and the (session, freeze) pair is not already in
    the ledger. The ledger is written BEFORE the launch settles so overlapping
    ticks can't double-fire.
    """
    from rekol.tasks import list_tasks

    now = now or _dt.datetime.now()
    launcher = launcher or _launch_detached
    if not is_enabled(index_dir):
        # Kill-switch. `disable` removes the marker, so a tick the user scheduled
        # themselves (the recommended Linux workflow: `enable --no-launchd` +
        # cron) stops resuming immediately instead of running on forever off a
        # leftover journal. Checked FIRST so a disabled install does no work.
        return []
    entries = _read_jsonl(freeze_journal_path(index_dir))
    ledger_entries = _read_jsonl(ledger_path(index_dir))
    already = {(e.get("session_id"), e.get("entry_ts")) for e in ledger_entries}

    claimed = {
        t.session_id: t
        for t in list_tasks(memory_home)
        if t.status == "in_progress" and t.session_id
    }

    actions: list[ResumeAction] = []
    for entry in reversed(entries):  # newest freeze first
        session_id, error_type, message = _entry_fields(entry)
        entry_ts = str(entry.get("ts") or "")
        if not session_id or error_type not in LIMIT_ERROR_TYPES:
            continue
        if (session_id, entry_ts) in already:
            continue
        task = claimed.get(session_id)
        if task is None:
            continue  # no in_progress claim → idle/interactive → never auto-resume
        try:
            frozen_at = _dt.datetime.fromisoformat(entry_ts)
        except ValueError:
            continue
        if (now - frozen_at) > _dt.timedelta(days=MAX_ENTRY_AGE_DAYS):
            continue
        resume_at = parse_reset_time(message, frozen_at) or (
            frozen_at + _dt.timedelta(minutes=FALLBACK_DELAY_MINUTES)
        )
        if now < resume_at:
            continue

        launched = False
        if not dry_run:
            # Ledger-first: a crash after this line means a skipped resume (safe,
            # user-visible via status) — never a double-fire.
            with ledger_path(index_dir).open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "session_id": session_id,
                            "entry_ts": entry_ts,
                            "task_id": task.id,
                            "resumed_at": now.isoformat(timespec="seconds"),
                        }
                    )
                    + "\n"
                )
            launched = launcher(session_id, index_dir / RESUME_LOG_NAME)
        actions.append(
            ResumeAction(
                session_id=session_id,
                task_id=task.id,
                entry_ts=entry_ts,
                resume_at=resume_at,
                launched=launched,
            )
        )
        break  # concurrency cap: at most one resume per tick
    return actions
