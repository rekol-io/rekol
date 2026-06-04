"""Shared pytest fixtures and test-isolation guards.

SECURITY/test-hygiene: the index now lives in a machine-local cache at
``${XDG_CACHE_HOME:-~/.cache}/rekol/<hash>`` (outside ``$REKOL_HOME``). Without
isolation, every test that builds an index would write into the developer's REAL
``~/.cache``. The autouse fixture below redirects ``XDG_CACHE_HOME`` at a
per-test temp dir so the index always lands in a throwaway location, and clears
``REKOL_INDEX_DIR`` so a stray override in the ambient env never leaks in.

Tests that need the resolved cache path import ``cache_dir_for`` from
``cache_helpers`` (tests/ is on ``sys.path`` via pyproject's pytest config).
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_index_cache(tmp_path_factory, monkeypatch) -> None:
    """Point the index cache at a throwaway dir for every test.

    Autouse so no test can accidentally write the index (incl. the
    secrets-bearing ``sessions.db``) into the real ``~/.cache``.
    """
    cache_root = tmp_path_factory.mktemp("xdg-cache")
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_root))
    monkeypatch.delenv("REKOL_INDEX_DIR", raising=False)
