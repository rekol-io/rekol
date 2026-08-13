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

The durable transcript archive (#8) gets the same treatment: it is default-ON, so
``session-index`` archive-syncs verbatim transcripts (potentially secret-bearing)
into ``${XDG_DATA_HOME:-~/.local/share}/rekol/archive`` before ingesting. Without
isolation, every session-index test would write into the developer's REAL archive.
The fixture redirects ``XDG_DATA_HOME`` at a per-test temp dir and clears
``REKOL_ARCHIVE_DIR`` so a stray ambient override never leaks in; tests that need
to assert against the archive path set ``REKOL_ARCHIVE_DIR`` explicitly.

Tests that need the resolved cache path import ``cache_dir_for`` from
``cache_helpers`` (tests/ is on ``sys.path`` via pyproject's pytest config).
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_index_cache(tmp_path_factory, monkeypatch) -> None:
    """Point the index cache + transcript archive at throwaway dirs for every
    test, and clear the ambient data-home env so it cannot pollute a test that
    sets its own home.

    Autouse so no test can accidentally write the index (incl. the
    secrets-bearing ``sessions.db``) into the real ``~/.cache``, nor the
    default-ON transcript archive (verbatim, potentially secret-bearing) into the
    real ``~/.local/share/rekol/archive``, and so a developer's exported
    ``REKOL_HOME`` never silently overrides a test's home.
    """
    cache_root = tmp_path_factory.mktemp("xdg-cache")
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_root))
    monkeypatch.delenv("REKOL_INDEX_DIR", raising=False)
    # Hermetic archive: redirect XDG_DATA_HOME (where the default archive lives)
    # at a throwaway dir and clear the ambient REKOL_ARCHIVE_DIR override, so a
    # default-ON session-index run never writes the real machine archive. Tests
    # that assert the archive path set REKOL_ARCHIVE_DIR explicitly (it wins).
    data_root = tmp_path_factory.mktemp("xdg-data")
    monkeypatch.setenv("XDG_DATA_HOME", str(data_root))
    monkeypatch.delenv("REKOL_ARCHIVE_DIR", raising=False)
    # Hermetic data-home: clear the ambient vars so a test that sets only
    # MEMORY_HOME isn't shadowed by an exported REKOL_HOME (which has precedence).
    monkeypatch.delenv("REKOL_HOME", raising=False)
    monkeypatch.delenv("MEMORY_HOME", raising=False)
    # Hermetic Claude config tree. `claude_projects_dir` defaults to
    # <CLAUDE_CONFIG_DIR>/projects, so without this any test that calls
    # load_config() without setting it explicitly reads the DEVELOPER'S REAL
    # transcript store — thousands of files, and whatever is in them. That leak
    # was invisible while "no session index" was graded INFO; #165 made the grade
    # depend on whether transcripts exist, which turned it into a failing test in
    # test_doctor_deep.py and revealed that ~10 other test files had the same
    # exposure. Redirecting the whole tree fixes the class in one place; a test
    # that needs a populated projects dir points claude_projects_dir at its own.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path_factory.mktemp("claude-config")))
    monkeypatch.delenv("CLAUDE_SETTINGS_PATH", raising=False)
