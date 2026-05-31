"""Tests for docs_convert.transcript — prefix-salted uuids, mtime timestamps, schema."""

from __future__ import annotations

from pathlib import Path

from memory_tools.docs_convert.transcript import _row_uuid, _session_id, build_rows
from memory_tools.docs_convert.walk import FileEntry, SessionGroup


def _group() -> SessionGroup:
    return SessionGroup(
        session_name="Topic A",
        folder_abs="/src/Topic A",
        rel_to_source="Topic A",
        files=[
            FileEntry(
                path=Path("/src/Topic A/note.md"),
                rel_to_source="Topic A/note.md",
                rel_to_session="note.md",
                mtime_unix=1745835000,  # 2025-04-28T10:10:00Z
            )
        ],
    )


def test_build_rows_emits_valid_ingester_schema() -> None:
    rows = build_rows(_group(), prefix="arc", texts={"note.md": "hello body"})
    assert len(rows) == 1
    r = rows[0]
    assert r["type"] == "user"  # passes _MESSAGE_TYPES filter
    assert r["message"]["role"] == "document"  # stored role tag
    assert r["parentUuid"] is None
    assert r["cwd"] == "/src/Topic A"
    assert r["uuid"] and r["sessionId"] and r["timestamp"]


def test_build_rows_prefixes_content_with_relative_path() -> None:
    rows = build_rows(_group(), prefix="arc", texts={"note.md": "hello body"})
    assert rows[0]["message"]["content"] == "note.md\n\nhello body"


def test_build_rows_timestamp_is_iso_with_z() -> None:
    rows = build_rows(_group(), prefix="arc", texts={"note.md": "x"})
    # Must be parseable by ingest._parse_timestamp (ISO-8601, trailing Z)
    assert rows[0]["timestamp"].endswith("Z")
    assert rows[0]["timestamp"].startswith("2025-04-28T")


def test_uuid_is_deterministic_for_same_prefix_and_path() -> None:
    assert _row_uuid("arc", "Topic A/note.md") == _row_uuid("arc", "Topic A/note.md")


def test_uuid_differs_across_prefixes() -> None:
    # Cross-source collision guard: same path, different archive → different uuid
    assert _row_uuid("arc", "Topic A/note.md") != _row_uuid("other", "Topic A/note.md")


def test_session_id_differs_across_prefixes() -> None:
    assert _session_id("arc", "Topic A") != _session_id("other", "Topic A")


def test_two_files_same_session_same_session_id_different_uuid() -> None:
    group = SessionGroup(
        session_name="Topic A",
        folder_abs="/src/Topic A",
        rel_to_source="Topic A",
        files=[
            FileEntry(Path("/src/Topic A/a.md"), "Topic A/a.md", "a.md", 1745835000),
            FileEntry(Path("/src/Topic A/b.md"), "Topic A/b.md", "b.md", 1745835001),
        ],
    )
    rows = build_rows(group, prefix="arc", texts={"a.md": "body a", "b.md": "body b"})
    assert len(rows) == 2
    assert rows[0]["sessionId"] == rows[1]["sessionId"]  # same session
    assert rows[0]["uuid"] != rows[1]["uuid"]  # distinct messages


def test_build_rows_skips_files_with_no_extracted_text() -> None:
    # A file whose extraction returned None is absent from `texts` → no row,
    # so a path-only ghost message is never emitted.
    rows = build_rows(_group(), prefix="arc", texts={})
    assert rows == []
