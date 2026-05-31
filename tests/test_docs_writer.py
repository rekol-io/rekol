"""Tests for docs_convert.writer — one .jsonl per session, clean overwrite."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rekol.docs_convert.writer import _safe_filename, write_sessions


def test_write_sessions_writes_one_jsonl_per_session(tmp_path: Path) -> None:
    rows_by_session = {
        "Topic A": [{"type": "user", "uuid": "u1", "message": {"content": "x"}}],
        "Security": [{"type": "user", "uuid": "u2", "message": {"content": "y"}}],
    }
    written = write_sessions(tmp_path, prefix="arc", rows_by_session=rows_by_session)
    target = tmp_path / "arc"
    assert (target / "Topic-A.jsonl").exists()
    assert (target / "Security.jsonl").exists()
    assert len(written) == 2


def test_write_sessions_rows_are_valid_jsonl(tmp_path: Path) -> None:
    rows_by_session = {
        "T": [
            {"type": "user", "uuid": "u1", "message": {"content": "a"}},
            {"type": "user", "uuid": "u2", "message": {"content": "b"}},
        ]
    }
    write_sessions(tmp_path, prefix="arc", rows_by_session=rows_by_session)
    lines = (tmp_path / "arc" / "T.jsonl").read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["uuid"] == "u1"
    assert json.loads(lines[1])["uuid"] == "u2"


def test_write_sessions_overwrites_prefix_dir_cleanly(tmp_path: Path) -> None:
    target = tmp_path / "arc"
    target.mkdir()
    stale = target / "OldTopic.jsonl"
    stale.write_text('{"stale": true}\n')
    write_sessions(
        tmp_path,
        prefix="arc",
        rows_by_session={"New": [{"type": "user", "uuid": "u", "message": {"content": "c"}}]},
    )
    assert not stale.exists()  # stale file removed
    assert (target / "New.jsonl").exists()


def test_safe_filename_sanitises_separators() -> None:
    assert _safe_filename("Topic A") == "Topic-A"
    assert _safe_filename("a/b\\c") == "a-b-c"
    assert _safe_filename("_root") == "_root"


def test_write_sessions_skips_empty_session(tmp_path: Path) -> None:
    written = write_sessions(tmp_path, prefix="arc", rows_by_session={"Empty": []})
    assert written == []
    assert not (tmp_path / "arc" / "Empty.jsonl").exists()


def test_write_sessions_raises_on_filename_collision(tmp_path: Path) -> None:
    # "Topic A" and "Topic  A" (double space) both sanitise to "Topic-A"
    rows = {
        "Topic A": [{"type": "user", "uuid": "u1", "message": {"content": "x"}}],
        "Topic  A": [{"type": "user", "uuid": "u2", "message": {"content": "y"}}],
    }
    with pytest.raises(ValueError, match="collision"):
        write_sessions(tmp_path, prefix="arc", rows_by_session=rows)


def test_write_sessions_unicode_roundtrips(tmp_path: Path) -> None:
    rows = [{"type": "user", "uuid": "u1", "message": {"content": "Café 日本語"}}]
    write_sessions(tmp_path, prefix="arc", rows_by_session={"Unicode": rows})
    line = (tmp_path / "arc" / "Unicode.jsonl").read_text(encoding="utf-8").strip()
    assert json.loads(line)["message"]["content"] == "Café 日本語"
