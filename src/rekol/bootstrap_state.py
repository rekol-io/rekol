"""Checkpoint/state for the resumable memory-bootstrap routine (T3, #41).

The bootstrap is a long, LLM-driven precision pass over a potentially large
session corpus, run by the user's own Claude. It WILL get interrupted — a
SessionEnd reap, a ``^C``, a crash mid-batch. The hard acceptance criterion is
that an interrupted run *resumes without reprocessing* the batches it already
finished.

This module owns the tiny JSON checkpoint that makes that possible. It records
the batch plan (deterministic, ordered), which batches have finished, the scope
params the plan was built from (so a resume can refuse to mix a different
scope's batches into the same run), and a running candidate count. It is
deliberately metadata-only — no verbatim transcript content lives here — so the
checkpoint can sit next to the candidate artifacts it tracks in the local-only
``pending-review/`` cache dir (#57) without becoming a secrets sink.

Writes are atomic (temp file + ``os.replace``) so a process killed mid-write can
never leave a half-written checkpoint that silently loses prior progress; the
next run reads either the old complete state or the new complete state, never a
torn one.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

# The checkpoint filename, kept under pending-review/ alongside the per-batch
# candidate files. Leading dot so a `ls pending-review/` of the human-facing
# review files isn't cluttered by the machine checkpoint.
STATE_FILENAME = ".bootstrap-state.json"

# Bump when the on-disk JSON shape changes incompatibly; load_state can then
# refuse an unreadable old checkpoint loudly instead of mis-parsing it.
STATE_SCHEMA_VERSION = 1


class BootstrapStateError(ValueError):
    """Raised when a checkpoint on disk is corrupt or has an unreadable schema.

    Subclasses :class:`ValueError` so callers that already guard ``ValueError``
    (the CLI surface) catch it, while the distinct type lets the resume path
    tell "no prior run" (``None``) apart from "prior run, but its checkpoint is
    broken" (this error) — the latter must never be swallowed as "start fresh",
    which would silently reprocess everything.
    """


def state_path_for(pending_review_dir: Path) -> Path:
    """Return the checkpoint path inside a given pending-review queue dir.

    Colocated with the per-batch candidate files in ``pending_review_dir`` so the
    resume artifacts are self-contained: deleting that directory cleanly discards
    both the candidates and the checkpoint together. The queue lives in the
    local-only cache, not ``$REKOL_HOME`` (#57) — see
    :attr:`rekol.config.Config.pending_review_dir`.

    Args:
        pending_review_dir: The pending-review queue directory (the cache-local
            ``<cache>/pending-review``).

    Returns:
        Path to ``<pending_review_dir>/.bootstrap-state.json`` (not created here;
        :func:`save_state` makes the parent on first write).
    """
    return pending_review_dir / STATE_FILENAME


@dataclass
class BootstrapState:
    """Resume checkpoint for one bootstrap run.

    ``batches`` is the full ordered plan; ``completed`` is the subset that has
    finished (capture + REKOL.md update done by the skill). ``pending_batches``
    is what a resume still has to do, in plan order. ``scope`` is the resolved
    scope-control params the plan was built from, so :meth:`scope_matches` can
    refuse to resume a run whose scope was widened/narrowed underneath it.
    """

    run_id: str
    embedding_model: str
    scope: dict
    batches: list[str]
    completed: list[str] = field(default_factory=list)
    created_iso: str = ""
    updated_iso: str = ""
    candidates_written: int = 0

    def mark_batch_done(self, batch_id: str) -> None:
        """Record ``batch_id`` as finished. Idempotent; rejects unknown ids.

        Idempotent so a retried/duplicated "done" (e.g. a resume that re-runs the
        last batch before its checkpoint landed) does not double-count. An id not
        in :attr:`batches` is a programming error — surfaced loudly rather than
        silently appended, which would corrupt :meth:`is_complete`.
        """
        if batch_id not in self.batches:
            raise ValueError(f"batch {batch_id!r} is not in the batch plan")
        if batch_id not in self.completed:
            self.completed.append(batch_id)

    def pending_batches(self) -> list[str]:
        """Batches still to do, in the original plan order.

        Plan order (not ``completed`` order) so a resume processes the corpus in
        the same deterministic sequence every time.
        """
        done = set(self.completed)
        return [b for b in self.batches if b not in done]

    def is_complete(self) -> bool:
        """True when every planned batch has finished."""
        return not self.pending_batches()

    def scope_matches(self, scope: dict) -> bool:
        """True when ``scope`` equals the scope this run's plan was built from.

        A resume must not silently fold a differently-scoped invocation into an
        existing run — that would leave the batch plan and the actual corpus out
        of sync. The CLI uses this to demand an explicit reset on scope change.
        """
        return self.scope == scope


def save_state(state: BootstrapState, path: Path) -> None:
    """Atomically write ``state`` to ``path`` as JSON.

    Writes to a sibling temp file then ``os.replace``s it over the target, so a
    crash mid-write leaves either the previous complete checkpoint or the new
    complete one — never a torn file that would mis-resume. Creates the parent
    ``pending-review/`` directory if absent.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": STATE_SCHEMA_VERSION, **asdict(state)}
    tmp = path.with_name(path.name + ".tmp")
    # Write + flush + fsync the temp file before the rename so the bytes are on
    # disk when the (atomic) rename publishes them; otherwise a power loss could
    # rename an empty/partial inode into place.
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def load_state(path: Path) -> BootstrapState | None:
    """Load a checkpoint, or ``None`` if no prior run exists.

    Returns ``None`` only for a genuinely-absent file (no prior run). A present
    but unreadable file (truncated JSON, wrong schema) raises
    :class:`BootstrapStateError` so the resume path treats "broken checkpoint"
    as a hard stop, not as "start fresh" — silently restarting would reprocess
    the whole corpus, defeating the resumability AC.

    Args:
        path: The checkpoint path (see :func:`state_path_for`).

    Returns:
        The loaded :class:`BootstrapState`, or ``None`` when ``path`` is absent.

    Raises:
        BootstrapStateError: When the file exists but is corrupt or carries an
            unrecognised schema version.
    """
    if not path.exists():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise BootstrapStateError(
            f"corrupt bootstrap checkpoint at {path}: {exc}. "
            f"Delete it (or pass --reset) to start a fresh run."
        ) from exc
    version = raw.get("schema_version")
    if version != STATE_SCHEMA_VERSION:
        raise BootstrapStateError(
            f"bootstrap checkpoint at {path} has unsupported schema_version "
            f"{version!r} (expected {STATE_SCHEMA_VERSION}). Pass --reset to start fresh."
        )
    return BootstrapState(
        run_id=str(raw["run_id"]),
        embedding_model=str(raw["embedding_model"]),
        scope=dict(raw["scope"]),
        batches=list(raw["batches"]),
        completed=list(raw.get("completed", [])),
        created_iso=str(raw.get("created_iso", "")),
        updated_iso=str(raw.get("updated_iso", "")),
        candidates_written=int(raw.get("candidates_written", 0)),
    )
