"""``rekol task``: the cross-session task layer's CLI (#113).

Thin wrappers over :mod:`rekol.tasks` — all write paths go through the store's
CAS update so concurrent sessions never clobber each other. ``start`` is the
claim operation whose ``in_progress`` + ``session_id`` state #143's auto-resume
watchdog reads as its intent semaphore.
"""

from __future__ import annotations

from dataclasses import replace

import click

from rekol.config import load_config
from rekol.tasks import Task, TaskError, create_task, list_tasks, slugify, update_task

_STATUS_GLYPH = {"open": "○", "in_progress": "◐", "blocked": "⊘", "done": "●"}


@click.group(name="task")
def main() -> None:
    """Durable cross-session tasks in $REKOL_HOME/tasks/ (one file per task)."""


@main.command(name="add")
@click.argument("title")
@click.option("--link", "links", multiple=True, help="Related issue/PR/note (repeatable).")
@click.option("--role", default="", help="Owning role (dev/product/qa/...); empty = unclaimed.")
@click.option("--note", default="", help="Body text: next action / context for the next session.")
def add(title: str, links: tuple[str, ...], role: str, note: str) -> None:
    """Create a new open task."""
    cfg = load_config()
    task = Task(id=slugify(title), title=title, owner_role=role, links=list(links), body=note)
    path = create_task(cfg.memory_home, task)
    click.echo(f"added {path.stem} ({path})")


@main.command(name="start")
@click.argument("task_id")
@click.option("--session", default="", help="Claiming session id (the #143 resume semaphore).")
@click.option("--role", default="", help="Claiming role; kept if omitted.")
def start(task_id: str, session: str, role: str) -> None:
    """Claim a task: status=in_progress (+ session_id when given)."""
    cfg = load_config()
    try:
        task = update_task(
            cfg.memory_home,
            task_id,
            lambda t: replace(
                t,
                status="in_progress",
                session_id=session or t.session_id,
                owner_role=role or t.owner_role,
            ),
        )
    except TaskError as exc:
        raise SystemExit(f"error: {exc}") from exc
    suffix = f" (session {task.session_id})" if task.session_id else ""
    click.echo(f"{task.id}: in_progress{suffix}")


@main.command(name="done")
@click.argument("task_id")
def done(task_id: str) -> None:
    """Complete a task (clears the claim: done tasks never look resumable)."""
    cfg = load_config()
    try:
        task = update_task(
            cfg.memory_home, task_id, lambda t: replace(t, status="done", session_id="")
        )
    except TaskError as exc:
        raise SystemExit(f"error: {exc}") from exc
    click.echo(f"{task.id}: {task.status}")


@main.command(name="block")
@click.argument("task_id")
@click.option("--reason", required=True, help="Why it's parked (appended to the body).")
def block(task_id: str, reason: str) -> None:
    """Park a task with a reason."""
    cfg = load_config()
    try:
        task = update_task(
            cfg.memory_home,
            task_id,
            lambda t: replace(
                t, status="blocked", body=(t.body.rstrip() + f"\n\nBlocked: {reason}").strip()
            ),
        )
    except TaskError as exc:
        raise SystemExit(f"error: {exc}") from exc
    click.echo(f"{task.id}: blocked")


@main.command(name="list")
@click.option(
    "--status",
    "statuses",
    multiple=True,
    type=click.Choice(["open", "in_progress", "blocked", "done"]),
    help="Filter to these statuses (repeatable).",
)
@click.option("--all", "show_all", is_flag=True, help="Include done/blocked tasks.")
def list_cmd(statuses: tuple[str, ...], show_all: bool) -> None:
    """List tasks (open + in_progress by default)."""
    cfg = load_config()
    wanted = set(statuses) if statuses else (None if show_all else {"open", "in_progress"})
    shown = 0
    for task in list_tasks(cfg.memory_home):
        if wanted is not None and task.status not in wanted:
            continue
        claim = f"  [{task.owner_role or 'unclaimed'}]"
        session = f" session={task.session_id}" if task.session_id else ""
        click.echo(f"{_STATUS_GLYPH[task.status]} {task.id}: {task.title}{claim}{session}")
        shown += 1
    if shown == 0:
        click.echo("no matching tasks")
