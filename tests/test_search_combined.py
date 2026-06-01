"""Tests for the combined memory + sessions search layer."""

from __future__ import annotations

from pathlib import Path

from rekol.embeddings import HashingEmbedder
from rekol.search_combined import CombinedSearchResult, search_all
from rekol.sessions.store import SessionStore
from rekol.store import IndexStore


def _seed_memory(tmp_path: Path) -> IndexStore:
    store = IndexStore(db_path=tmp_path / "mem.db", dim=384)
    store.init_schema()
    store.upsert_file("/m/topics/litellm.md", mtime=1, content_hash="h")
    emb = HashingEmbedder(dim=384).embed("litellm base url configuration")
    store.replace_chunks_for_file(
        "/m/topics/litellm.md",
        [
            dict(
                heading="LiteLLM proxy",
                line_start=1,
                line_end=5,
                text="LiteLLM proxy at simone.home routes Claude through OpenRouter",
                tags=["litellm"],
                aliases=["base url"],
                embedding=emb,
            )
        ],
    )
    return store


def _seed_sessions(tmp_path: Path) -> SessionStore:
    store = SessionStore(db_path=tmp_path / "sessions.db", dim=384)
    store.init_schema()
    rowid = store.insert_message(
        dict(
            session_id="s-1",
            message_uuid="u-1",
            parent_uuid=None,
            role="user",
            content="how do i set the litellm base_url",
            cwd="/tmp/repo",
            timestamp_iso="2026-05-26T00:00:00Z",
            timestamp_unix=1748217600,
            jsonl_path="/fake.jsonl",
            line_number=2,
        )
    )
    emb = HashingEmbedder(dim=384).embed("how do i set the litellm base_url")
    store.upsert_embedding(rowid, emb)
    return store


def test_search_all_returns_both_tiers(tmp_path: Path) -> None:
    mem = _seed_memory(tmp_path)
    sess = _seed_sessions(tmp_path)
    embedder = HashingEmbedder(dim=384)
    result = search_all(
        query="litellm base url",
        embedder=embedder,
        memory_store=mem,
        session_store=sess,
        memory_top_k=5,
        sessions_top_k=5,
    )
    assert isinstance(result, CombinedSearchResult)
    assert len(result.memory_hits) >= 1
    assert len(result.session_hits) >= 1


def test_search_all_source_memory_only_skips_sessions(tmp_path: Path) -> None:
    mem = _seed_memory(tmp_path)
    sess = _seed_sessions(tmp_path)
    embedder = HashingEmbedder(dim=384)
    result = search_all(
        query="litellm",
        embedder=embedder,
        memory_store=mem,
        session_store=sess,
        source="memory",
    )
    assert len(result.memory_hits) >= 1
    assert result.session_hits == []


def test_search_all_promote_candidates(tmp_path: Path) -> None:
    # Empty memory, populated sessions → query should surface as a promotion candidate
    mem = IndexStore(db_path=tmp_path / "mem.db", dim=384)
    mem.init_schema()
    sess = _seed_sessions(tmp_path)
    embedder = HashingEmbedder(dim=384)
    result = search_all(
        query="litellm base url",
        embedder=embedder,
        memory_store=mem,
        session_store=sess,
    )
    assert result.is_promotion_candidate is True


def test_search_all_source_sessions_alone_does_not_flag_promotion(tmp_path: Path) -> None:
    """Regression: when only sessions is queried, is_promotion_candidate must
    be False even if there are session hits, because memory was never asked.
    Without the sources_queried guard, this returns True spuriously.
    """
    sess = _seed_sessions(tmp_path)
    embedder = HashingEmbedder(dim=384)
    result = search_all(
        query="litellm",
        embedder=embedder,
        session_store=sess,
        source="sessions",
    )
    assert "sessions" in result.sources_queried
    assert "memory" not in result.sources_queried
    assert result.is_promotion_candidate is False


def test_filtered_count_suppresses_promotion_candidate() -> None:
    r = CombinedSearchResult(
        query="q",
        memory_hits=[],
        session_hits=[{"x": 1}],
        sources_queried=["memory", "sessions"],
        memory_filtered_count=2,
    )
    assert r.is_promotion_candidate is False  # matches exist but were filtered


def test_promotion_candidate_when_truly_empty() -> None:
    r = CombinedSearchResult(
        query="q",
        memory_hits=[],
        session_hits=[{"x": 1}],
        sources_queried=["memory", "sessions"],
        memory_filtered_count=0,
    )
    assert r.is_promotion_candidate is True


def test_search_all_applies_temporal_ranking_with_config(tmp_path: Path, monkeypatch) -> None:
    from rekol.config import load_config

    monkeypatch.setenv("REKOL_HOME", str(tmp_path))
    cfg = load_config()
    store = IndexStore(db_path=cfg.index_db_path, dim=384)
    store.init_schema()
    emb = HashingEmbedder(dim=384)
    for name, inv in (("live", None), ("bad", "2026-03-01")):
        fp = str(tmp_path / "topics" / f"{name}.md")
        store.upsert_file(fp, mtime=1, content_hash=name)
        store.replace_chunks_for_file(
            fp,
            [
                dict(
                    heading=None,
                    line_start=1,
                    line_end=1,
                    text="deploy process",
                    tags=[],
                    aliases=[],
                    embedding=emb.embed("deploy process"),
                )
            ],
            invalidated_at=inv,
        )
    res = search_all("deploy process", emb, memory_store=store, source="memory", config=cfg)
    assert len(res.memory_hits) == 1  # the invalidated one is excluded
    assert res.memory_filtered_count == 1
    store.close()
