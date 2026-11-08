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
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import _is_under_a_temp_root, _real_data_offenders


def test_a_tilde_that_the_code_expands_to_the_real_home_is_caught() -> None:
    """Codex's canary. The raw string neither exists nor sits under tmp.

    `REKOL_INDEX_DIR='~'` was skipped twice over by the first version — `~` does
    not resolve under tmp and `Path('~').exists()` is False — while `config.py`
    expanded it with `os.path.expanduser` and wrote to the real home. The guard
    now resolves each value the way the code does before testing it.
    """
    if _is_under_a_temp_root(os.path.expanduser("~")):
        pytest.skip("HOME is inside a temp root, so `~` is not a real destination here")
    with pytest.MonkeyPatch.context() as patched:
        patched.setenv("REKOL_INDEX_DIR", "~")
        offenders = _real_data_offenders()
    assert any("REKOL_INDEX_DIR" in o for o in offenders), offenders
    # And it SHOWS the expansion, so the report names the directory that would
    # have been written rather than the string that looked harmless.
    assert any("resolves to" in o for o in offenders), offenders


def _a_real_directory_outside_tmp() -> str | None:
    """A directory that exists and is NOT under a temp root, or None.

    `os.path.expanduser("~")` was used here directly, and a review showed that
    breaks under the very setup this guard exists for: a sandbox that puts HOME
    inside a temp dir (which is Codex's) makes the home a TEMP path, so the
    "caught" assertion fails for the right reason and reads as a regression.
    Walking up from the filesystem root instead gives a directory that is real
    on any machine, and the test skips rather than lies if none can be found.
    """
    for candidate in ("/usr", "/etc", "/var", os.path.expanduser("~")):
        if os.path.isdir(candidate) and not _is_under_a_temp_root(candidate):
            return candidate
    return None


def test_an_existing_real_directory_is_caught() -> None:
    """The ordinary case: a steering var pointing somewhere that exists."""
    real = _a_real_directory_outside_tmp()
    if real is None:
        pytest.skip("no directory outside a temp root on this machine")
    with pytest.MonkeyPatch.context() as patched:
        patched.setenv("REKOL_HOME", real)
        offenders = _real_data_offenders()
    assert any("REKOL_HOME" in o or "memory_home" in o for o in offenders), offenders


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
        offenders = _real_data_offenders()
    assert offenders == [], offenders


def test_a_whitespace_value_that_falls_back_to_a_real_default_is_caught() -> None:
    """`CLAUDE_CONFIG_DIR="   "` is NOT a harmless sentinel, and I had it wrong.

    This test originally asserted the opposite — it bundled whitespace in with
    `/a` and `/b` as a sentinel pointing at nothing. A review (2026-11-08) showed
    that `config.py` treats a blank value as UNSET and falls back to the real
    default, so the code reads `~/.claude/projects` while the old guard skipped
    the variable for being falsy. Measured, with a tmp `REKOL_HOME` set:

        XDG_CACHE_HOME=""      -> ~/.cache/rekol/<hash>
        REKOL_INDEX_DIR=""     -> ~/.cache/rekol/<hash>
        XDG_DATA_HOME=""       -> ~/.local/share/rekol/archive
        CLAUDE_CONFIG_DIR="  " -> ~/.claude/projects

    Same class as the `~` Codex found: the guard inspected a different string
    from the one the code resolves. It now asks `config.py` for the destination
    instead, which is why this is caught and why `/a` above still is not.
    """
    with pytest.MonkeyPatch.context() as patched:
        patched.setenv("REKOL_HOME", os.environ.get("REKOL_HOME") or "/a")
        patched.setenv("CLAUDE_CONFIG_DIR", "   ")
        offenders = _real_data_offenders()
    assert any("claude_projects_dir" in o for o in offenders), offenders
    # Reported as the RESOLVED destination, not the blank string, so the message
    # names the directory at risk.
    assert any(".claude" in o for o in offenders), offenders


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

    # ASSERTING PRESENCE, NOT ABSENCE, and that is the whole point of this fix.
    # A review mutated conftest back to the shared `monkeypatch` fixture and this
    # test still passed 5/5 in a clean environment (2026-11-08): the fixture's
    # `delenv` records are undone to "absent", which is exactly what the old
    # assertions below checked for. It only went red on a machine that exports
    # REKOL_HOME — so it caught the regression on my laptop and never in CI.
    #
    # The variables conftest SETS are the ones that cannot be faked by an empty
    # environment: after a correct `undo()` by a test, they must still be set and
    # still point into tmp.
    for var in ("XDG_CACHE_HOME", "XDG_DATA_HOME", "CLAUDE_CONFIG_DIR"):
        value = os.environ.get(var)
        assert value, f"{var} was unset by monkeypatch.undo() — isolation was reverted"
        assert _is_under_a_temp_root(value), f"{var}={value} no longer points into tmp"
    # The cleared ones must still be clear.
    for var in ("REKOL_HOME", "MEMORY_HOME", "REKOL_TOOLS_HOME"):
        assert var not in os.environ, f"{var} came back after monkeypatch.undo()"
    assert _real_data_offenders() == [], _real_data_offenders()


