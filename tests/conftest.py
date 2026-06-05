"""Shared pytest fixtures and test-isolation guards.

SECURITY/test-hygiene: the index now lives in a machine-local cache at
``${XDG_CACHE_HOME:-~/.cache}/rekol/<hash>`` (outside ``$REKOL_HOME``). Without
isolation, every test that builds an index would write into the developer's REAL
``~/.cache``. The autouse fixture below redirects ``XDG_CACHE_HOME`` at a
per-test temp dir so the index always lands in a throwaway location, and clears
``REKOL_INDEX_DIR`` so a stray override in the ambient env never leaks in.

It ALSO clears ``REKOL_HOME`` / ``MEMORY_HOME``: a contributor who exports those
in their shell (every installed rekol user does) would otherwise have the ambient
data-home leak into any test that only sets ``MEMORY_HOME`` — and since
``REKOL_HOME`` takes precedence, the runtime would resolve a DIFFERENT cache
digest than the test asserts against, so the test fails locally but passes in CI
(where neither var is set). Clearing both makes the suite hermetic; tests that
need a home set it explicitly.

Tests that need the resolved cache path import ``cache_dir_for`` from
``cache_helpers`` (tests/ is on ``sys.path`` via pyproject's pytest config).
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_index_cache(tmp_path_factory, monkeypatch) -> None:
    """Point the index cache at a throwaway dir for every test, and clear the
    ambient data-home env so it cannot pollute a test that sets its own home.

    Autouse so no test can accidentally write the index (incl. the
    secrets-bearing ``sessions.db``) into the real ``~/.cache``, and so a
    developer's exported ``REKOL_HOME`` never silently overrides a test's home.
    """
    cache_root = tmp_path_factory.mktemp("xdg-cache")
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_root))
    monkeypatch.delenv("REKOL_INDEX_DIR", raising=False)
    # Hermetic data-home: clear the ambient vars so a test that sets only
    # MEMORY_HOME isn't shadowed by an exported REKOL_HOME (which has precedence).
    monkeypatch.delenv("REKOL_HOME", raising=False)
    monkeypatch.delenv("MEMORY_HOME", raising=False)
