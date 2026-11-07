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

import os
import tempfile
from pathlib import Path

__all__ = ["AtomicWriteError", "write_text_atomically"]


class AtomicWriteError(Exception):
    """A file could not be written, and was left exactly as it was.

    Carried as its own type so the CLI can print one sentence saying the memory
    is unchanged. A raw traceback here is worse than unhelpful: the user has no
    way to tell from it whether their file survived, and that is the only thing
    they want to know.
    """


def write_text_atomically(path: Path, text: str) -> None:
    """Replace ``path``'s content with ``text``, atomically.

    Either the file ends up holding exactly ``text``, or it is untouched and
    :class:`AtomicWriteError` is raised. There is no third outcome, and in
    particular no window in which the file is shorter than either version.

    ``path``'s parent must exist; callers that create a new memory create its
    layer directory first, which they already did.
    """
    directory = path.parent
    staged_fd, staged_name = tempfile.mkstemp(dir=directory, prefix=".partial-", suffix=".md.tmp")
    staged_path = Path(staged_name)
    try:
        with os.fdopen(staged_fd, "w", encoding="utf-8") as staged:
            staged.write(text)
            staged.flush()
            # Before the rename, not after: a rename is atomic with respect to
            # the NAME, not to the data behind it. Without this a crash can
            # leave the published name pointing at blocks that were never
            # written.
            os.fsync(staged.fileno())
        # Preserve the mode of the file being replaced, if there is one. Without
        # this an edit silently resets permissions to the 0600 mkstemp gives,
        # which for a memory home shared over a network mount is a real change.
        try:
            os.chmod(staged_path, path.stat().st_mode & 0o7777)
        except FileNotFoundError:
            os.chmod(staged_path, 0o644)  # a new file: the ordinary default
        os.replace(staged_path, path)
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
