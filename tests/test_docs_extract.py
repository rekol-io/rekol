"""Tests for docs_convert.extract — text passthrough, empty/oversize/bad-encoding guards."""

from __future__ import annotations

from pathlib import Path

from memory_tools.docs_convert.extract import extract_text, is_text_native


def test_is_text_native_recognises_text_extensions() -> None:
    assert is_text_native(Path("a.md"))
    assert is_text_native(Path("a.JSON"))  # case-insensitive
    assert is_text_native(Path("a.csv"))


def test_is_text_native_rejects_binary_and_jsonl() -> None:
    assert not is_text_native(Path("a.xlsx"))
    assert not is_text_native(Path("a.png"))
    assert not is_text_native(Path("a.html"))
    # .jsonl is deliberately skipped: extension collides with our output format
    assert not is_text_native(Path("a.jsonl"))
    # extensionless files (e.g. Makefile) have no suffix → not text-native
    assert not is_text_native(Path("Makefile"))


def test_extract_text_passes_through_plain_text(tmp_path: Path) -> None:
    f = tmp_path / "note.md"
    f.write_text("hello world\n\nsecond para")
    assert extract_text(f, max_bytes=10_000) == "hello world\n\nsecond para"


def test_extract_text_returns_none_for_whitespace_only(tmp_path: Path) -> None:
    f = tmp_path / "blank.txt"
    f.write_text("   \n\t  \n")
    assert extract_text(f, max_bytes=10_000) is None


def test_extract_text_returns_none_when_over_max_bytes(tmp_path: Path) -> None:
    f = tmp_path / "big.log"
    f.write_text("x" * 5000)
    assert extract_text(f, max_bytes=1000) is None


def test_extract_text_returns_none_for_missing_file(tmp_path: Path) -> None:
    assert extract_text(tmp_path / "ghost.txt", max_bytes=10_000) is None


def test_extract_text_replaces_bad_encoding(tmp_path: Path) -> None:
    f = tmp_path / "weird.txt"
    f.write_bytes(b"good \xff\xfe bytes")
    out = extract_text(f, max_bytes=10_000)
    assert out is not None
    assert "good" in out and "bytes" in out  # replacement chars tolerated, no crash
