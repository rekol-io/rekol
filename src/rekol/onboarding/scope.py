"""Scope-control wiring for ``rekol init`` → ``rekol bootstrap`` (T1, #39).

T3 (#41) defined the canonical scope knobs on ``rekol bootstrap``:
``--scope-project`` (repeatable), ``--scope-days``, ``--all-time``,
``--max-sessions``, with a bounded conservative default (recent window, capped
sessions, all projects). T1 reuses that *exact* shape rather than inventing a
parallel one — the init flow merely collects the same knobs at the bootstrap
step and threads them straight through.

This builder is pure so the wiring is unit-testable without running a real
bootstrap: it turns the collected knob values into the argv ``rekol init`` hands
to the in-process ``rekol bootstrap`` invocation. Omitting a knob means "use
T3's default", so a bare call yields ``["bootstrap"]`` and the bounded default
applies untouched.
"""

from __future__ import annotations


def bootstrap_argv(
    *,
    all_time: bool = False,
    scope_days: int | None = None,
    scope_projects: tuple[str, ...] = (),
    max_sessions: int | None = None,
) -> list[str]:
    """Build the ``rekol bootstrap`` argv from the shared scope knobs.

    Mirrors T3's flag names exactly. ``--all-time`` widens the recency window to
    the whole corpus and *overrides* ``--scope-days`` (T3 does the same at the
    flag level), so when ``all_time`` is set we omit ``--scope-days`` to avoid
    sending a contradictory pair. A ``None``/empty knob is left off entirely so
    T3's bounded default governs it.
    """
    argv: list[str] = ["bootstrap"]
    if all_time:
        argv.append("--all-time")
    elif scope_days is not None:
        argv += ["--scope-days", str(scope_days)]
    for project in scope_projects:
        argv += ["--scope-project", project]
    if max_sessions is not None:
        argv += ["--max-sessions", str(max_sessions)]
    return argv
