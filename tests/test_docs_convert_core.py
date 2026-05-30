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


def test_convert_tree_counts_too_large_files(tmp_path: Path) -> None:
    src = tmp_path / "src"; src.mkdir()
    (src / "S").mkdir()
    (src / "S" / "big.md").write_text("x" * 200)
    stats = convert_tree(src, tmp_path / "out", prefix="p", max_bytes=10)
    assert stats.files_skipped_too_large == 1
    assert stats.files_converted == 0


def test_convert_tree_multi_session_and_root(tmp_path: Path) -> None:
    src = tmp_path / "src"; src.mkdir()
    (src / "loose.md").write_text("root level")          # _root session
    (src / "A").mkdir(); (src / "A" / "a.md").write_text("aaa")
    (src / "B").mkdir(); (src / "B" / "b.md").write_text("bbb")
    stats = convert_tree(src, tmp_path / "out", prefix="p", max_bytes=10_000)
    assert stats.jsonl_written == 3   # _root + A + B
    assert stats.files_converted == 3


def test_convert_stats_as_line_format() -> None:
    line = ConvertStats(folders_seen=2, jsonl_written=1, files_converted=1).as_line()
    assert line == ("folders_seen=2 jsonl_written=1 files_converted=1 "
                    "files_skipped_unsupported=0 files_skipped_empty=0 "
                    "files_skipped_too_large=0 errors=0")
