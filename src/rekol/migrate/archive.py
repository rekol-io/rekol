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
import hashlib
import os
import shutil
import stat
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


def archive_file(lf: LegacyFile, *, expected_digest: str | None = None) -> Path:
    """Publish ``lf.source_path`` into ``<source_root>/old-memory-archive/``.

    Returns the new path. If the target name is taken, tries ``-1``, ``-2``, …
    Raises ``ArchivePublishError`` without touching the source if it cannot
    publish a complete copy.

    ``expected_digest`` is the sha256 the caller has already IMPORTED from this
    file. Pass it and the archive refuses unless the bytes it is about to retire
    are still those bytes — see :func:`publish_into_archive`.
    """
    return publish_into_archive(
        lf.source_path, lf.source_root / ARCHIVE_DIR_NAME, expected_digest=expected_digest
    )


class _HardLinksUnsupportedError(Exception):
    """This filesystem cannot hard-link, so the weaker copy contract applies."""


def _retract_archive_entry(candidate: Path, source_path: Path) -> str:
    """Drop ``candidate`` ONLY if those bytes keep another name, and say which.

    Returns the clause to put in the refusal message. Both outcomes are
    acceptable; what is not acceptable is guessing, or claiming a removal that
    did not happen.

    WHY A REMOVAL NEEDS A PRECONDITION AT ALL (Codex P1, 2026-11-07). On the
    hard-link path the mismatch branch used to unlink the archive entry
    unconditionally, with a comment arguing it was safe "because it is a hard
    link to the still-present source". That argument holds at the moment the
    identity check passes and stops holding immediately after: Codex replaced
    the source pathname between that check and the final hash, which left the
    archive entry holding the LAST link to the old bytes. The unlink then
    destroyed them, and the import undo destroyed the imported copy of them, so
    they survived nowhere.

    So the precondition is the thing the argument assumed: the entry is still
    the same inode as the live source, and the link count shows a name other
    than this one. ``st_nlink >= 2`` is implied by the inode match — two
    distinct names for one inode — and is checked anyway because it is the
    direct reading of "another name survives" and costs nothing.

    On the COPY path the precondition can never hold: a verified copy is a
    different inode by construction, so the entry is always kept. That is
    deliberate and is product's ruling of 2026-11-07, reversing the previous
    round's "remove the published copy so a retry finds no twin": a kept archive
    entry that is not linked to the live source meets the contract, and "bytes
    removed on a guess" is the wrong default in a data-loss fix. The cost is an
    untidy archive after a refusal — a retry adds ``-1`` beside it — and the
    refusal message names the entry so it is not a surprise.
    """
    try:
        published_stat = candidate.lstat()
        source_stat = source_path.lstat()
    except OSError as exc:
        return (
            f"the archive entry {candidate} was KEPT because it could not be "
            f"inspected ({exc}), so whether removing it was safe is unknown"
        )

    same_inode = (published_stat.st_dev, published_stat.st_ino) == (
        source_stat.st_dev,
        source_stat.st_ino,
    )
    if not (same_inode and published_stat.st_nlink >= 2):
        return (
            f"the archive entry {candidate} was KEPT: it is not a second name "
            f"for the live source, so removing it could have destroyed the only "
            f"copy of those bytes"
        )

    try:
        os.unlink(candidate)
    except OSError as exc:
        return f"the archive entry {candidate} could NOT be removed ({exc}) and is still there"
    return (
        f"the archive entry {candidate} was removed, which was safe because it "
        f"was a second name for the source that is still there"
    )


# errno values that mean "hard links are not available here" rather than "this
# particular link failed": exFAT and some network mounts, and cross-device.
_NO_HARD_LINK_ERRNOS = frozenset(
    {errno.EPERM, errno.EOPNOTSUPP, errno.ENOSYS, errno.EXDEV, errno.EMLINK}
)