def test_an_empty_TMPDIR_does_not_make_the_cwd_a_temp_root() -> None:
    """A set-but-empty `TMPDIR` must not exempt the whole working directory.

    `os.environ.get("TMPDIR", "/tmp")` returns `""` for a set-but-empty value,
    and `Path("").resolve()` is the CWD — so the repo root counted as a temp
    root and every path inside it was exempt from the guard (review,
    2026-11-08). `tempfile.gettempdir()` ignores an empty TMPDIR.
    """
    with pytest.MonkeyPatch.context() as patched:
        patched.setenv("TMPDIR", "")
        assert not _is_under_a_temp_root(os.getcwd()), (
            "the working directory counts as a temp root, so nothing inside it is guarded"
        )
        # And a genuine temp path is still recognised with TMPDIR blank.
        assert _is_under_a_temp_root("/private/tmp")


def test_the_hook_wrapper_really_fails_an_offending_test(tmp_path: Path) -> None:
    """THE ENFORCEMENT HALF, which nothing exercised until now.

    Every other test here calls `_real_data_offenders()` directly, so the hook
    wrapper's `raise AssertionError(...)` and its `outcome.excinfo is None` gate
    were untested — a review mutated the raise to `if False:` and this file still
    passed 5/5 (2026-11-08). The helper computing the right answer is not the
    same claim as the suite actually failing.

    So this runs an INNER pytest session, in a subprocess with its own copy of
    `conftest.py`, and asserts the outer result is a failure. Subprocess rather
    than the `pytester` fixture because `pytester` needs the plugin enabled
    repo-wide, which is a bigger change than the thing being pinned.
    """
    real = _a_real_directory_outside_tmp()
    if real is None:
        pytest.skip("no directory outside a temp root on this machine")

    conftest_src = Path(__file__).parent / "conftest.py"
    (tmp_path / "conftest.py").write_text(conftest_src.read_text())
    (tmp_path / "test_offender.py").write_text(
        "import os\n"
        "def test_points_at_a_real_dir(monkeypatch):\n"
        f"    monkeypatch.setenv('REKOL_HOME', {real!r})\n"
        "    assert True   # the test body itself passes\n"
    )

    result = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_offender.py"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={k: v for k, v in os.environ.items() if not k.startswith("REKOL_")},
    )

    assert result.returncode != 0, (
        "the inner test passed, so the hook did not enforce anything:\n" + result.stdout
    )
    assert "OUTSIDE tmp" in result.stdout, result.stdout
    # And the body itself passed, so the failure came from the hook rather than
    # from the test — otherwise this would pass for the wrong reason.
    assert "1 failed" in result.stdout, result.stdout


def test_the_hook_does_not_mask_a_tests_own_failure(tmp_path: Path) -> None:
    """A test that fails on its own must keep ITS error, not get the guard's.

    The `outcome.excinfo is None` gate is what makes that true. Pinned here
    because the same mutation that disables the raise also disables the gate,
    and nothing distinguished the two.
    """
    real = _a_real_directory_outside_tmp()
    if real is None:
        pytest.skip("no directory outside a temp root on this machine")

    conftest_src = Path(__file__).parent / "conftest.py"
    (tmp_path / "conftest.py").write_text(conftest_src.read_text())
    (tmp_path / "test_both.py").write_text(
        "import os\n"
        "def test_fails_on_its_own(monkeypatch):\n"
        f"    monkeypatch.setenv('REKOL_HOME', {real!r})\n"
        "    assert 1 == 2, 'the test-s own assertion'\n"
    )

    result = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "test_both.py"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={k: v for k, v in os.environ.items() if not k.startswith("REKOL_")},
    )
    assert result.returncode != 0
    assert "the test-s own assertion" in result.stdout, (
        "the guard replaced the test's own failure:\n" + result.stdout
    )
