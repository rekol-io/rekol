"""Tests for JSONL ingest of Claude Code transcripts."""

from __future__ import annotations

from pathlib import Path

from memory_tools.sessions.ingest import ingest_file, iter_messages_in_file
from memory_tools.sessions.store import SessionStore

FIXTURE = Path(__file__).parent / "fixtures" / "sample_session.jsonl"


def test_iter_messages_skips_non_message_types() -> None:
    msgs = list(iter_messages_in_file(FIXTURE))
    # 3 user/assistant turns, queue-operation rows skipped
    assert len(msgs) == 3
    roles = [m["role"] for m in msgs]
    assert roles == ["user", "assistant", "user"]


def test_iter_messages_flattens_list_content() -> None:
    msgs = list(iter_messages_in_file(FIXTURE))
    # Last message has list-of-blocks content
    assert msgs[2]["content"] == "second user turn with list content"


def test_iter_messages_captures_required_fields() -> None:
    msgs = list(iter_messages_in_file(FIXTURE))
    m = msgs[0]
    assert m["session_id"] == "s-1"
    assert m["message_uuid"] == "u-1"
    assert m["cwd"] == "/tmp/repoA"
    assert m["timestamp_iso"].startswith("2026-04-24")
    assert m["timestamp_unix"] > 0
    assert m["line_number"] == 2  # 1-indexed line in the file


def test_ingest_file_inserts_messages(tmp_path: Path) -> None:
    store = SessionStore(db_path=tmp_path / "s.db", dim=384)
    store.init_schema()
    stats = ingest_file(FIXTURE, store)
    assert stats.messages_inserted == 3
    assert stats.messages_skipped_dupe == 0
    store.close()


def test_ingest_file_is_idempotent_via_mtime_skip(tmp_path: Path) -> None:
    """Second ingest of an unchanged file must skip entirely via files_seen,
    NOT do a row-by-row dedupe walk. The mtime gate is what keeps the
    SessionEnd hook fast on machines with deep history.
    """
    store = SessionStore(db_path=tmp_path / "s.db", dim=384)
    store.init_schema()
    ingest_file(FIXTURE, store)
    stats = ingest_file(FIXTURE, store)
    assert stats.files_skipped_unchanged == 1
    assert stats.messages_inserted == 0
    assert stats.messages_skipped_dupe == 0  # never even opened the file


def test_ingest_file_force_bypasses_mtime_skip(tmp_path: Path) -> None:
    store = SessionStore(db_path=tmp_path / "s.db", dim=384)
    store.init_schema()
    ingest_file(FIXTURE, store)
    stats = ingest_file(FIXTURE, store, force=True)
    assert stats.files_skipped_unchanged == 0
    assert stats.files_ingested == 1
    # All 3 indexable rows are duplicates of the prior ingest
    assert stats.messages_inserted == 0
    assert stats.messages_skipped_dupe == 3


def test_ingest_counts_no_text_rows_separately(tmp_path: Path) -> None:
    """Tool-use-only assistant rows and tool_result-only user rows must
    increment messages_skipped_no_text, NOT messages_skipped_malformed.
    """
    store = SessionStore(db_path=tmp_path / "s.db", dim=384)
    store.init_schema()
    stats = ingest_file(FIXTURE, store)
    assert stats.messages_inserted == 3
    assert stats.messages_skipped_no_text == 2
    assert stats.messages_skipped_malformed == 0
