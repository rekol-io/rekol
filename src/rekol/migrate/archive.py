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
import errno
import os
import shutil
import tempfile
from collections.abc import Iterator
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


class ArchivePublishError(Exception):
    """Publishing into the archive failed and the source was left untouched."""


# Bounded so a pathological archive dir cannot spin forever; far above any real
# number of same-named originals in one directory.
MAX_ARCHIVE_NAME_ATTEMPTS = 1000


def archive_file(lf: LegacyFile) -> Path:
    """Publish ``lf.source_path`` into ``<source_root>/old-memory-archive/``.

    Returns the new path. If the target name is taken, tries ``-1``, ``-2``, …
    Raises ``ArchivePublishError`` without touching the source if it cannot
    publish a complete copy.
    """
    return publish_into_archive(lf.source_path, lf.source_root / ARCHIVE_DIR_NAME)


def publish_into_archive(source_path: Path, archive_dir: Path) -> Path:
    """Move ``source_path`` into ``archive_dir``, never overwriting anything.

    WHY THIS IS NOT ``shutil.move`` TO A CHECKED NAME (Codex, 2026-11-06).
    The previous version picked a name with ``exists()`` and then called
    ``shutil.move``. Three defects, all of which could destroy the only
    byte-complete copy of a user's file:

    1. **The check was not a reservation.** A file created at that name between
       the check and the move was silently overwritten, because ``os.rename``
       replaces its destination. A deterministic probe lost an earlier archived
       original this way. Sequential suffix tests cannot see it.
    2. **``exists()`` is False for a dangling symlink**, so such a name read as
       free and the move clobbered the link.
    3. **Across filesystems ``shutil.move`` becomes copy-then-unlink**, and a
       failed copy can leave partial debris at the destination — which the next
       run's ``exists()`` would then treat as an occupied, complete archive copy.

    So the publication is staged instead: copy into a unique temporary file in
    the archive dir, fsync it, then claim the final name with ``os.link``, which
    is atomic and **fails** if the name is taken — including by a dangling
    symlink, where ``O_EXCL`` semantics apply to the name and not to what it
    points at. The final name therefore only ever appears as a complete file,
    and partial debris can only ever sit at a ``.partial-`` temporary, which is
    cleaned up here and never collides with a real archive name.

    The source is unlinked only after the final name exists, and with
    ``os.unlink``, which removes a symlink itself rather than its target.

    SYMLINK SOURCES are followed and archived as real content, which is the
    choice that makes "kept byte-complete in the archive" literally true — the
    alternative, moving the link, re-resolves a relative target against the
    archive directory and breaks it (Codex's P2). The link's target file is left
    exactly where it is; only the link is removed, which is all that is needed
    to stop autoMemory injecting it. A **dangling** source symlink has no
    content to preserve, so it is refused and nothing is touched.
    """
    archive_dir.mkdir(parents=True, exist_ok=True)

    try:
        source_size = source_path.stat().st_size  # follows symlinks on purpose
    except OSError as exc:
        raise ArchivePublishError(
            f"cannot read {source_path} to archive it: {exc}. Nothing was moved."
        ) from exc

    staged_fd, staged_name = tempfile.mkstemp(dir=archive_dir, prefix=".partial-", suffix=".tmp")
    staged_path = Path(staged_name)
    try:
        with os.fdopen(staged_fd, "wb") as staged, open(source_path, "rb") as original:
            shutil.copyfileobj(original, staged)
            staged.flush()
            os.fsync(staged.fileno())
        # A short copy means the archive copy is not byte-complete, so it must
        # never be published under a real name.
        staged_size = staged_path.stat().st_size
        if staged_size != source_size:
            raise ArchivePublishError(
                f"incomplete copy of {source_path} ({staged_size} of "
                f"{source_size} bytes); nothing was moved."
            )
        shutil.copystat(source_path, staged_path, follow_symlinks=True)

        published = _claim_archive_name(staged_path, archive_dir, source_path.name)
    except ArchivePublishError:
        staged_path.unlink(missing_ok=True)
        raise
    except OSError as exc:
        staged_path.unlink(missing_ok=True)
        raise ArchivePublishError(
            f"could not archive {source_path}: {exc}. Nothing was moved."
        ) from exc

    staged_path.unlink(missing_ok=True)
    # Only now is the content definitely in the archive under a complete name.
    os.unlink(source_path)
    return published