def publish_into_archive(
    source_path: Path, archive_dir: Path, *, expected_digest: str | None = None
) -> Path:
    """Move ``source_path`` into ``archive_dir``, never overwriting anything.

    TWO GUARANTEES, DELIBERATELY NOT THE SAME ONE (product ruling, 2026-11-06).

    **Hard-link publication — the ordinary path.** ``os.link`` is atomic, fails
    when the destination name is taken (a dangling symlink included, because the
    check is on the name), and publishes the very same inode. A writer who edits
    THAT INODE between the link and the unlink edits the archived copy too, so
    nothing is lost. What can still lose bytes is a writer who replaces the
    PATHNAME, because the name then denotes a different file from the one that
    was archived — so device and inode are confirmed immediately before the
    unlink. What remains, and is accepted: a replacement in the interval between
    that confirmation and the unlink. This is not an atomic compare-and-unlink
    and is not described as one.

    **Verified copy — symlink sources and filesystems without hard links.**
    Here there genuinely are two copies, so a writer editing the source between
    the copy and the unlink would otherwise be silently discarded: the source
    would be gone and the archive would hold the old bytes. That is the data-loss
    window the first version of this function introduced, and it is why copying
    is no longer the ordinary path. The source is re-read and re-hashed after the
    copy and before anything is removed; on any mismatch the source is KEPT, the
    failure is reported, and the directory is not retired.

    Concurrent-writer contract, in one sentence for both paths: the source's
    content and identity are verified as late as possible, and **the only
    accepted window is the interval between that last verification and the
    unlink** — a write of any kind in that interval (in place or a replacement
    of the pathname) is lost. Everything earlier is caught:

    * **copy path** — the source is re-read and re-hashed after publication and
      its digest compared both with the staged copy and with the caller's
      imported bytes, so an in-place write is caught however small; its identity
      is confirmed against the pathname, which is what catches a replacement.
      Codex showed an earlier version lost a whole hash's worth of window here;
      that is closed.
    * **hard-link path** — device and inode are confirmed against the published
      copy, and then the caller's digest is re-verified, immediately before the
      unlink.

    Two narrower statements that used to be here were wrong and are worth not
    writing again. "Accepted: an in-place write that leaves size, mtime AND
    content unchanged" describes nothing (an unchanged file is not a write).
    "Accepted: an in-place write that leaves size and mtime unchanged" is true
    of what :func:`_digest_of`'s identity evidence can SEE, and that is where it
    is now documented — but it is not accepted by this function, because the
    digest comparison catches the changed bytes regardless of size and mtime.

    A final archive name never receives bytes incrementally. It is claimed by an
    atomic ``os.link``, or where that is unsupported by an empty ``O_EXCL``
    placeholder that is then ``os.replace``d, so an interruption leaves either
    the staged ``.partial-`` temporary or a 0-byte file — never a fragment that
    reads as a shortened original.

    SYMLINK SOURCES are archived as a real-content snapshot, which is what makes
    "kept byte-complete in the archive" true — moving the link re-resolves a
    relative target against the archive dir and breaks it. The target file is
    left where it is; only the link is removed. A **dangling** or **looping**
    link has no readable content, so it is refused with nothing touched.
    """
    archive_dir.mkdir(parents=True, exist_ok=True)

    # lstat, not stat: a dangling or self-looping link must be diagnosed here
    # rather than raising ENOENT/ELOOP from somewhere deeper (Codex P2).
    try:
        link_stat = source_path.lstat()
    except OSError as exc:
        raise ArchivePublishError(
            f"cannot inspect {source_path} to archive it: {exc}. Nothing was moved."
        ) from exc

    is_symlink = stat.S_ISLNK(link_stat.st_mode)
    if is_symlink:
        try:
            source_path.stat()  # follows the link: ELOOP or ENOENT surfaces here
        except OSError as exc:
            raise ArchivePublishError(
                f"{source_path} is a symlink whose target cannot be read ({exc}); "
                f"nothing was moved and the directory was not retired, so its "
                f"content is still wherever the link points."
            ) from exc
    else:
        try:
            return _publish_by_hard_link(source_path, archive_dir, expected_digest=expected_digest)
        except _HardLinksUnsupportedError:
            pass  # fall through to the copy, with the weaker contract above

    return _publish_by_verified_copy(source_path, archive_dir, expected_digest=expected_digest)


