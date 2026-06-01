"""Tests for SessionStore — schema init, insert, FTS5 + vec search, dedupe."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from rekol.sessions.store import SessionStore, SessionStoreDimMismatchError


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
    store.insert_message(
        _make_msg(uuid="a", session="s1") | {"content": "the litellm base_url is configured"}
    )
    store.insert_message(
        _make_msg(uuid="b", session="s1", line=2) | {"content": "unrelated message about cats"}
    )
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


def test_upsert_embedding_no_commit_batches_within_caller_transaction(tmp_path: Path) -> None:
    """The no-commit variant lets ingest write a whole file's embeddings in one
    transaction (no per-row fsync). Rows must be searchable once the caller's
    transaction commits.
    """
    store = SessionStore(db_path=tmp_path / "s.db", dim=4)
    store.init_schema()
    rowid_a = store.insert_message(_make_msg(uuid="a"))
    rowid_b = store.insert_message(_make_msg(uuid="b", line=2))
    with store.conn:
        store.upsert_embedding_no_commit(rowid_a, np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32))
        store.upsert_embedding_no_commit(rowid_b, np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32))
    hits = store.search_vec(np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32), top_k=1)
    assert len(hits) == 1
    assert hits[0]["message_uuid"] == "b"
    store.close()


def test_fetch_unembedded_and_counts(tmp_path: Path) -> None:
    """The self-heal primitives must report which messages lack an embedding so
    an index built FTS-only (or with --no-embed) can be brought to full
    semantic coverage without a destructive rebuild.
    """
    store = SessionStore(db_path=tmp_path / "s.db", dim=4)
    store.init_schema()
    rid_a = store.insert_message(_make_msg(uuid="a"))
    rid_b = store.insert_message(_make_msg(uuid="b", line=2))
    rid_c = store.insert_message(_make_msg(uuid="c", line=3))
    assert store.count_messages() == 3
    assert store.count_embeddings() == 0
    # All three are unembedded initially.
    pending = store.fetch_unembedded(limit=10)
    assert {rowid for rowid, _content in pending} == {rid_a, rid_b, rid_c}
    # Embed one; it must drop out of the unembedded set and the counts update.
    with store.conn:
        store.upsert_embedding_no_commit(rid_a, np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32))
    assert store.count_embeddings() == 1
    remaining = store.fetch_unembedded(limit=10)
    assert rid_a not in {rowid for rowid, _content in remaining}
    assert {rowid for rowid, _content in remaining} == {rid_b, rid_c}
    store.close()


def test_existing_vec_dim_reports_stored_dimension(tmp_path: Path) -> None:
    """existing_vec_dim must report the width an index was built with, so a later
    run under a different-dimension model can fail fast instead of crashing
    cryptically on the first vector insert.
    """
    store = SessionStore(db_path=tmp_path / "s.db", dim=4)
    store.init_schema()
    rid = store.insert_message(_make_msg(uuid="a"))
    with store.conn:
        store.upsert_embedding_no_commit(rid, np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32))
    # vec0 reads the dim from the schema; numpy fallback infers it from the row.
    assert store.existing_vec_dim() == 4
    store.close()


def test_reconcile_embedding_dim_noop_when_matching(tmp_path: Path) -> None:
    store = SessionStore(db_path=tmp_path / "s.db", dim=4)
    store.init_schema()
    rid = store.insert_message(_make_msg(uuid="a"))
    with store.conn:
        store.upsert_embedding_no_commit(rid, np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32))
    # Same width — must not raise or disturb the stored embedding.
    store.reconcile_embedding_dim(4)
    assert store.count_embeddings() == 1
    store.close()


def test_reconcile_embedding_dim_recreates_empty_stale_table(tmp_path: Path) -> None:
    """A vec table created at one width but never written (e.g. by a --no-embed
    run) must be recreated at the new width rather than blocking embedding.
    """
    store = SessionStore(db_path=tmp_path / "s.db", dim=4)
    store.init_schema()
    assert store.count_embeddings() == 0  # nothing written
    # No rows → safe to adopt the new width with no data loss, no error.
    store.reconcile_embedding_dim(8)
    assert store.dim == 8
    # A subsequent 8-dim embedding now writes and is searchable.
    rid = store.insert_message(_make_msg(uuid="a"))
    with store.conn:
        store.upsert_embedding_no_commit(rid, np.array([1, 0, 0, 0, 0, 0, 0, 0], dtype=np.float32))
    assert store.count_embeddings() == 1
    store.close()


def test_reconcile_embedding_dim_raises_on_populated_mismatch(tmp_path: Path) -> None:
    """A vec index that already holds vectors at one width must refuse a
    different width loudly, so callers surface remediation instead of crashing.
    """
    store = SessionStore(db_path=tmp_path / "s.db", dim=4)
    store.init_schema()
    rid = store.insert_message(_make_msg(uuid="a"))
    with store.conn:
        store.upsert_embedding_no_commit(rid, np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32))
    with pytest.raises(SessionStoreDimMismatchError) as excinfo:
        store.reconcile_embedding_dim(8)
    assert excinfo.value.existing_dim == 4
    assert excinfo.value.wanted_dim == 8
    store.close()


def test_fetch_unembedded_respects_limit(tmp_path: Path) -> None:
    store = SessionStore(db_path=tmp_path / "s.db", dim=4)
    store.init_schema()
    for i in range(5):
        store.insert_message(_make_msg(uuid=f"u{i}", line=i + 1))
    assert len(store.fetch_unembedded(limit=2)) == 2
    store.close()


def test_search_fts_score_is_positive_higher_is_better(tmp_path: Path) -> None:
    """Regression: BM25 returns negative scores; the wrapper must negate so
    the higher-is-better merge in search_combined is correct.
    """
    store = SessionStore(db_path=tmp_path / "s.db", dim=384)
    store.init_schema()
    store.insert_message(
        _make_msg(uuid="strong", session="s1") | {"content": "litellm litellm litellm proxy"}
    )
    store.insert_message(
        _make_msg(uuid="weak", session="s1", line=2)
        | {"content": "litellm appears once buried in unrelated text about cats and dogs"}
    )
    hits = store.search_fts("litellm", top_k=5)
    assert len(hits) == 2
    # Both scores must be >= 0 (negative scores indicate the formula bug)
    assert all(h["score"] >= 0 for h in hits), [h["score"] for h in hits]
    # Stronger match must rank higher
    strong = next(h for h in hits if h["message_uuid"] == "strong")
    weak = next(h for h in hits if h["message_uuid"] == "weak")
    assert strong["score"] > weak["score"]
    store.close()


def test_search_fts_handles_punctuated_query_without_crashing(tmp_path: Path) -> None:
    """Regression: a raw query with FTS5 operator chars (``-``, ``:``, ``_``)
    used to be passed straight into MATCH and raised
    ``sqlite3.OperationalError: no such column: ...``. The query must be
    sanitised into quoted phrases first.
    """
    store = SessionStore(db_path=tmp_path / "s.db", dim=384)
    store.init_schema()
    store.insert_message(
        _make_msg(uuid="a", session="s1")
        | {"content": "the slack-daemon must unset ANTHROPIC_API_KEY for bedrock auth"}
    )
    store.insert_message(
        _make_msg(uuid="b", session="s1", line=2) | {"content": "unrelated message about cats"}
    )
    # Hyphen, underscore, and a normal word together — the original crasher.
    hits = store.search_fts("slack-daemon ANTHROPIC_API_KEY bedrock", top_k=5)
    assert len(hits) == 1
    assert hits[0]["message_uuid"] == "a"
    store.close()


def test_search_fts_defuses_leading_hyphen_operator(tmp_path: Path) -> None:
    """A leading ``-`` is FTS5's NOT operator; sanitisation must treat it as a
    literal term so ``-bedrock`` finds 'bedrock' rather than excluding it.
    """
    store = SessionStore(db_path=tmp_path / "s.db", dim=384)
    store.init_schema()
    store.insert_message(
        _make_msg(uuid="a", session="s1") | {"content": "deploy notes mention bedrock"}
    )
    hits = store.search_fts("-bedrock", top_k=5)
    assert len(hits) == 1
    assert hits[0]["message_uuid"] == "a"
    store.close()


def test_search_fts_colon_query_does_not_crash(tmp_path: Path) -> None:
    """A ``:`` is FTS5 column-filter syntax; a raw ``foo:bar`` query must not
    raise.
    """
    store = SessionStore(db_path=tmp_path / "s.db", dim=384)
    store.init_schema()
    store.insert_message(
        _make_msg(uuid="a", session="s1") | {"content": "the config sets base_url:litellm here"}
    )
    # The ``:`` is treated as part of a literal phrase (not FTS5 column syntax),
    # so it matches the same adjacent token sequence in the content.
    hits = store.search_fts("base_url:litellm", top_k=5)
    assert hits[0]["message_uuid"] == "a"
    store.close()


def test_search_fts_empty_query_returns_no_hits(tmp_path: Path) -> None:
    """A whitespace-only (or operator-only) query has nothing searchable; it
    must return [] rather than issuing an empty MATCH (which FTS5 rejects).
    """
    store = SessionStore(db_path=tmp_path / "s.db", dim=384)
    store.init_schema()
    store.insert_message(_make_msg(uuid="a", session="s1") | {"content": "hello"})
    assert store.search_fts("   ", top_k=5) == []
    assert store.search_fts("- :", top_k=5) == []
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
