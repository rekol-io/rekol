"""Tests for JSONL ingest of Claude Code transcripts."""

from __future__ import annotations

from pathlib import Path

from rekol.embeddings import HashingEmbedder
from rekol.sessions.ingest import embed_missing, ingest_file, iter_messages_in_file
from rekol.sessions.store import SessionStore

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


def test_ingest_file_with_embedder_makes_messages_vector_searchable(tmp_path: Path) -> None:
    """When an embedder is passed, every inserted message must get an embedding
    so vector search returns it. This is the FIX that turns transcript search
    from FTS5-only into hybrid keyword+semantic.
    """
    store = SessionStore(db_path=tmp_path / "s.db", dim=384)
    store.init_schema()
    embedder = HashingEmbedder(dim=384)
    stats = ingest_file(FIXTURE, store, embedder=embedder)
    assert stats.messages_inserted == 3
    # The exact content of the u-3 turn embeds to (near) itself under hashing,
    # so a vector query over the same text must surface that message.
    query_vec = embedder.embed("second user turn with list content")
    hits = store.search_vec(query_vec, top_k=3)
    assert hits, "vector search returned nothing — embeddings were not written"
    assert hits[0]["message_uuid"] == "u-3"
    store.close()


def test_ingest_file_without_embedder_leaves_vector_index_empty(tmp_path: Path) -> None:
    """Default (no embedder) keeps today's FTS-only behaviour: nothing is
    written to the vector index, so search_vec returns no hits.
    """
    store = SessionStore(db_path=tmp_path / "s.db", dim=384)
    store.init_schema()
    stats = ingest_file(FIXTURE, store)  # embedder defaults to None
    assert stats.messages_inserted == 3
    query_vec = HashingEmbedder(dim=384).embed("second user turn with list content")
    assert store.search_vec(query_vec, top_k=3) == []
    store.close()


def test_embed_missing_heals_an_fts_only_index(tmp_path: Path) -> None:
    """An index ingested without embeddings (FTS-only, or --no-embed) is
    permanently skipped by the mtime gate, so it would stay keyword-only
    forever. embed_missing must backfill embeddings for those messages so the
    index becomes fully semantic without a destructive rebuild.
    """
    store = SessionStore(db_path=tmp_path / "s.db", dim=384)
    store.init_schema()
    embedder = HashingEmbedder(dim=384)
    # First ingest WITHOUT embeddings — simulates an old/pre-fix index.
    ingest_file(FIXTURE, store)
    assert store.count_embeddings() == 0
    query_vec = embedder.embed("second user turn with list content")
    assert store.search_vec(query_vec, top_k=3) == []  # nothing semantic yet

    healed = embed_missing(store, embedder)
    assert healed == 3
    assert store.count_embeddings() == 3
    hits = store.search_vec(query_vec, top_k=3)
    assert hits and hits[0]["message_uuid"] == "u-3"
    store.close()


def test_embed_missing_is_a_noop_when_fully_embedded(tmp_path: Path) -> None:
    """Steady state: when every message already has an embedding, embed_missing
    must do no work (returns 0) so the SessionEnd hook stays cheap.
    """
    store = SessionStore(db_path=tmp_path / "s.db", dim=384)
    store.init_schema()
    embedder = HashingEmbedder(dim=384)
    ingest_file(FIXTURE, store, embedder=embedder)
    assert store.count_messages() == store.count_embeddings() == 3
    assert embed_missing(store, embedder) == 0
    store.close()


def test_ingest_file_embedder_incremental_skip_does_not_reembed(tmp_path: Path) -> None:
    """Second ingest of an unchanged file must skip via the mtime gate without
    re-embedding — the embedding work must not defeat the SessionEnd-hook
    fast path on deep-history machines.
    """
    store = SessionStore(db_path=tmp_path / "s.db", dim=384)
    store.init_schema()
    embedder = HashingEmbedder(dim=384)
    ingest_file(FIXTURE, store, embedder=embedder)
    stats = ingest_file(FIXTURE, store, embedder=embedder)
    assert stats.files_skipped_unchanged == 1
    assert stats.messages_inserted == 0
    # Still exactly the 3 embeddings from the first pass — none added, none lost.
    query_vec = embedder.embed("hello there")
    assert store.search_vec(query_vec, top_k=5)
    store.close()
