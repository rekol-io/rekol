"""docs_convert honors an explicit text-extension set (the --include/--exclude basis)."""

from __future__ import annotations

from pathlib import Path

from rekol.docs_convert import TEXT_EXTENSIONS
from rekol.docs_convert.extract import is_text_native
from rekol.docs_convert.walk import group_sessions


def test_default_extensions_exclude_html(tmp_path: Path) -> None:
    assert is_text_native(tmp_path / "a.md") is True
    assert is_text_native(tmp_path / "a.html") is False


def test_explicit_set_includes_html(tmp_path: Path) -> None:
    exts = TEXT_EXTENSIONS | {"html"}
    assert is_text_native(tmp_path / "a.html", text_extensions=exts) is True


def test_group_sessions_threads_extension_set(tmp_path: Path) -> None:
    child = tmp_path / "session1"
    child.mkdir()
    (child / "note.html").write_text("<p>hi</p>", encoding="utf-8")
    # Default set drops .html → no groups
    assert group_sessions(tmp_path) == []
    # Explicit set including html → one group with the file
    groups = group_sessions(tmp_path, text_extensions=TEXT_EXTENSIONS | {"html"})
    assert len(groups) == 1
    assert groups[0].files[0].path.name == "note.html"
