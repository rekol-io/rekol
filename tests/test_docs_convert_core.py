"""Tests for the convert_tree orchestration + ConvertStats accounting."""
from __future__ import annotations

from pathlib import Path

from memory_tools.docs_convert import convert_tree, ConvertStats


def _tree(root: Path) -> None:
    (root / "Topic A").mkdir(parents=True)
    (root / "Topic A" / "note.md").write_text("alpha body")
    (root / "Topic A" / "blank.txt").write_text("   \n")     # empty → skipped
    (root / "Topic A" / "sheet.xlsx").write_bytes(b"PK")     # unsupported → skipped
    (root / "Empty").mkdir()                                  # no text → no jsonl


def test_convert_tree_writes_and_reports_stats(tmp_path: Path) -> None:
    src = tmp_path / "src"; src.mkdir()
    _tree(src)
    out = tmp_path / "projects"
    stats = convert_tree(src, out, prefix="arc", max_bytes=10_000)
    assert isinstance(stats, ConvertStats)
    assert stats.folders_seen == 2            # "Topic A" + "Empty"
    assert stats.jsonl_written == 1           # only "Topic A"
    assert stats.files_converted == 1         # note.md
    assert stats.files_skipped_empty == 1     # blank.txt
    assert stats.files_skipped_unsupported == 1  # sheet.xlsx
    assert (out / "arc" / "Topic-A.jsonl").exists()


def test_convert_tree_dry_run_writes_nothing(tmp_path: Path) -> None:
    src = tmp_path / "src"; src.mkdir()
    _tree(src)
    out = tmp_path / "projects"
    stats = convert_tree(src, out, prefix="arc", max_bytes=10_000, dry_run=True)
    assert stats.jsonl_written == 1           # reports what WOULD be written
    assert not (out / "arc").exists()         # but nothing on disk
