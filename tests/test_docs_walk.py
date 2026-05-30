"""Tests for docs_convert.walk — immediate-child grouping, recursive file collection."""
from __future__ import annotations

from pathlib import Path

from memory_tools.docs_convert.walk import group_sessions


def _make_tree(root: Path) -> None:
    # Child folder with files directly inside
    (root / "Topic A").mkdir(parents=True)
    (root / "Topic A" / "note1.md").write_text("alpha")
    (root / "Topic A" / "note2.txt").write_text("beta")
    # Child folder with DEEP nesting (must group under the immediate child)
    (root / "Security" / "scope" / "investigation").mkdir(parents=True)
    (root / "Security" / "scope" / "investigation" / "deep.json").write_text("{}")
    (root / "Security" / "top.csv").write_text("a,b")
    # Child folder with ONLY non-text files → no session
    (root / "Binaries").mkdir()
    (root / "Binaries" / "sheet.xlsx").write_bytes(b"PK\x03\x04")
    # A file directly under root → _root session
    (root / "loose.md").write_text("loose content")


def test_group_sessions_groups_by_immediate_child(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    groups = {g.session_name: g for g in group_sessions(tmp_path, max_bytes=10_000)}
    assert set(groups) == {"Topic A", "Security", "_root"}  # Binaries excluded


def test_group_sessions_collects_nested_files_under_one_session(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    groups = {g.session_name: g for g in group_sessions(tmp_path, max_bytes=10_000)}
    sec = groups["Security"]
    rels = sorted(f.rel_to_session for f in sec.files)
    assert rels == ["scope/investigation/deep.json", "top.csv"]


def test_group_sessions_root_session_for_loose_files(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    groups = {g.session_name: g for g in group_sessions(tmp_path, max_bytes=10_000)}
    root = groups["_root"]
    assert [f.rel_to_session for f in root.files] == ["loose.md"]


def test_group_sessions_excludes_jsonl_and_binary(tmp_path: Path) -> None:
    (tmp_path / "Mixed").mkdir()
    (tmp_path / "Mixed" / "keep.md").write_text("yes")
    (tmp_path / "Mixed" / "log.jsonl").write_text('{"x":1}')
    (tmp_path / "Mixed" / "img.png").write_bytes(b"\x89PNG")
    groups = {g.session_name: g for g in group_sessions(tmp_path, max_bytes=10_000)}
    assert [f.rel_to_session for f in groups["Mixed"].files] == ["keep.md"]


def test_group_sessions_rel_to_source_includes_session_folder(tmp_path: Path) -> None:
    _make_tree(tmp_path)
    groups = {g.session_name: g for g in group_sessions(tmp_path, max_bytes=10_000)}
    a_note = next(f for f in groups["Topic A"].files if f.rel_to_session == "note1.md")
    assert a_note.rel_to_source == "Topic A/note1.md"