def _publish_by_hard_link(
    source_path: Path, archive_dir: Path, *, expected_digest: str | None = None
) -> Path:
    """Link the source's inode into the archive, then drop the source name."""
    for candidate in _candidate_archive_names(archive_dir, source_path.name):
        try:
            os.link(source_path, candidate)
        except FileExistsError:
            continue  # someone holds that name; take the next one
        except OSError as exc:
            if exc.errno in _NO_HARD_LINK_ERRNOS:
                raise _HardLinksUnsupportedError from exc
            raise ArchivePublishError(
                f"could not archive {source_path}: {exc}. Nothing was moved."
            ) from exc
        # "Same inode, no content window" holds only while the pathname still
        # DENOTES that inode (Codex, 2026-11-06). An editor's atomic save
        # replaces the source pathname with a brand-new file, so an
        # unconditional unlink here deleted the NEW bytes while the archive kept
        # the old ones. So the source's identity is confirmed against what was
        # just published, immediately before removing the name.
        try:
            source_stat = source_path.lstat()
            published_stat = candidate.lstat()
        except OSError as exc:
            raise ArchivePublishError(
                f"could not confirm {source_path} after archiving it ({exc}); the "
                f"source was KEPT and the directory was not retired."
            ) from exc
        if (source_stat.st_dev, source_stat.st_ino) != (
            published_stat.st_dev,
            published_stat.st_ino,
        ):
            # Deliberately NOT removing the published copy: it is the only name
            # left for the bytes that were there when we linked, so unlinking it
            # would destroy them. Both survive; the run reports and does not
            # retire. This is the same rule _retract_archive_entry applies —
            # the identity check having just failed is exactly its "not a second
            # name for the live source" case — spelled out here because the
            # reason the name must survive is the reason the check exists.
            raise ArchivePublishError(
                f"{source_path} was replaced while it was being archived, so it "
                f"is no longer the file that was archived to {candidate}. Both "
                f"were KEPT and the directory was not retired — re-run to "
                f"archive the current content."
            )
        # IDENTITY IS NOT CONTENT (QA + product, 2026-11-07). The check above
        # proves this is the same FILE; an in-place write keeps the inode, so it
        # says nothing about the bytes. Measured on this very path: the caller
        # imported OLD, the user edited in place, the link published NEW, the
        # source was unlinked, the directory was retired, and the run exited 0 —
        # so the newest text existed only in the archive, which is not
        # searchable, and the home held a stale memory with nothing to flag it.
        #
        # So when the caller tells us which bytes it imported, that is what has
        # to be retired. The check sits as late as possible, immediately before
        # the unlink, which leaves the same last-check-to-unlink interval
        # product has already accepted on the copy path — and no wider one.
        if expected_digest is not None:
            current = _digest_of(source_path)
            if current.digest != expected_digest:
                raise ArchivePublishError(
                    f"{source_path} changed while it was being archived, so the "
                    f"bytes already imported are not the bytes that would have "
                    f"been retired. The source was KEPT, the directory was not "
                    f"retired, and " + _retract_archive_entry(candidate, source_path) + "."
                )
        # os.unlink removes the name, never a link target. A failure here is not
        # a tidy-up problem: the import is already written, so it has to reach
        # the caller as an ArchivePublishError rather than a bare OSError, which
        # bypassed the undo and left the import behind under a message that said
        # nothing about it (Codex P2, 2026-11-07).
        try:
            os.unlink(source_path)
        except OSError as exc:
            raise ArchivePublishError(
                f"{source_path} was archived to {candidate} but could not then "
                f"be removed ({exc}), so the directory was not retired, and "
                + _retract_archive_entry(candidate, source_path)
                + " — the content is still at the source either way."
            ) from exc
        return candidate
    raise ArchivePublishError(
        f"no free name for {source_path.name} in {archive_dir} after "
        f"{MAX_ARCHIVE_NAME_ATTEMPTS} attempts; nothing was moved."
    )


@dataclass(frozen=True)
class _HashedFile:
    """A digest together with proof of WHICH file it is a digest of."""

    digest: str
    size: int
    dev: int
    ino: int
    mtime_ns: int


