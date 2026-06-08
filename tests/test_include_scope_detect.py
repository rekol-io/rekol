"""Discovery: detect candidate knowledge dirs with the auto-junk-filter.

RED-first tests for the riskiest surface of T8 (#63): the junk-filter must
hard-exclude build/vendor noise so "All" is clean, the scan must be capped so a
huge tree never hangs the onboarding flow, and the per-dir ``.md`` count is the
coverage signal the UI ranks on.
"""

from __future__ import annotations

from pathlib import Path

from rekol.include_scope import (
    JUNK_DIR_NAMES,
    DiscoveredDir,
    discover_knowledge_dirs,
)


def _md(path: Path, name: str = "a.md") -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / name).write_text("# note\n", encoding="utf-8")


def test_discovers_dirs_with_md_and_counts(tmp_path: Path) -> None:
    _md(tmp_path / "papers", "one.md")
    (tmp_path / "papers" / "two.md").write_text("# two\n", encoding="utf-8")
    _md(tmp_path / "logs", "log.md")
    found = discover_knowledge_dirs(tmp_path)
    by_path = {d.path: d for d in found}
    assert (tmp_path / "papers") in by_path
    assert by_path[tmp_path / "papers"].md_count == 2
    assert by_path[tmp_path / "logs"].md_count == 1


def test_ranked_most_md_first(tmp_path: Path) -> None:
    # Grouping is by the directory directly holding the files (the session-like
    # unit). The dir with the most .md ranks first — the coverage signal.
    small = tmp_path / "small"
    small.mkdir()
    (small / "a.md").write_text("x", encoding="utf-8")
    big = tmp_path / "big"
    big.mkdir()
    for i in range(5):
        (big / f"n{i}.md").write_text("x", encoding="utf-8")
    found = discover_knowledge_dirs(tmp_path)
    assert found[0].md_count >= found[-1].md_count
    assert found[0].path == big
    assert found[0].md_count == 5


def test_junk_dirs_are_hard_excluded(tmp_path: Path) -> None:
    # Each junk dir holds .md that must NOT be discovered.
    for junk in ("node_modules", ".git", "build", "dist", "vendor", "venv", ".venv"):
        _md(tmp_path / "project" / junk, "junk.md")
    _md(tmp_path / "project" / "docs", "real.md")
    found = discover_knowledge_dirs(tmp_path)
    found_paths = {d.path for d in found}
    assert (tmp_path / "project" / "docs") in found_paths
    for junk in JUNK_DIR_NAMES:
        assert not any(junk in d.path.parts for d in found), f"{junk} leaked into discovery"


def test_nested_junk_excludes_whole_subtree(tmp_path: Path) -> None:
    # .md buried deep inside node_modules must never surface.
    deep = tmp_path / "repo" / "node_modules" / "pkg" / "sub"
    _md(deep, "buried.md")
    found = discover_knowledge_dirs(tmp_path)
    assert all("node_modules" not in d.path.parts for d in found)


def test_scan_cap_bounds_the_walk(tmp_path: Path) -> None:
    # Many files; cap must stop the walk so a huge tree never hangs onboarding.
    for i in range(50):
        (tmp_path / f"d{i}").mkdir()
        (tmp_path / f"d{i}" / "n.md").write_text("x", encoding="utf-8")
    found = discover_knowledge_dirs(tmp_path, max_files=10)
    total = sum(d.md_count for d in found)
    # Never count more files than the cap allowed us to walk.
    assert total <= 10


def test_empty_or_missing_root_returns_empty(tmp_path: Path) -> None:
    assert discover_knowledge_dirs(tmp_path / "nope") == []
    (tmp_path / "empty").mkdir()
    assert discover_knowledge_dirs(tmp_path / "empty") == []


def test_dir_with_only_non_md_text_still_discovered_with_zero_md(tmp_path: Path) -> None:
    # A dir of .txt/.py knowledge is includable; md_count is 0 but text_count > 0.
    d = tmp_path / "notes"
    d.mkdir()
    (d / "a.txt").write_text("notes", encoding="utf-8")
    (d / "b.py").write_text("print()", encoding="utf-8")
    found = discover_knowledge_dirs(tmp_path)
    by_path = {x.path: x for x in found}
    assert d in by_path
    assert by_path[d].md_count == 0
    assert by_path[d].text_count == 2


def test_discovered_dir_is_sortable_dataclass(tmp_path: Path) -> None:
    a = DiscoveredDir(path=tmp_path / "a", md_count=1, text_count=1)
    b = DiscoveredDir(path=tmp_path / "b", md_count=2, text_count=2)
    assert a != b
