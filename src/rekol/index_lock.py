"""Cross-command index-write lock (issue #24).

Serializes the curated-index WRITERS — a manual ``rekol index rebuild`` and the
hook-fired ``rekol index update`` — so a rebuild's atomic temp-DB swap can never
discard a concurrent update's commit (the update would otherwise write to the
old ``index.db`` inode that the swap is about to replace, and the write would be
lost on the now-unlinked inode).

Why ``flock`` and not the bash hook's ``mkdir`` mutex: ``flock`` is advisory and
released **automatically** when the holding file descriptor is closed — including
on process death — so a crashed or SIGKILL'd holder can never wedge the lock the
way a leftover lock directory can. The bash ``auto-reindex.sh`` mutex stays as a
burst-coalescing optimization; this lock is the authoritative serialization, and
because the hook ultimately runs ``rekol index update`` it is enforced in one
place (Python) for both writers — no cross-language coupling.
"""

from __future__ import annotations

import contextlib
import errno
import os
from collections.abc import Iterator
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - non-Unix; rekol's hooks are Unix-only
    fcntl = None  # type: ignore[assignment]


class IndexBusyError(RuntimeError):
    """Raised when a non-blocking index-write lock is already held by someone else."""


def index_lock_path(index_dir: Path) -> Path:
    """Path to the index-write lockfile inside the cache dir."""
    return index_dir / ".index-write.lock"


@contextlib.contextmanager
def index_write_lock(lock_path: Path, *, blocking: bool) -> Iterator[None]:
    """Hold an exclusive advisory lock on ``lock_path`` for the ``with`` body.

    Args:
        lock_path: Lockfile in the cache dir (created if absent).
        blocking: ``True`` waits for the lock — used by ``rebuild``, which must
            run to completion. ``False`` raises :class:`IndexBusyError` at once if
            another writer holds it — used by a hook-fired ``update``, for which
            skipping is correct because the running writer already covers the
            change (and the next update reconciles anything missed).

    Raises:
        IndexBusyError: ``blocking=False`` and the lock is held elsewhere.
    """
    if fcntl is None:  # pragma: no cover - non-Unix fallback
        # No advisory locking available: degrade to a no-op (the bash hook mutex
        # still coalesces hook invocations). rekol is Unix-only, so this is a
        # guard rather than a supported path.
        yield
        return

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        flags = fcntl.LOCK_EX if blocking else (fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            fcntl.flock(fd, flags)
        except OSError as exc:
            if not blocking and exc.errno in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                raise IndexBusyError(f"index write lock is held: {lock_path}") from exc
            raise
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
