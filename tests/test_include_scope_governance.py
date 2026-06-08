"""Deny-list governance: which files under an included dir are in scope.

The deny-list is what reconciles "user picks" with "memory accumulates": a dir
is included once, then on every run the deny globs (plus the always-on junk
filter) decide which files index. These RED tests pin that governance.
"""

from __future__ import annotations

from pathlib import Path

from rekol.include_scope import (
    file_in_scope,
    iter_scoped_files,
)


def _touch(path: Path, name: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    f = path / name
    f.write_text("content\n", encoding="utf-8")
    return f


def test_file_in_scope_with_no_deny(tmp_path: Path) -> None:
    f = _touch(tmp_path / "papers", "h.md")
    assert file_in_scope(f, tmp_path, deny=[])


def test_deny_glob_excludes_matching_file(tmp_path: Path) -> None:
    keep = _touch(tmp_path / "papers", "keep.md")
    drop = _touch(tmp_path / "papers" / "old-projects", "drop.md")
    assert file_in_scope(keep, tmp_path, deny=["old-projects"])
    assert not file_in_scope(drop, tmp_path, deny=["old-projects"])


def test_deny_bare_segment_excludes_whole_subtree(tmp_path: Path) -> None:
    # "exclude a folder -> everything under it out" (spec: Custom exclude-list).
    drop = _touch(tmp_path / "a" / "vendored" / "deep", "x.md")
    assert not file_in_scope(drop, tmp_path, deny=["vendored"])


def test_junk_is_denied_even_without_explicit_deny(tmp_path: Path) -> None:
    # The auto-junk-filter governs scope independently of the user deny-list.
    junk = _touch(tmp_path / "repo" / "node_modules" / "pkg", "j.md")
    assert not file_in_scope(junk, tmp_path, deny=[])


def test_iter_scoped_files_yields_only_in_scope_text(tmp_path: Path) -> None:
    _touch(tmp_path / "papers", "a.md")
    _touch(tmp_path / "papers" / "skipme", "b.md")
    _touch(tmp_path / "node_modules", "junk.md")
    _touch(tmp_path / "papers", "image.png")  # non-text extension dropped
    scoped = sorted(p.name for p in iter_scoped_files(tmp_path, deny=["skipme"]))
    assert scoped == ["a.md"]


def test_iter_scoped_files_respects_cap(tmp_path: Path) -> None:
    for i in range(30):
        _touch(tmp_path / f"d{i}", "n.md")
    scoped = list(iter_scoped_files(tmp_path, deny=[], max_files=5))
    assert len(scoped) <= 5


def test_iter_scoped_files_missing_dir_is_empty(tmp_path: Path) -> None:
    assert list(iter_scoped_files(tmp_path / "nope", deny=[])) == []
