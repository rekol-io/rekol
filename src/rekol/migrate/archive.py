"""Archive legacy memory files and leave a migration marker.

The archive lives under ``<memory_dir>/old-memory-archive/`` and holds the only
byte-complete copy of each original — including any ``MEMORY.md`` found in the
directory, which is MOVED here rather than deleted. Nothing in this module
unlinks a user file; if you are adding a path that does, it is wrong.

NO TIME-BASED DELETION PROMISE (#166). The marker used to say "safe to delete
after 1 week", and the prose here used to describe a one-week retention policy.
Both were false: migration synthesises a new frontmatter whitelist and
`_strip_frontmatter` discards the original YAML, so any UNRECOGNISED legacy key
survives only in the archived original. A user following that instruction
destroyed those values. Do not reintroduce a deletion deadline without a
metadata-preservation contract to back it.

We deliberately DO NOT write a ``MEMORY.md`` retirement pointer.  Claude Code's
built-in autoMemory feature auto-injects ``~/.claude/projects/<slug>/memory/MEMORY.md``
into every session — so a tombstone there is loaded as memory content, including
the literal string ``$MEMORY_HOME`` (which Claude cannot resolve in injected
text).  The hidden ``.MIGRATED`` marker has the same idempotency role for the
migrate tool but is not auto-injected.
"""

from __future__ import annotations

import datetime as dt
import shutil
from dataclasses import dataclass
from pathlib import Path

from rekol.migrate.discover import MIGRATION_MARKER_NAME, LegacyFile

ARCHIVE_DIR_NAME = "old-memory-archive"


@dataclass(frozen=True)
class RetirementOutcome:
    """What ``write_retirement_pointer`` actually did.

    ``retired`` is the one the caller must respect: when it is False the
    directory carries no marker, so a later run will retry it. Reporting an
    error while leaving the directory retired is the combination that strands a
    file forever, which is what QA measured on 2026-11-06.
    """

    retired: bool
    legacy_pointer_archived: Path | None
    error: str | None = None


def archive_file(lf: LegacyFile) -> Path:
    """Move ``lf.source_path`` into ``<source_root>/old-memory-archive/``.

    Returns the new path.  If the target name already exists, appends
    ``-1``, ``-2``, ... before the suffix.
    """
    archive_dir = lf.source_root / ARCHIVE_DIR_NAME
    archive_dir.mkdir(exist_ok=True)
    target = _free_archive_name(archive_dir, lf.source_path.name)
    shutil.move(str(lf.source_path), str(target))
    return target


def _free_archive_name(archive_dir: Path, name: str) -> Path:
    """A path in ``archive_dir`` that does not exist yet: ``x.md``, ``x-1.md``, and so on.

    Never returns an existing path, so archiving can never overwrite an earlier
    archived copy — the archive holds the only byte-complete original, so one
    silent overwrite is one unrecoverable file.
    """
    target = archive_dir / name
    if not target.exists():
        return target
    stem = Path(name).stem
    suffix = Path(name).suffix
    counter = 1
    while True:
        candidate = archive_dir / f"{stem}-{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def write_retirement_pointer(
    memory_dir: Path,
    *,
    memory_home: Path,
    archive_legacy_pointer: bool = True,
) -> RetirementOutcome:
    """Mark ``memory_dir`` as already migrated using a hidden ``.MIGRATED`` file.

    Idempotent: if the marker already exists, leaves it alone.

    Any ``MEMORY.md`` in ``memory_dir`` is **moved into the archive**, never
    deleted. The outcome reports the archived path, whether the directory ended
    up retired, and any error — see ``RetirementOutcome``.

    WHY THIS IS A MOVE AND NOT AN UNLINK (data loss, found by QA 2026-11-06).
    This used to call ``legacy_pointer.unlink()``. The stated purpose was to
    remove a *tombstone* written by an older version of this tool, so Claude
    Code's autoMemory would stop injecting it. But both callers sit behind the
    ``has_migration_marker`` early return in ``migrator.py``, and that check
    already treats an old-format tombstone as retired — so a real tombstone is
    skipped and never reaches here. **Every file this unlink could actually
    delete was a user's own ``MEMORY.md``**, and because ``discover_files_in_dir``
    skips that name, it was neither imported nor archived first: the content
    existed nowhere afterwards. A directory holding only ``MEMORY.md`` was
    emptied and marked migrated, exit 0.
    Moving it achieves the original goal — autoMemory cannot inject what is no
    longer at that path — without the deletion the goal never required.

    THE LEGACY POINTER IS ARCHIVED FIRST AND THE MARKER WRITTEN LAST.

    The reverse order shipped briefly and was wrong, and the argument for it was
    wrong too. It claimed the opposite order "would leave neither file on partial
    failure, causing a re-run to re-migrate already-archived content" — but
    ``discover_files_in_dir`` iterates files only and never recurses, so
    ``old-memory-archive/`` cannot be re-discovered and that failure mode does
    not exist. Meanwhile marker-first had a real failure mode, which QA measured:
    an unwritable archive dir left ``.MIGRATED`` claiming completion with
    ``MEMORY.md`` still in place, so ``has_migration_marker`` skipped the
    directory for good and the file was stranded.

    Marker-last inverts that: if archiving fails, nothing claims the directory is
    done and a later run retries. If archiving succeeds and the marker write
    fails, the content is safely in the archive, the directory is re-examined
    next run, finds no files to import, and retires then — it converges.

    ``archive_legacy_pointer=False`` leaves any ``MEMORY.md`` strictly alone
    (``migrate repo``; see the caller for why).
    """
    memory_dir.mkdir(parents=True, exist_ok=True)

    archived: Path | None = None
    legacy_pointer = memory_dir / "MEMORY.md"
    if archive_legacy_pointer and legacy_pointer.exists():
        try:
            archive_dir = memory_dir / ARCHIVE_DIR_NAME
            archive_dir.mkdir(exist_ok=True)
            target = _free_archive_name(archive_dir, legacy_pointer.name)
            shutil.move(str(legacy_pointer), str(target))
            archived = target
        except OSError as exc:
            # Caught, not raised: an unwritable archive dir used to surface as a
            # traceback. Returning instead keeps the message readable AND leaves
            # the directory unretired, so the work is retried rather than lost.
            return RetirementOutcome(
                retired=False,
                legacy_pointer_archived=None,
                error=(
                    f"could not archive {legacy_pointer}: {exc}. "
                    f"{memory_dir} was NOT retired and MEMORY.md is untouched, "
                    f"so re-running retries it once the archive is writable."
                ),
            )

    marker = memory_dir / MIGRATION_MARKER_NAME
    if not marker.exists():
        today = dt.date.today().isoformat()
        try:
            marker.write_text(
                f"migrated to {memory_home} on {today}\n"
                f"Files discovered here are in {ARCHIVE_DIR_NAME}/ — KEEP them unless you have\n"
                f"checked that the migrated copies contain everything you need.\n"
                f"Migration rewrites frontmatter: recognised fields are regenerated and any\n"
                f"UNRECOGNISED key is dropped, so those values survive only in the archive.\n"
            )
        except OSError as exc:
            return RetirementOutcome(
                retired=False,
                legacy_pointer_archived=archived,
                error=(
                    f"could not write {marker}: {exc}. {memory_dir} was NOT "
                    f"retired, so re-running retries it."
                ),
            )

    return RetirementOutcome(retired=True, legacy_pointer_archived=archived)
