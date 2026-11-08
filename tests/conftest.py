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

import os
import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_index_cache(tmp_path_factory) -> None:
    """Point the index cache + transcript archive at throwaway dirs for every
    test, and clear the ambient data-home env so it cannot pollute a test that
    sets its own home.

    Autouse so no test can accidentally write the index (incl. the
    secrets-bearing ``sessions.db``) into the real ``~/.cache``, nor the
    default-ON transcript archive (verbatim, potentially secret-bearing) into the
    real ``~/.local/share/rekol/archive``, and so a developer's exported
    ``REKOL_HOME`` never silently overrides a test's home.

    ITS OWN ``MonkeyPatch``, NOT THE ``monkeypatch`` FIXTURE, and that is the
    whole point (Codex + product, 2026-11-07). This used to request the
    function-scoped ``monkeypatch``, which a test shares — so a test calling
    ``monkeypatch.undo()`` to drop one ``setattr`` of its own ALSO undid every
    line below, restoring the developer's real ``REKOL_HOME``. Codex's sandbox
    caught a migrate test doing exactly that and then running the CLI, which
    tried to write into Leon's real memory home; a local run has no sandbox and
    would have succeeded.
    Many tests in this suite call ``undo()`` (21 occurrences across 4 files). Converting all of them would
    leave the next one free to do it again: **isolation must not be revertible
    by the code it isolates.** A private instance, undone in teardown, is what
    makes that true, and it fixes every site at once.
    """
    monkeypatch = pytest.MonkeyPatch()
    # The try starts HERE, before the first patch. It used to open further down,
    # so an exception from `mktemp` or an early `setenv` left the patches already
    # applied with no teardown to undo them (review, 2026-11-08).
    try:
        _apply_isolation(monkeypatch, tmp_path_factory)
        yield
    finally:
        monkeypatch.undo()


def _apply_isolation(monkeypatch: pytest.MonkeyPatch, tmp_path_factory) -> None:  # noqa: ANN001
    """Point every destination that steers rekol at a throwaway dir."""
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
    # REKOL_TOOLS_HOME too, found by the guard below on its first run: it was
    # never cleared, so on any machine with rekol installed every test ran with
    # it pointing at the real `~/.local/share/rekol`. Nothing in the pytest
    # suite writes there today, which is why it went unnoticed — but it is a
    # var that steers rekol at real data, and the suite is supposed to be
    # hermetic rather than hermetic-where-it-happens-to-matter.
    monkeypatch.delenv("REKOL_TOOLS_HOME", raising=False)
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


# The env vars this guard covers. NOT "every variable that can steer rekol" —
# that is what this comment used to claim, and a review disproved it (2026-11-08):
#   * `CLAUDE_SETTINGS_PATH` is read by `cli_resume.py` and points rekol at a
#     settings.json. It is NOT covered: the code reads it raw, with no
#     `expanduser`, so resolving it here as the others are resolved would flag a
#     `~` value the code would never expand. Covering it needs its own rule (#255).
#   * `REKOL_TOOLS_HOME` IS listed and nothing under `src/` reads it — it is
#     `install.sh`'s. So this is a reviewed set, not one derived from the code,
#     and it has to be revisited whenever a new destination appears.
_HOME_STEERING_VARS = (
    "REKOL_HOME",
    "MEMORY_HOME",
    "REKOL_INDEX_DIR",
    "REKOL_ARCHIVE_DIR",
    "REKOL_TOOLS_HOME",
    "XDG_CACHE_HOME",
    "XDG_DATA_HOME",
    "CLAUDE_CONFIG_DIR",
)


def _as_the_code_resolves_it(value: str) -> Path:
    """Expand ``value`` the way rekol does before it uses it as a destination.

    ``os.path.expanduser`` is what `config.py` applies to every one of these —
    `REKOL_HOME`, `MEMORY_HOME`, the `REKOL_INDEX_DIR` override,
    `XDG_CACHE_HOME`, the archive dir. Checking the RAW string instead let
    `REKOL_INDEX_DIR='~'` through: the literal `~` neither resolves under tmp
    nor exists, so it was skipped twice over, while the code expanded it to the
    real home and wrote there (Codex, 2026-11-07). A guard has to resolve a
    value the same way the code it guards does, or it is checking a different
    string from the one that gets used.
    """
    return Path(os.path.expanduser(value))


def _is_under_a_temp_root(value: str) -> bool:
    """Is ``value`` inside a directory pytest or the OS hands out for scratch?"""
    try:
        resolved = _as_the_code_resolves_it(value).resolve()
    except (OSError, ValueError):
        return False
    # `tempfile.gettempdir()`, not `os.environ.get("TMPDIR", "/tmp")`: a
    # set-but-EMPTY TMPDIR makes that `get` return "", and `Path("").resolve()`
    # is the CWD — so the repo root would count as a temp root and every path
    # inside it would be exempt. gettempdir ignores an empty TMPDIR (verified).
    roots = [Path(tempfile.gettempdir()), Path("/tmp"), Path("/private/tmp")]
    for root in roots:
        try:
            resolved.relative_to(root.resolve())
        except (ValueError, OSError):
            continue
        return True
    return False


