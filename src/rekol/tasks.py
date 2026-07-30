"""Cross-session task layer (#113): per-task markdown files with CAS writes.

Tasks live one-per-file in ``$REKOL_HOME/tasks/`` — a fully SHARED store (every
session reads all tasks; any session may claim or complete one). One file per
task is a *write-granularity* choice, not a visibility one: concurrent sessions
updating different tasks touch different files, so a sync provider never has to
merge concurrent writes to a single file (see ``docs/task-layer.md``).

Two conflict classes, two mechanisms:

* **Same-machine races** — every update goes through :func:`update_task`'s
  optimistic-concurrency (CAS) loop: hash the file on read, build the new
  content, re-hash just before writing, and only write (temp-file +
  ``os.replace``, the repo's atomic-write idiom) if the file is unchanged;
  otherwise re-read and retry, bounded. Lock-free — nothing leaks if a process
  dies mid-update. The compare→replace window is microseconds and per-task
  files make same-file races rare to begin with.
* **Cross-machine races via sync** — client-side CAS cannot see another
  machine's unsynced write, so the mitigation is the storage shape itself
  (per-task files shrink the collision surface); a residual same-task fork
  surfaces as a sync-provider conflicted copy.

Deliberately NOT in the semantic index: ``tasks/`` sits outside the indexed
layer dirs, so operational task state never pollutes ``rekol search``.

Downstream: #143's auto-resume watchdog reads ``status: in_progress`` +
``session_id`` as its "does this session need continuing?" semaphore — those
fields are the contract this module must keep stable.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import os
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

import frontmatter

TASKS_DIR_NAME = "tasks"
TASK_STATUSES = ("open", "in_progress", "blocked", "done")

# Bounded CAS retries: a same-machine race re-reads and retries; if the file is
# being rewritten this many times in a row something is genuinely wrong and we
# surface it instead of spinning.
_CAS_MAX_ATTEMPTS = 3

_SLUG_RE = re.compile(r"[^a-z0-9]+")


class TaskError(ValueError):
    """Raised for invalid task files, unknown ids, or a lost CAS race."""


@dataclass
class Task:
    """One task file's parsed content.

    ``session_id`` + ``status == "in_progress"`` is the #143 claim semaphore:
    empty ``session_id`` means the task is shared/unclaimed.
    """

    id: str
    title: str
    status: str = "open"
    owner_role: str = ""
    session_id: str = ""
    created: str | None = None
    updated: str | None = None
    links: list[str] = field(default_factory=list)
    body: str = ""


def tasks_dir(memory_home: Path) -> Path:
    """The shared per-task-file store directory under the memory home."""
    return memory_home / TASKS_DIR_NAME


def slugify(title: str) -> str:
    """Derive a filesystem-safe task id from a title (lowercase, dash-joined)."""
    slug = _SLUG_RE.sub("-", title.lower()).strip("-")
    return slug[:60] or "task"


def _today() -> str:
    return _dt.date.today().isoformat()


def _serialize(task: Task) -> str:
    """Render a task to markdown with stable field ordering.

    Hand-rolled (not yaml.dump) so field order is deterministic — stable output
    keeps diffs/hashes meaningful and CAS comparisons honest.
    """
    links = "[" + ", ".join(f'"{link}"' for link in task.links) + "]"
    lines = [
        "---",
        f"id: {task.id}",
        f"title: {task.title}",
        f"status: {task.status}",
        f'owner_role: "{task.owner_role}"',
        f'session_id: "{task.session_id}"',
        f"created: {task.created or _today()}",
        f"updated: {task.updated or _today()}",
        f"links: {links}",
        "---",
        "",
    ]
    body = task.body.strip()
    return "\n".join(lines) + (body + "\n" if body else "")


def parse_task(path: Path) -> Task:
    """Parse one task file; raise :class:`TaskError` on a malformed one."""
    try:
        post = frontmatter.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise TaskError(f"{path}: could not parse task frontmatter: {exc}") from exc
    meta = post.metadata
    if not meta or "title" not in meta or meta["title"] in (None, ""):
        raise TaskError(f"{path}: missing required task field 'title'")
    status = str(meta.get("status", "open"))
    if status not in TASK_STATUSES:
        raise TaskError(f"{path}: invalid status {status!r} (allowed: {list(TASK_STATUSES)})")
    links_raw = meta.get("links") or []
    if not isinstance(links_raw, list):
        raise TaskError(f"{path}: field 'links' must be a list")
    return Task(
        id=str(meta.get("id") or path.stem),
        title=str(meta["title"]),
        status=status,
        owner_role=str(meta.get("owner_role") or ""),
        session_id=str(meta.get("session_id") or ""),
        created=str(meta["created"]) if meta.get("created") else None,
        updated=str(meta["updated"]) if meta.get("updated") else None,
        links=[str(link) for link in links_raw],
        body=post.content,
    )


def list_tasks(memory_home: Path) -> list[Task]:
    """All parseable tasks, oldest-created first (stable for injection/display).

    Malformed files are skipped here (a broken task must not take down the
    SessionStart injection or ``task list``); they still surface loudly when
    addressed directly by id.
    """
    directory = tasks_dir(memory_home)
    if not directory.is_dir():
        return []
    tasks: list[Task] = []
    for path in sorted(directory.glob("*.md")):
        if path.name.startswith(".") or path.name.lower().startswith("readme"):
            continue
        try:
            tasks.append(parse_task(path))
        except TaskError:
            continue
    tasks.sort(key=lambda t: (t.created or "", t.id))
    return tasks


def _task_path(memory_home: Path, task_id: str) -> Path:
    # Reject separators so a crafted id can't escape the tasks dir.
    if "/" in task_id or "\\" in task_id or task_id.startswith("."):
        raise TaskError(f"invalid task id: {task_id!r}")
    return tasks_dir(memory_home) / f"{task_id}.md"


def create_task(memory_home: Path, task: Task) -> Path:
    """Create a new task file; O_EXCL so an id collision fails loudly.

    Collision handling appends ``-2``, ``-3``… — two sessions adding
    similarly-titled tasks both succeed with distinct files (never clobber).
    """
    directory = tasks_dir(memory_home)
    directory.mkdir(parents=True, exist_ok=True)
    base = task.id
    for attempt in range(1, 100):
        candidate = base if attempt == 1 else f"{base}-{attempt}"
        path = _task_path(memory_home, candidate)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            continue
        stamped = replace(task, id=candidate, created=task.created or _today(), updated=_today())
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(_serialize(stamped))
        return path
    raise TaskError(f"could not allocate an id for task {base!r}")


def update_task(memory_home: Path, task_id: str, mutate: Callable[[Task], Task]) -> Task:
    """Read-modify-write one task under optimistic concurrency (CAS).

    ``mutate`` receives the freshly-parsed task and returns the new version; it
    may be invoked more than once (once per retry), so it must be pure. On each
    attempt: hash the raw bytes read → apply ``mutate`` → re-hash the file; if
    unchanged, atomically replace (temp file in the same dir + ``os.replace``);
    if another writer landed in between, re-read and retry, bounded by
    ``_CAS_MAX_ATTEMPTS`` — losing every round means persistent contention,
    which we surface rather than silently overwrite.
    """
    path = _task_path(memory_home, task_id)
    if not path.is_file():
        raise TaskError(f"no such task: {task_id!r} (looked for {path})")

    for _ in range(_CAS_MAX_ATTEMPTS):
        raw = path.read_bytes()
        before = hashlib.sha256(raw).hexdigest()
        current = parse_task(path)
        updated = replace(mutate(current), updated=_today())
        new_content = _serialize(updated)

        if hashlib.sha256(path.read_bytes()).hexdigest() != before:
            continue  # lost the race — re-read and retry

        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(new_content)
            os.replace(tmp_name, path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
        return updated

    raise TaskError(f"task {task_id!r}: concurrent writes kept winning the CAS race; retry")
