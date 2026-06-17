"""Tests for scripts/bump_version.py (#102 — automated patch bump, skip on minor).

Covers the pure logic (parse, patch bump, series compare), the drift guard, the
file round-trip (one line changed, everything else preserved), and the CLI surface
(--check, --set, --baseline-ref skip) with the module's file paths redirected at temp
copies so the real version files are never touched.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import bump_version as bv  # noqa: E402


def _write_pair(tmp_path: Path, version: str, init_version: str | None = None) -> tuple[Path, Path]:
    """Create temp pyproject.toml + __init__.py at the given version(s)."""
    py = tmp_path / "pyproject.toml"
    py.write_text(
        "[build-system]\n"
        'requires = ["setuptools>=77", "wheel"]\n\n'
        "[project]\n"
        'name = "rekol"\n'
        f'version = "{version}"\n'
        'requires-python = ">=3.11"\n'
    )
    init = tmp_path / "__init__.py"
    init.write_text(f'"""doc."""\n\n__version__ = "{init_version or version}"\n')
    return py, init


# --------------------------- pure version logic ---------------------------


def test_parse_and_str_roundtrip() -> None:
    assert str(bv.Version.parse("0.1.13")) == "0.1.13"
    assert bv.Version.parse("0.1.13") == bv.Version(0, 1, 13)


@pytest.mark.parametrize("bad", ["0.1", "0.1.x", "1.2.3.4", "", "v0.1.2"])
def test_parse_rejects_non_xyz(bad: str) -> None:
    with pytest.raises(bv.VersionError):
        bv.Version.parse(bad)


def test_bumped_patch() -> None:
    assert bv.Version.parse("0.1.13").bumped_patch() == bv.Version(0, 1, 14)
    assert bv.Version.parse("0.1.9").bumped_patch() == bv.Version(0, 1, 10)


def test_decide_bumps_patch_without_baseline() -> None:
    assert bv.decide(bv.Version(0, 1, 13), None) == bv.Version(0, 1, 14)


def test_decide_bumps_patch_when_series_unchanged() -> None:
    # baseline same (major, minor) → ordinary PR → bump patch
    assert bv.decide(bv.Version(0, 1, 13), bv.Version(0, 1, 11)) == bv.Version(0, 1, 14)


def test_decide_skips_when_minor_changed() -> None:
    # a deliberate minor release (0.1.x -> 0.2.0) must NOT roll to 0.2.1
    assert bv.decide(bv.Version(0, 2, 0), bv.Version(0, 1, 13)) is None


def test_decide_skips_when_major_changed() -> None:
    assert bv.decide(bv.Version(1, 0, 0), bv.Version(0, 9, 4)) is None


# --------------------------- read / drift / write ---------------------------


def test_read_current_ok(tmp_path: Path) -> None:
    py, init = _write_pair(tmp_path, "0.1.13")
    assert bv.read_current(py, init) == bv.Version(0, 1, 13)


def test_read_current_detects_drift(tmp_path: Path) -> None:
    py, init = _write_pair(tmp_path, "0.1.13", init_version="0.1.12")
    with pytest.raises(bv.VersionError, match="drift"):
        bv.read_current(py, init)


def test_extract_missing_line_raises(tmp_path: Path) -> None:
    bad = tmp_path / "pyproject.toml"
    bad.write_text("[project]\nname = 'rekol'\n")  # no version line
    with pytest.raises(bv.VersionError, match="no version line"):
        bv._extract(bad.read_text(), bv._PYPROJECT_RE, str(bad))


def test_write_version_changes_only_the_version_line(tmp_path: Path) -> None:
    py, init = _write_pair(tmp_path, "0.1.13")
    bv.write_version(bv.Version(0, 1, 14), py, init)
    assert bv.read_current(py, init) == bv.Version(0, 1, 14)
    # surrounding content preserved
    py_text = py.read_text()
    assert 'name = "rekol"' in py_text
    assert 'requires-python = ">=3.11"' in py_text
    assert 'requires = ["setuptools>=77", "wheel"]' in py_text
    assert '"""doc."""' in init.read_text()


def test_write_version_anchored_does_not_touch_dependency_specs(tmp_path: Path) -> None:
    # A line that merely contains the word "version" elsewhere must be left alone.
    py = tmp_path / "pyproject.toml"
    py.write_text('[project]\nversion = "0.1.13"\ndescription = "version-aware tool"\n')
    init = tmp_path / "__init__.py"
    init.write_text('__version__ = "0.1.13"\n')
    bv.write_version(bv.Version(0, 1, 14), py, init)
    assert 'description = "version-aware tool"' in py.read_text()
    assert 'version = "0.1.14"' in py.read_text()


# --------------------------- CLI surface ---------------------------


@pytest.fixture
def redirected(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    py, init = _write_pair(tmp_path, "0.1.13")
    monkeypatch.setattr(bv, "PYPROJECT", py)
    monkeypatch.setattr(bv, "INIT_PY", init)
    return py, init


def test_cli_bumps_patch(redirected, capsys) -> None:
    py, init = redirected
    assert bv.main([]) == 0
    assert bv.read_current(py, init) == bv.Version(0, 1, 14)
    assert "0.1.13 -> 0.1.14" in capsys.readouterr().out


def test_cli_check_writes_nothing(redirected, capsys) -> None:
    py, init = redirected
    assert bv.main(["--check"]) == 0
    assert bv.read_current(py, init) == bv.Version(0, 1, 13)  # unchanged
    assert "would bump: 0.1.13 -> 0.1.14" in capsys.readouterr().out


def test_cli_set_explicit(redirected, capsys) -> None:
    py, init = redirected
    assert bv.main(["--set", "0.2.0"]) == 0
    assert bv.read_current(py, init) == bv.Version(0, 2, 0)


def test_cli_baseline_skip_on_minor(redirected, monkeypatch, capsys) -> None:
    py, init = redirected
    # Current is 0.2.0 (a minor release in this PR); baseline main is 0.1.13.
    bv.write_version(bv.Version(0, 2, 0), py, init)
    monkeypatch.setattr(bv, "_version_at_ref", lambda ref, pyproject=py: bv.Version(0, 1, 13))
    assert bv.main(["--baseline-ref", "origin/main"]) == 0
    # skipped → stays 0.2.0, not 0.2.1
    assert bv.read_current(py, init) == bv.Version(0, 2, 0)
    assert "skip" in capsys.readouterr().out


def test_cli_baseline_bumps_when_series_unchanged(redirected, monkeypatch) -> None:
    py, init = redirected  # current 0.1.13
    monkeypatch.setattr(bv, "_version_at_ref", lambda ref, pyproject=py: bv.Version(0, 1, 11))
    assert bv.main(["--baseline-ref", "origin/main"]) == 0
    assert bv.read_current(py, init) == bv.Version(0, 1, 14)


def test_cli_drift_errors(tmp_path: Path, monkeypatch, capsys) -> None:
    py, init = _write_pair(tmp_path, "0.1.13", init_version="0.1.12")
    monkeypatch.setattr(bv, "PYPROJECT", py)
    monkeypatch.setattr(bv, "INIT_PY", init)
    assert bv.main([]) == 1
    assert "drift" in capsys.readouterr().err