def _real_data_offenders() -> list[str]:
    """Every steering var pointing at something that EXISTS outside tmp.

    EXISTENCE IS THE DISCRIMINATOR, and leaving it out made this guard fire on
    seven healthy tests the first time it ran. `test_rekol_home_takes_precedence`
    sets `REKOL_HOME=/a` and `MEMORY_HOME=/b`, and the shipped-frontmatter tests
    set `CLAUDE_CONFIG_DIR` to whitespace — deliberate sentinels for resolution
    order, pointing at nothing, writing nothing. A guard that fires on those is
    one someone deletes, which is worse than no guard at all.
    A path that does not exist cannot be real data. One that DOES exist outside
    tmp is the dangerous case, and that is every variable this was written for:
    `~/Dropbox/memory`, `~/.cache/rekol/<hash>`, `~/.local/share/rekol/archive`.
    A test that points at a non-existent real path and then CREATES it is still
    caught, because this runs after the test body, when it exists.
    """
    offenders = []

    # FIRST, THE DESTINATIONS THE CODE WOULD ACTUALLY RESOLVE. This is the only
    # check that cannot drift, because it asks `config.py` where it would go
    # rather than re-deriving the expansion here — and re-deriving is what has
    # now been wrong twice. Codex caught `REKOL_INDEX_DIR='~'`, where the raw
    # string neither existed nor sat under tmp. Then a review found the same
    # class again: a set-but-EMPTY or whitespace value, which `config.py` treats
    # as unset and resolves to its REAL default, while the env scan below skips
    # it for being falsy. Measured, with a tmp REKOL_HOME set:
    #   XDG_CACHE_HOME=""      -> ~/.cache/rekol/<hash>          (silent)
    #   REKOL_INDEX_DIR=""     -> ~/.cache/rekol/<hash>          (silent)
    #   XDG_DATA_HOME=""       -> ~/.local/share/rekol/archive   (silent)
    #   CLAUDE_CONFIG_DIR="  " -> ~/.claude/projects             (silent)
    # `load_config()` is read-only — it resolves paths and does not create them
    # — so calling it here cannot make the directory it is warning about.
    try:
        from rekol.config import load_config

        cfg = load_config()
    except Exception:  # noqa: BLE001 — no home configured is the ordinary case
        cfg = None
    if cfg is not None:
        for label, dest in (
            ("memory_home", cfg.memory_home),
            ("index_dir", cfg.index_dir),
            ("archive_dir", cfg.archive_dir),
            ("claude_projects_dir", cfg.claude_projects_dir),
        ):
            text = str(dest)
            if _is_under_a_temp_root(text):
                continue
            try:
                if not Path(text).exists():
                    continue
            except OSError:
                continue
            offenders.append(f"{label} resolves to {text}")

    # THEN the raw env scan, kept as a second layer: it still catches a variable
    # pointing somewhere real when `load_config()` cannot run at all (no home
    # configured), which is most of this suite.
    for var in _HOME_STEERING_VARS:
        value = os.environ.get(var)
        if not value or _is_under_a_temp_root(value):
            continue
        try:
            if not _as_the_code_resolves_it(value).exists():
                continue
        except OSError:
            continue
        shown = f"{var}={value}"
        expanded = _as_the_code_resolves_it(value)
        if str(expanded) != value:
            shown += f" (resolves to {expanded})"
        offenders.append(shown)
    return offenders


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):  # noqa: ANN001, ANN201
    """FAIL a test that leaves rekol pointed at a directory outside tmp.

    Asked for by product after Codex's sandbox caught a test writing to Leon's
    real memory home. The fixture above makes the isolation unrevertible; this
    checks the RESULT, because the two failures are different: one is
    "isolation was undone", the other is "a test set a real path on purpose, or
    read one from the ambient environment".

    A HOOK WRAPPER RATHER THAN A FIXTURE, and that was not the first attempt.
    As an autouse fixture it caught the undone-isolation case but NOT a test
    that set a real path deliberately, because the `monkeypatch` fixture the
    test itself requests is torn down FIRST — the env was already restored
    before the fixture looked, so the check passed on the very thing it was
    written for. A `pytest_runtest_call` wrapper runs immediately after the test
    body, while every fixture is still live, so it sees both.

    Measured, with conftest's private MonkeyPatch reverted to the shared one:
    a test calling `monkeypatch.undo()` leaks `MEMORY_HOME=~/Dropbox/memory`,
    `REKOL_INDEX_DIR=~/.cache/rekol/<hash>` and
    `REKOL_ARCHIVE_DIR=~/.local/share/rekol/archive` — the real memory home and
    the real transcript archive. That is what Codex's sandbox stopped, and a
    local run has no sandbox.

    WHAT THIS IS NOT, stated because the first version of this docstring implied
    more (product, 2026-11-07). It is a **post-test check of configured
    destinations**, not a write barrier: it runs after the test body and reports
    where the test was POINTED, so a test that writes outside tmp and then puts
    the variable back is not caught. In particular a value set and restored
    inside a `pytest.MonkeyPatch.context()` is invisible to it, because the
    context exits before this looks.
    Checking at resolution time instead — refusing inside `load_config()` when
    the destination is outside tmp under pytest — is the version that would
    catch those. That is **#255**, filed after a review pointed out this comment
    claimed it had been filed when it had not. An `xfail` test can also absorb
    the AssertionError below as an expected failure, which is in #255 too.
    """
    outcome = yield
    offenders = _real_data_offenders()
    if offenders and outcome.excinfo is None:
        # Only when the test otherwise passed: a test that already failed has
        # its own error to show, and burying it under this one helps nobody.
        raise AssertionError(
            "this test left rekol pointed at a directory OUTSIDE tmp, which is a "
            "real directory on this machine: "
            + "; ".join(offenders)
            + ". Point every one of these at tmp_path, and never call "
            "`monkeypatch.undo()` — it reverts conftest's isolation along with "
            "your own patches."
        )
