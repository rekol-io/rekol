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


def _create_stage(directory: Path) -> tuple[int, Path]:
    """Create an exclusive temporary in ``directory`` and return ``(fd, path)``.

    ``os.open`` with ``0o666`` rather than ``tempfile.mkstemp``, which forces
    ``0o600``. The mode argument is what the KERNEL masks with the process
    umask, so a new memory ends up exactly as private as anything else the user
    creates -- ``0600`` under umask 077, ``0644`` under 022 -- and nothing here
    has to read, set or restore the umask, which would be racy in a library.

    The name is random rather than counted so two concurrent writers cannot
    collide, and ``O_EXCL`` means a collision fails rather than truncating.
    """
    for _ in range(_STAGE_NAME_ATTEMPTS):
        candidate = directory / f".partial-{os.urandom(8).hex()}.md.tmp"
        try:
            fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
        except FileExistsError:
            continue
        return fd, candidate
    raise OSError(
        errno.EEXIST,
        f"no free staging name in {directory} after {_STAGE_NAME_ATTEMPTS} attempts",
    )


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

    # INSIDE the boundary (product, 2026-11-07). A read-only directory or an
    # exhausted inode table raises here, and outside the try that escaped as a
    # raw OSError traceback -- which breaks the one promise this module makes,
    # that a failure says in one sentence whether the file survived.
    try:
        staged_fd, staged_path = _create_stage(directory)
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
        # An EXISTING target keeps its own mode: without this, an edit silently
        # reset permissions to whatever the staging created, which on a memory
        # home shared over a network mount is a real change.
        #
        # A NEW file gets whatever the umask says, and nothing is forced here.
        # Forcing 0644 was a security regression (product, 2026-11-07): under
        # umask 077 `write_text` produces 0600, so `capture` used to keep a new
        # memory private to the user and this made it readable by every local
        # account. The umask is applied by the KERNEL at creation in
        # `_create_stage`, which is why there is no `os.chmod` on this branch --
        # reading and restoring the process umask would be racy in a library.
        try:
            os.chmod(staged_path, target.stat().st_mode & 0o7777)
        except FileNotFoundError:
            pass
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
