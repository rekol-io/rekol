"""The one way rekol writes a file in the memory home (#238).

WHY THIS EXISTS
---------------
``Path.write_text`` and ``open(path, "w")`` **truncate before writing**. Every
command that edits an existing memory in place — ``confirm``, ``flag-suspect``,
``invalidate``, ``review`` — therefore destroyed the user's file first and wrote
the replacement second. A fault in between (ENOSPC, a kill, a full disk on a
synced Dropbox home) left the memory truncated or empty, and the only copy of
that content was the one that had just been truncated.

``capture`` had the same shape with a different consequence: an interrupted
write left a 0-byte or half-written ``.md`` file under a real name, which the
indexer then read as a memory.

So the rule is one line long and this module is the only place it lives:

    **a final name in the memory home only ever receives bytes via os.replace**

``os.replace`` is atomic within a filesystem: the name points at the old content
or the new content, never at a partial write. The staged temporary is created in
the SAME directory so the replace cannot cross a filesystem boundary, and it is
fsynced before publication so a crash cannot leave the rename pointing at
unwritten blocks.

WHY THE TEMPORARY IS NAMED THE WAY IT IS
----------------------------------------
``.partial-<random>.md.tmp``: a leading dot AND a suffix that is not ``.md``.
The dot alone is NOT enough — ``Path.glob("**/*.md")`` matches dotfiles, which
is measurable and surprising, and the indexer's walk uses exactly that glob. So
a staged file named ``.partial-x.md`` would be indexed as a memory. The ``.tmp``
suffix is what keeps it out of the walk; the dot is belt and braces for anything
that filters on hidden names instead.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path

_STAGE_NAME_ATTEMPTS = 100

__all__ = ["AtomicWriteError", "write_text_atomically"]


# DOCUMENTED LIMITS of this publication (product, 2026-11-07; filed, not fixed
# here, because widening them is a filesystem project rather than this change):
#
#   * HARD LINKS to the target are broken. `os.replace` swaps the name, so a
#     second name for the old inode keeps the OLD content. `write_text` wrote
#     through the link and kept both in step.
#   * EXTENDED ATTRIBUTES and ACLs are not copied from the target; the published
#     file carries whatever the staged one had.
#   * OWNERSHIP comes from the staged file, i.e. the writing user. Replacing a
#     file owned by someone else changes its owner.
#   * DURABILITY of the directory entry is not claimed. The file's bytes are
#     fsynced before the rename, so a crash leaves the old content or the new
#     content -- never a fragment -- but the rename itself is not fsynced, so
#     which of the two survives a power loss is up to the filesystem.


class AtomicWriteError(Exception):
    """A file could not be written, and was left exactly as it was.

    Carried as its own type so the CLI can print one sentence saying the memory
    is unchanged. A raw traceback here is worse than unhelpful: the user has no
    way to tell from it whether their file survived, and that is the only thing
    they want to know.
    """


def _create_stage(directory: Path, mode: int) -> tuple[int, Path]:
    """Create an exclusive temporary in ``directory`` and return ``(fd, path)``.

    ``os.open`` rather than ``tempfile.mkstemp``, which forces ``0o600``. The
    mode argument is what the KERNEL masks with the process umask, so nothing
    here has to read, set or restore the umask, which would be racy in a
    library, and the file is never for an instant more permissive than ``mode``.

    ``mode`` is ``0o666`` for a NEW file, so a new memory ends up exactly as
    private as anything else the user creates -- ``0600`` under umask 077,
    ``0644`` under 022. For an EXISTING target it is that target's own mode, so
    the stage starts no more readable than the file it will become; see
    :func:`write_text_atomically`.

    The name is random rather than counted so two concurrent writers cannot
    collide, and ``O_EXCL`` means a collision fails rather than truncating.
    """
    for _ in range(_STAGE_NAME_ATTEMPTS):
        candidate = directory / f".partial-{os.urandom(8).hex()}.md.tmp"
        try:
            fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
        except FileExistsError:
            continue
        return fd, candidate
    raise OSError(
        errno.EEXIST,
        f"no free staging name in {directory} after {_STAGE_NAME_ATTEMPTS} attempts",
    )


def _mode_of_existing(target: Path, path: Path) -> int | None:
    """``target``'s permission bits, or ``None`` when there is no file there yet.

    Any other error is refused rather than guessed at. The mode decides how
    private the staged copy of the user's content will be, so "could not tell"
    cannot quietly become "0666 and hope the umask covers it" — that is the
    regression this function exists to prevent, one step removed.
    """
    try:
        return target.stat().st_mode & 0o7777
    except OSError as exc:
        if exc.errno in (errno.ENOENT, errno.ENOTDIR):
            return None
        raise AtomicWriteError(
            f"could not read the current permissions of {path}: {exc}. Nothing "
            f"was written to it, because the staged copy could not be made as "
            f"private as the file it would replace."
        ) from exc


def write_text_atomically(path: Path, text: str) -> None:
    """Replace ``path``'s content with ``text``, atomically.

    Either the file ends up holding exactly ``text``, or it is untouched and
    :class:`AtomicWriteError` is raised. There is no third outcome, and in
    particular no window in which the file is shorter than either version.

    SYMLINKS ARE FOLLOWED, which this has to do explicitly (product, 2026-11-07).
    ``os.replace`` acts on the NAME, so replacing a symlinked memory would
    delete the link and leave a regular file holding the new text while the real
    file kept the old — a silent fork, with the user's edit in a different place
    from the file they think they edited. ``Path.write_text`` follows the link,
    so that worked before this module existed and must keep working. The target
    is resolved first and the temporary is staged next to it, which also keeps
    the ``os.replace`` on one filesystem when the link points off the home.

    A DANGLING link is followed too, again matching ``write_text``: the target is
    created. If the target's directory does not exist, the staging fails and
    that surfaces as :class:`AtomicWriteError` like any other write failure.
    """
    # realpath, not Path.resolve(strict=True): the target need not exist (a new
    # memory, or a dangling link), and parent symlinks are resolved too so the
    # staging really does land beside the real file.
    target = Path(os.path.realpath(path))
    directory = target.parent

    # THE TARGET'S MODE IS READ FIRST, BEFORE THE STAGE EXISTS (Codex P1,
    # product 2026-11-07: a regression introduced by this module, and the reason
    # is privacy rather than tidiness).
    #
    # The stage used to be created 0666-and-umask and the target's mode copied
    # onto it afterwards. Under umask 022 that wrote the full content of a 0600
    # memory into a world-readable 0644 temporary, and a kill before the chmod
    # left that file sitting in the memory home. Private content was readable by
    # every local account for the duration of the write, which is longer than it
    # sounds on a synced home.
    #
    # The rule is: AT NO POINT CAN THE STAGE BE MORE READABLE THAN THE FINAL
    # FILE. Passing the target's mode to `os.open` is what enforces it, because
    # the kernel masks that mode with the umask -- so the stage is at most the
    # target's permissions and possibly tighter, never wider, from the instant
    # it exists and therefore before its first byte. The exact mode is restored
    # after the write, so a tighter umask does not re-permission the file.
    existing_mode = _mode_of_existing(target, path)
    try:
        staged_fd, staged_path = _create_stage(
            directory, 0o666 if existing_mode is None else existing_mode
        )
    except OSError as exc:
        raise AtomicWriteError(
            f"could not prepare a temporary file in {directory} to write "
            f"{path}: {exc}. The file is UNCHANGED — nothing was written to it."
        ) from exc
    try:
        with os.fdopen(staged_fd, "w", encoding="utf-8") as staged:
            staged.write(text)
            staged.flush()
            # Before the rename, not after: a rename is atomic with respect to
            # the NAME, not to the data behind it. Without this a crash can
            # leave the published name pointing at blocks that were never
            # written.
            os.fsync(staged.fileno())
        # An EXISTING target keeps its own mode EXACTLY: the stage was created
        # no wider than this, but a tight umask may have made it narrower, and
        # an edit must not silently tighten a file either -- on a memory home
        # shared over a network mount that is a real change.
        #
        # A NEW file gets whatever the umask says, and nothing is forced here.
        # Forcing 0644 was a security regression (product, 2026-11-07): under
        # umask 077 `write_text` produces 0600, so `capture` used to keep a new
        # memory private to the user and this made it readable by every local
        # account. The umask is applied by the KERNEL at creation in
        # `_create_stage`, which is why there is no `os.chmod` on this branch --
        # reading and restoring the process umask would be racy in a library.
        if existing_mode is not None:
            os.chmod(staged_path, existing_mode)
        os.replace(staged_path, target)
    except OSError as exc:
        staged_path.unlink(missing_ok=True)
        raise AtomicWriteError(
            f"could not write {path}: {exc}. The file is UNCHANGED — nothing "
            f"was written to it, and the partial copy was removed."
        ) from exc
    except BaseException:
        # KeyboardInterrupt and SystemExit too: the staged file must not be left
        # behind as debris, and the target is untouched either way.
        staged_path.unlink(missing_ok=True)
        raise
