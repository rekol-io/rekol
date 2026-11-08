"""The suite's own isolation guard, tested (#224, Codex + product).

`conftest.py` holds two halves of one promise: an autouse fixture whose patches
no test can revert, and a `pytest_runtest_call` wrapper that fails any test left
pointing a steering var at a real directory. Both exist because Codex's sandbox
caught a migrate test writing into Leon's real `~/Dropbox/memory/knowledge/`.

A guard nobody tests is a guard nobody can trust, and this one has already been
wrong twice in ways that only showed up when it was run: as a fixture it passed
on the very case it was written for, and before `expanduser` it let
`REKOL_INDEX_DIR='~'` through.

HOW THESE TESTS AVOID TRIPPING THE GUARD THEY TEST. They call
`_real_data_offenders()` directly inside a `pytest.MonkeyPatch.context()`, which
restores the variable before the hook wrapper looks. That is exactly the blind
spot the guard's own docstring now admits to — a value set and restored inside a
context is invisible to it — so the thing that makes these tests possible is the
same limitation product asked to be stated plainly. Closing it means checking at
resolution time, which is the filed follow-up.
"""

from __future__ import annotations

import os

import pytest

from conftest import _real_data_offenders


def test_a_tilde_that_the_code_expands_to_the_real_home_is_caught() -> None:
    """Codex's canary. The raw string neither exists nor sits under tmp.

    `REKOL_INDEX_DIR='~'` was skipped twice over by the first version — `~` does
    not resolve under tmp and `Path('~').exists()` is False — while `config.py`
    expanded it with `os.path.expanduser` and wrote to the real home. The guard
    now resolves each value the way the code does before testing it.
    """
    with pytest.MonkeyPatch.context() as patched:
        patched.setenv("REKOL_INDEX_DIR", "~")
        offenders = _real_data_offenders()
    assert any("REKOL_INDEX_DIR" in o for o in offenders), offenders
    # And it SHOWS the expansion, so the report names the directory that would
    # have been written rather than the string that looked harmless.
    assert any("resolves to" in o for o in offenders), offenders


def test_an_existing_real_directory_is_caught() -> None:
    """The ordinary case: a steering var pointing somewhere that exists."""
    with pytest.MonkeyPatch.context() as patched:
        patched.setenv("REKOL_HOME", os.path.expanduser("~"))
        offenders = _real_data_offenders()
    assert any("REKOL_HOME" in o for o in offenders), offenders


def test_a_sentinel_that_points_at_nothing_is_left_alone() -> None:
    """The anti-wolf control, and it is not hypothetical.

    Without the existence check this guard failed seven healthy tests:
    `test_rekol_home_takes_precedence` sets `REKOL_HOME=/a` and
    `MEMORY_HOME=/b`, and the shipped-frontmatter tests set `CLAUDE_CONFIG_DIR`
    to whitespace — deliberate sentinels for resolution order, pointing at
    nothing and writing nothing. A guard that fires on those is one somebody
    deletes, so it must stay quiet here.
    """
    with pytest.MonkeyPatch.context() as patched:
        patched.setenv("REKOL_HOME", "/a")
        patched.setenv("MEMORY_HOME", "/b")
        patched.setenv("CLAUDE_CONFIG_DIR", "   ")
        offenders = _real_data_offenders()
    assert offenders == [], offenders


def test_a_tmp_destination_is_left_alone(tmp_path) -> None:
    """The healthy path every other test in this suite takes."""
    with pytest.MonkeyPatch.context() as patched:
        patched.setenv("REKOL_HOME", str(tmp_path / "home"))
        patched.setenv("REKOL_INDEX_DIR", str(tmp_path / "cache"))
        (tmp_path / "home").mkdir()
        (tmp_path / "cache").mkdir()
        offenders = _real_data_offenders()
    assert offenders == [], offenders


def test_the_isolation_fixture_cannot_be_undone_by_a_test(monkeypatch) -> None:
    """The other half: `monkeypatch.undo()` must not restore the ambient home.

    Thirteen tests in this suite call `undo()`. Before the fix conftest shared
    the function-scoped `monkeypatch`, so each of those reverted its `delenv`
    calls and the developer's real `REKOL_HOME` came back mid-test. conftest now
    uses a private `pytest.MonkeyPatch`, out of reach of the code under test.
    """
    monkeypatch.setattr(os, "sep", os.sep)  # something for undo() to revert
    monkeypatch.undo()
    # conftest cleared these; they must still be clear.
    for var in ("REKOL_HOME", "MEMORY_HOME", "REKOL_TOOLS_HOME"):
        assert var not in os.environ, f"{var} came back after monkeypatch.undo()"
    # And the redirected ones must still point into tmp.
    assert _real_data_offenders() == [], _real_data_offenders()