def _claim_archive_name(staged_path: Path, archive_dir: Path, name: str) -> Path:
    """Hard-link ``staged_path`` to the first free archive name, atomically.

    ``os.link`` is the reservation: it fails with ``FileExistsError`` when the
    destination name exists, so a competing writer can never be overwritten.
    That is the whole point — ``exists()`` followed by a move is a race, and the
    race loses an unrecoverable original.
    """
    for candidate in _candidate_archive_names(archive_dir, name):
        try:
            os.link(staged_path, candidate)
            return candidate
        except FileExistsError:
            continue  # someone holds that name; take the next one
        except OSError as exc:
            # Some filesystems (exFAT, some network mounts) have no hard links.
            # Fall back to an exclusive create, which is still an atomic claim
            # on the NAME; the cost is that a hard kill mid-copy can leave a
            # short file under a real name, which the link path cannot.
            if exc.errno not in (errno.EPERM, errno.EOPNOTSUPP, errno.ENOSYS, errno.EXDEV):
                raise
            return _claim_by_exclusive_copy(staged_path, archive_dir, name)
    raise ArchivePublishError(
        f"no free name for {name} in {archive_dir} after "
        f"{MAX_ARCHIVE_NAME_ATTEMPTS} attempts; nothing was moved."
    )


def _claim_by_exclusive_copy(staged_path: Path, archive_dir: Path, name: str) -> Path:
    """Fallback for filesystems without hard links: O_CREAT|O_EXCL then copy."""
    for candidate in _candidate_archive_names(archive_dir, name):
        try:
            fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            continue
        try:
            with os.fdopen(fd, "wb") as destination, staged_path.open("rb") as staged:
                shutil.copyfileobj(staged, destination)
                destination.flush()
                os.fsync(destination.fileno())
        except OSError:
            candidate.unlink(missing_ok=True)
            raise
        return candidate
    raise ArchivePublishError(
        f"no free name for {name} in {archive_dir} after "
        f"{MAX_ARCHIVE_NAME_ATTEMPTS} attempts; nothing was moved."
    )


def _candidate_archive_names(archive_dir: Path, name: str) -> Iterator[Path]:
    """``x.md``, ``x-1.md``, ``x-2.md``, … — names only, with no existence check.

    Deliberately does not test ``exists()``: the caller claims each name
    atomically and moves on if it is taken, so there is no window between
    checking and claiming. A ``.partial-`` staging file is never one of these,
    so abandoned debris can never be mistaken for an archived original.
    """
    yield archive_dir / name
    stem = Path(name).stem
    suffix = Path(name).suffix
    for counter in range(1, MAX_ARCHIVE_NAME_ATTEMPTS):
        yield archive_dir / f"{stem}-{counter}{suffix}"


def predict_archive_name(source_path: Path, archive_dir: Path) -> Path:
    """Where ``publish_into_archive`` would put ``source_path`` right now.

    For dry-run previews ONLY. The dry run used to hardcode
    ``<archive>/<name>``, so it promised ``old-memory-archive/MEMORY.md`` while
    ``--commit`` would pick ``MEMORY-1.md`` because the first name was already
    taken (Codex, 2026-11-06) — a preview that does not match what happens is
    worse than no preview.

    This is a prediction, not a reservation: it tests existence, which is
    exactly the race ``publish_into_archive`` refuses to rely on. That is
    acceptable here because nothing is being written — a preview can only ever
    be a best guess about a future run, and the real publication still refuses
    to clobber whatever it finds.
    """
    for candidate in _candidate_archive_names(archive_dir, source_path.name):
        if not candidate.exists() and not candidate.is_symlink():
            return candidate
    return archive_dir / source_path.name


def write_migration_marker(marker: Path, text: str) -> None:
    """Write ``marker`` atomically: it either does not exist, or is complete.

    ``has_migration_marker()`` only asks whether the file exists, so a partially
    written marker is accepted as a completed migration (Codex, 2026-11-06). A
    plain ``write_text`` that fails or is interrupted part-way therefore leaves a
    directory that reports itself migrated while this function's caller reports
    "NOT retired, so re-running retries it" — the two statements contradict each
    other, and the one the next run believes is the file.

    Staged in the same directory so the ``os.replace`` is a same-filesystem
    rename, which is atomic: no reader ever sees a short marker.
    """
    marker.parent.mkdir(parents=True, exist_ok=True)
    staged_fd, staged_name = tempfile.mkstemp(
        dir=marker.parent, prefix=".partial-marker-", suffix=".tmp"
    )
    staged_path = Path(staged_name)
    try:
        with os.fdopen(staged_fd, "w") as staged:
            staged.write(text)
            staged.flush()
            os.fsync(staged.fileno())
        os.replace(staged_path, marker)
    except OSError:
        staged_path.unlink(missing_ok=True)
        raise


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
            archived = publish_into_archive(legacy_pointer, memory_dir / ARCHIVE_DIR_NAME)
        except (ArchivePublishError, OSError) as exc:
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
            write_migration_marker(
                marker,
                f"migrated to {memory_home} on {today}\n"
                f"Files discovered here are in {ARCHIVE_DIR_NAME}/ — KEEP them unless you have\n"
                f"checked that the migrated copies contain everything you need.\n"
                f"Migration rewrites frontmatter: recognised fields are regenerated and any\n"
                f"UNRECOGNISED key is dropped, so those values survive only in the archive.\n",
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
