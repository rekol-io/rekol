"""Tests for the cross-command index-write lock (issue #24)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from rekol.index_lock import IndexBusyError, index_lock_path, index_write_lock


def test_lock_acquires_and_releases(tmp_path: Path) -> None:
    """A single blocking acquire succeeds, and the lock is free again after."""
    lock = index_lock_path(tmp_path)
    with index_write_lock(lock, blocking=True):
        pass
    # Free now: a non-blocking acquire must succeed without raising.
    with index_write_lock(lock, blocking=False):
        pass


def test_non_blocking_raises_while_held(tmp_path: Path) -> None:
    """While the lock is held, a non-blocking acquire raises IndexBusyError, and
    succeeds again once the holder releases (proving it is released on exit).

    flock is per open-file-description, so a second ``index_write_lock`` (its own
    fd) genuinely contends with the outer hold even within one process — this is
    the same contention a hook-fired ``update`` sees against a running rebuild.
    """
    lock = index_lock_path(tmp_path)
    with index_write_lock(lock, blocking=True):
        with pytest.raises(IndexBusyError):
            with index_write_lock(lock, blocking=False):
                pass  # pragma: no cover - body must not run while contended
    # Released on exit from the outer `with`.
    with index_write_lock(lock, blocking=False):
        pass


def test_lock_released_on_fd_close_after_holder_exit(tmp_path: Path) -> None:
    """A holder that vanishes (fd closed, mimicking process death) frees the lock
    — flock auto-releases, so the lock can never wedge the way a stale lockdir can.
    """
    lock = index_lock_path(tmp_path)
    import fcntl

    fd = os.open(str(lock), os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX)
    # Held: non-blocking acquire fails.
    with pytest.raises(IndexBusyError):
        with index_write_lock(lock, blocking=False):
            pass  # pragma: no cover
    # "Process death": just close the fd (no explicit unlock).
    os.close(fd)
    with index_write_lock(lock, blocking=False):
        pass


def test_lockfile_parent_created(tmp_path: Path) -> None:
    """The lock works even if the cache dir does not exist yet (it is created)."""
    cache = tmp_path / "not-yet" / "rekol-cache"
    assert not cache.exists()
    with index_write_lock(index_lock_path(cache), blocking=True):
        pass
    assert cache.exists()
