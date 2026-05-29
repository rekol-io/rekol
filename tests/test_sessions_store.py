"""Tests for SessionStore — schema init, insert, FTS5 + vec search, dedupe."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from memory_tools.sessions.store import SessionStore


def test_init_schema_creates_expected_tables(tmp_path: Path) -> None:
    store = SessionStore(db_path=tmp_path / "sessions.db", dim=384)
    store.init_schema()
    tables = store.list_tables()
    assert "messages" in tables
    assert "messages_fts" in tables
    # When sqlite-vec is available, the virtual table is `messages_vec`.
    # When it is not, the numpy fallback is `messages_vec_numpy`.
    assert ("messages_vec" in tables) or ("messages_vec_numpy" in tables)
    # files_seen tracks per-JSONL mtime/size for incremental ingest skip.
    assert "files_seen" in tables
    store.close()


def test_init_schema_idempotent_across_vec_availability(tmp_path: Path) -> None:
    """Calling init_schema twice with different vec availability must not collide.

    Regression guard: the original design used the same name `messages_vec` for
    both the vec0 virtual table and the numpy-fallback regular table; that meant
    flipping availability shadowed the virtual table at runtime. The names must
    be distinct so init is safe to re-run as the environment changes.
    """
    # First init without vec
    store1 = SessionStore(db_path=tmp_path / "s.db", dim=384, use_sqlite_vec=False)
    store1.init_schema()
    tables_no_vec = store1.list_tables()
    store1.close()
    # Second init with vec available (simulates a later install)
    store2 = SessionStore(db_path=tmp_path / "s.db", dim=384, use_sqlite_vec=True)
    store2.init_schema()
    tables_with_vec = store2.list_tables()
    store2.close()
    assert "messages_vec_numpy" in tables_no_vec
    # If sqlite-vec is actually installed in the test env, the virtual table
    # is created additionally; otherwise both runs use the numpy fallback.
    if "messages_vec" in tables_with_vec:
        assert "messages_vec_numpy" in tables_with_vec  # fallback persists from earlier run


def _make_msg(uuid: str = "u1", session: str = "s1", line: int = 1) -> dict:
    return dict(
        session_id=session,
        message_uuid=uuid,
        parent_uuid=None,
        role="user",
        content="hello world",
        cwd="/tmp/repo",
        timestamp_iso="2026-05-28T20:00:00Z",
        timestamp_unix=1748462400,
        jsonl_path="/fake/session.jsonl",
        line_number=line,
    )


def test_insert_message_round_trips(tmp_path: Path) -> None:
    store = SessionStore(db_path=tmp_path / "s.db", dim=384)
    store.init_schema()
    rowid = store.insert_message(_make_msg())
    assert rowid > 0
    rows = list(store.conn.execute("SELECT content FROM messages"))
    assert rows[0]["content"] == "hello world"
    store.close()


def test_insert_message_dedupes_on_uuid(tmp_path: Path) -> None:
    store = SessionStore(db_path=tmp_path / "s.db", dim=384)
    store.init_schema()
    first = store.insert_message(_make_msg(uuid="dup", session="s1"))
    second = store.insert_message(_make_msg(uuid="dup", session="s1"))
    assert first > 0
    assert second is None  # signals dedupe
    count = store.conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
    assert count == 1
    store.close()


def test_search_fts_matches_keyword(tmp_path: Path) -> None:
    store = SessionStore(db_path=tmp_path / "s.db", dim=384)
    store.init_schema()
    store.insert_message(_make_msg(uuid="a", session="s1") | {"content": "the litellm base_url is configured"})
    store.insert_message(_make_msg(uuid="b", session="s1", line=2) | {"content": "unrelated message about cats"})
    hits = store.search_fts("litellm", top_k=5)
    assert len(hits) == 1
    assert hits[0]["message_uuid"] == "a"
    assert hits[0]["score"] > 0
    store.close()


def test_search_vec_returns_top_k(tmp_path: Path) -> None:
    store = SessionStore(db_path=tmp_path / "s.db", dim=4)
    store.init_schema()
    rowid_a = store.insert_message(_make_msg(uuid="a"))
    rowid_b = store.insert_message(_make_msg(uuid="b", line=2))
    store.upsert_embedding(rowid_a, np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32))
    store.upsert_embedding(rowid_b, np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32))
    hits = store.search_vec(np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32), top_k=1)
    assert len(hits) == 1
    assert hits[0]["message_uuid"] == "a"
    store.close()


def test_search_fts_score_is_positive_higher_is_better(tmp_path: Path) -> None:
    """Regression: BM25 returns negative scores; the wrapper must negate so
    the higher-is-better merge in search_combined is correct.
    """
    store = SessionStore(db_path=tmp_path / "s.db", dim=384)
    store.init_schema()
    store.insert_message(_make_msg(uuid="strong", session="s1") | {"content": "litellm litellm litellm proxy"})
    store.insert_message(_make_msg(uuid="weak", session="s1", line=2) | {"content": "litellm appears once buried in unrelated text about cats and dogs"})
    hits = store.search_fts("litellm", top_k=5)
    assert len(hits) == 2
    # Both scores must be >= 0 (negative scores indicate the formula bug)
    assert all(h["score"] >= 0 for h in hits), [h["score"] for h in hits]
    # Stronger match must rank higher
    strong = next(h for h in hits if h["message_uuid"] == "strong")
    weak = next(h for h in hits if h["message_uuid"] == "weak")
    assert strong["score"] > weak["score"]
    store.close()


def test_files_seen_skip_logic(tmp_path: Path) -> None:
    store = SessionStore(db_path=tmp_path / "s.db", dim=384)
    store.init_schema()
    # No record → not skip
    assert store.should_skip_file("/tmp/a.jsonl", 100, 500) is False
    # Record then matching mtime+size → skip
    store.record_file_seen("/tmp/a.jsonl", 100, 500)
    assert store.should_skip_file("/tmp/a.jsonl", 100, 500) is True
    # Either mtime or size differs → not skip
    assert store.should_skip_file("/tmp/a.jsonl", 101, 500) is False
    assert store.should_skip_file("/tmp/a.jsonl", 100, 501) is False
    store.close()