def _digest_of(path: Path) -> _HashedFile:
    """Hash ``path``, and prove the bytes belong to the file that name denotes NOW.

    A digest alone is not evidence about a pathname (Codex, 2026-11-07). Hashing
    reads through an open descriptor, so if an editor replaces the pathname while
    the read is in flight the descriptor keeps returning the OLD inode's bytes to
    EOF. The comparison against the archived copy then SUCCEEDS, and the unlink
    that follows deletes the NEW file. Codex reproduced it with a 3 MiB source
    replaced after the first 1 MiB had been read: publication reported success,
    the source was gone, the archive held the old content, and the new content
    existed nowhere.

    That is a whole-hash window, not the brief interval product accepted, so
    three observations are taken rather than one:

    * ``fstat`` on the descriptor BEFORE reading — the identity and state of the
      file whose bytes are about to be hashed;
    * ``fstat`` on the same descriptor AFTER reading — size and mtime must be
      unchanged. Asked for by product, and honestly: it is REDUNDANT in every
      case I could construct. An in-place write changes the content, so the
      digest comparison catches it, and it changes mtime, so the pathname stat
      below catches it too. Kept because it is one stat, it makes the order of
      observations explicit, and it closes a write that is reverted between the
      read and the final stat. Not load-bearing; the pathname check is;
    * ``os.stat`` on the PATHNAME after reading — device, inode, size and mtime
      must match the first observation, which is what catches the atomic-save
      replacement.

    Any mismatch, or any failure to establish them, raises. The caller keeps the
    source and refuses retirement; it never guesses.

    THE LIMIT OF THE IDENTITY EVIDENCE, stated here because this is where it
    lives: size and mtime are all that is compared, so an in-place write that
    leaves both unchanged satisfies all three observations even though the
    content changed. That is a limit on the EVIDENCE, not a hole in the caller's
    contract — the digest this function returns is of the bytes actually read,
    so the caller's comparison catches those changed bytes regardless. The
    window the caller does accept is the interval from the final ``os.stat``
    here to its own unlink, and nothing wider.
    """
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError as exc:
        raise ArchivePublishError(
            f"cannot read {path} to verify it: {exc}. Nothing was removed."
        ) from exc
    try:
        before = os.fstat(fd)
        digest = hashlib.sha256()
        size = 0
        # os.read on the raw descriptor, not a buffered wrapper, so the fstat
        # below is on the very descriptor these bytes came from.
        while chunk := os.read(fd, 1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
        after = os.fstat(fd)
        current = os.stat(path)
    except OSError as exc:
        raise ArchivePublishError(
            f"could not confirm the identity of {path} while verifying it "
            f"({exc}); nothing was removed and the directory was not retired."
        ) from exc
    finally:
        os.close(fd)

    if (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns):
        raise ArchivePublishError(
            f"{path} was written to while it was being verified "
            f"({before.st_size} bytes at {before.st_mtime_ns}, "
            f"{after.st_size} at {after.st_mtime_ns}); it was KEPT and the "
            f"directory was not retired — re-run to archive the current content."
        )
    if size != before.st_size:
        raise ArchivePublishError(
            f"read {size} of {before.st_size} bytes from {path} while verifying "
            f"it; it was KEPT and the directory was not retired."
        )
    if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ):
        raise ArchivePublishError(
            f"{path} was replaced while it was being verified, so the bytes just "
            f"read are no longer the file that name refers to. It was KEPT and "
            f"the directory was not retired — re-run to archive the current "
            f"content."
        )
    return _HashedFile(
        digest=digest.hexdigest(),
        size=size,
        dev=before.st_dev,
        ino=before.st_ino,
        mtime_ns=before.st_mtime_ns,
    )


def _publish_by_verified_copy(
    source_path: Path, archive_dir: Path, *, expected_digest: str | None = None
) -> Path:
    """Copy, re-verify the source is unchanged, and only then remove it."""
    try:
        expected_size = source_path.stat().st_size  # follows symlinks on purpose
    except OSError as exc:
        raise ArchivePublishError(
            f"cannot read {source_path} to archive it: {exc}. Nothing was moved."
        ) from exc

    staged_fd, staged_name = tempfile.mkstemp(dir=archive_dir, prefix=".partial-", suffix=".tmp")
    staged_path = Path(staged_name)
    try:
        with os.fdopen(staged_fd, "wb") as staged, source_path.open("rb") as original:
            shutil.copyfileobj(original, staged)
            staged.flush()
            os.fsync(staged.fileno())
        shutil.copystat(source_path, staged_path, follow_symlinks=True)

        # The staged file is ours and nobody else holds its name, so the
        # identity checks inside _digest_of are belt and braces here. Using the
        # same function anyway: one way to hash a file, not two.
        staged_hash = _digest_of(staged_path)
        # TWO DIFFERENT CHECKS, and collapsing them into one is a hole my first
        # attempt at Codex's P1b actually opened: moving verification after
        # publication without keeping this one let a SHORT STAGE be published as
        # a fragment, which a test caught.
        #
        # (1) Before publishing: is the staged copy complete? A failed or short
        #     copy must never reach a real name.
        if staged_hash.size != expected_size:
            raise ArchivePublishError(
                f"incomplete copy of {source_path} ({staged_hash.size} of "
                f"{expected_size} bytes); the source was KEPT and nothing was "
                f"published."
            )

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

    # (2) After publishing: has the SOURCE changed since we read it? This has to
    # be here rather than before the claim, because on the no-hard-link path the
    # claim itself used to copy the whole file again, so a source mutated during
    # that copy was lost with the check already behind us (Codex's P1b).
    # Verifying here covers everything up to the instant before the name goes.
    source_hash = _digest_of(source_path)
    mismatch = (staged_hash.digest, staged_hash.size) != (source_hash.digest, source_hash.size)
    # The caller's bytes too, not only our own copy's. Same reasoning as the
    # hard-link path: what gets retired has to be what was imported.
    not_what_was_imported = expected_digest is not None and source_hash.digest != expected_digest
    if mismatch or not_what_was_imported:
        # The published copy is KEPT, and the earlier round's "remove it so a
        # retry finds no twin" is reversed (product, 2026-11-07). A verified copy
        # is a different inode from the source by construction, so nothing else
        # names those bytes and removing them is destruction on a guess. The
        # helper states the rule once for both paths.
        raise ArchivePublishError(
            f"{source_path} changed while it was being archived "
            f"({source_hash.size} bytes now, {staged_hash.size} copied); the "
            f"source was KEPT, the directory was not retired, and "
            + _retract_archive_entry(published, source_path)
            + " — re-run to migrate the current content."
        )

    # As on the hard-link path: the import is already written, so a failure to
    # remove the source has to arrive as an ArchivePublishError.
    try:
        os.unlink(source_path)
    except OSError as exc:
        raise ArchivePublishError(
            f"{source_path} was archived to {published} but could not then be "
            f"removed ({exc}), so the directory was not retired, and "
            + _retract_archive_entry(published, source_path)
            + " — the content is still at the source either way."
        ) from exc
    return published


def source_digest(path: Path) -> str:
    """The sha256 of ``path``'s content, with its identity confirmed.

    Exposed for the migrator, which has to hash a file BEFORE importing it so
    the archive can refuse to retire anything else. One hashing function for
    both, so the two cannot disagree about what a digest of a file means.
    """
    return _digest_of(path).digest


def claim_free_name(staged_path: Path, directory: Path, name: str) -> Path:
    """Publish ``staged_path`` into ``directory`` under the first free name.

    Shared by the archive and by the migrator's import write, because both need
    the same two properties: the name is claimed atomically so a competing
    writer is never overwritten, and the file only ever appears under a real
    name COMPLETE — a fault while writing leaves the staged temporary behind,
    not a truncated file that looks like a memory.
    """
    return _claim_archive_name(staged_path, directory, name)


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
            return _claim_by_placeholder_and_replace(staged_path, archive_dir, name)
    raise ArchivePublishError(
        f"no free name for {name} in {archive_dir} after "
        f"{MAX_ARCHIVE_NAME_ATTEMPTS} attempts; nothing was moved."
    )


def _claim_by_placeholder_and_replace(staged_path: Path, directory: Path, name: str) -> Path:
    """Claim a free name with an EMPTY placeholder, then atomically replace it.

    For filesystems without hard links. The previous version copied the whole
    file into the final name, which broke two contracts at once (Codex,
    2026-11-06): an interrupted copy left a partial file visible under an
    ordinary ``.md`` name — exactly the wrong-memory hazard QA measured — and
    that copy happened *after* the digest check, so a source mutated during it
    was lost with the check already behind us.

    Here the final name only ever receives bytes via ``os.replace``, which is
    atomic: it holds either the empty placeholder or the complete file, never a
    fragment. ``O_EXCL`` on the placeholder keeps the claim no-clobber.

    The worst interruption leaves a 0-byte file under a real name. For the
    archive that is inert. For an import it is a file the indexer rejects as
    having no frontmatter, so it cannot become a wrong memory — the property
    that actually matters, and a test asserts it.
    """
    for candidate in _candidate_archive_names(directory, name):
        try:
            placeholder_fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            continue
        os.close(placeholder_fd)
        try:
            os.replace(staged_path, candidate)
        except OSError:
            # Our own placeholder, so removing it cannot destroy anything.
            candidate.unlink(missing_ok=True)
            raise
        return candidate
    raise ArchivePublishError(
        f"no free name for {name} in {directory} after "
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
    # `exists()` FOLLOWS the link, so a dangling or looping MEMORY.md looked
    # absent and the directory was retired with no errors at all — the link's
    # content abandoned, silently (Codex, 2026-11-06). `is_symlink()` is what
    # notices it; publication then refuses and the caller does not retire.
    legacy_present = legacy_pointer.exists() or legacy_pointer.is_symlink()
    if archive_legacy_pointer and legacy_present:
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
