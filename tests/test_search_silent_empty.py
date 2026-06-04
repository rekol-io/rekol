"""Regression tests for issue #18: search must never *silently* return 0
results when the index holds rows it should have matched.

The dogfooding failure: a stale/inconsistent transcript index where a raw
``messages_fts MATCH '<term>'`` reports matching rows, yet ``rekol search``
returned ``"sessions": []`` with no error and no warning — only a full
rebuild fixed it. The bug is the *silent* empty: the user sees "memory just
doesn't work" with zero signal why.

We reproduce the exact signature (FTS postings present but their rowids are
orphaned, so the JOIN to ``messages`` drops them all) and assert that the read
path now (a) detects it and (b) prints an actionable rebuild warning instead of
a silent empty. A genuinely-empty corpus, or a query that truly has no matches,
must stay quiet.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from rekol.cli_session_index import main as session_idx_main
from rekol.embeddings import HashingEmbedder
from rekol.search_combined import search_all
from rekol.sessions.store import SessionStore


def _seed_sessions(db_path: Path, *, n: int = 12) -> SessionStore:
    """Build a healthy sessions index with ``n`` messages mentioning 'uninstall'."""
    store = SessionStore(db_path=db_path, dim=384)
    store.init_schema()
    emb = HashingEmbedder(dim=384)
    for i in range(n):
        rowid = store.insert_message(
            dict(
                session_id=f"s-{i}",
                message_uuid=f"u-{i}",
                parent_uuid=None,
                role="user",
                content=f"how do I uninstall the rekol thing number {i}",
                cwd="/tmp/repo",
                timestamp_iso="2026-05-26T00:00:00Z",
                timestamp_unix=1748217600,
                jsonl_path="/fake.jsonl",
                line_number=i,
            )
        )
        assert rowid is not None
        store.upsert_embedding(rowid, emb.embed(f"how do I uninstall number {i}"))
    return store


def _make_fts_index_stale(store: SessionStore) -> None:
    """Induce the #18 desync: FTS postings survive, but their rowids are orphaned.

    Drops the sync triggers, wipes ``messages`` (so the FTS delete trigger never
    runs), then reinserts equivalent rows under fresh autoincrement ids that the
    FTS index has never seen. The FTS index still matches 'uninstall', but every
    matched rowid now points at a deleted message — exactly the state where a raw
    MATCH reports rows while search returns nothing.
    """
    store.conn.executescript(
        "DROP TRIGGER IF EXISTS messages_ai;"
        "DROP TRIGGER IF EXISTS messages_ad;"
        "DROP TRIGGER IF EXISTS messages_au;"
    )
    store.conn.execute("DELETE FROM messages")
    store.conn.commit()
    for i in range(12):
        store.conn.execute(
            "INSERT INTO messages(session_id, message_uuid, parent_uuid, role, content, "
            "cwd, timestamp_iso, timestamp_unix, jsonl_path, line_number) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                f"s2-{i}",
                f"u2-{i}",
                None,
                "user",
                f"how do I uninstall the rekol thing number {i}",
                "/tmp/repo",
                "2026-05-26T00:00:00Z",
                1748217600,
                "/fake.jsonl",
                i,
            ),
        )
    store.conn.commit()


# --------------------------- store-level detection ---------------------------


def test_fts_index_is_stale_false_on_healthy_index(tmp_path: Path) -> None:
    store = _seed_sessions(tmp_path / "sessions.db")
    try:
        assert store.fts_postings_match("uninstall") > 0
        assert store.fts_index_is_stale("uninstall") is False
        # A term genuinely absent from the corpus is NOT "stale" — no false alarm.
        assert store.fts_index_is_stale("zzzdefinitelynotpresent") is False
    finally:
        store.close()


def test_fts_index_is_stale_detects_orphaned_postings(tmp_path: Path) -> None:
    store = _seed_sessions(tmp_path / "sessions.db")
    try:
        _make_fts_index_stale(store)
        # The #18 signature: raw MATCH still reports rows ...
        assert store.fts_postings_match("uninstall") > 0
        # ... but search_fts (the JOIN to messages) drops every one of them ...
        assert store.search_fts("uninstall", top_k=5) == []
        # ... and that exact inconsistency is what fts_index_is_stale flags.
        assert store.fts_index_is_stale("uninstall") is True
    finally:
        store.close()


def test_search_all_session_hits_empty_on_stale_index(tmp_path: Path) -> None:
    """Confirms the user-visible symptom before the warning: zero session hits."""
    store = _seed_sessions(tmp_path / "sessions.db")
    try:
        _make_fts_index_stale(store)
        emb = HashingEmbedder(dim=384)
        result = search_all(
            "uninstall", emb, session_store=store, source="sessions", sessions_top_k=5
        )
        assert result.session_hits == []  # silent-empty symptom reproduced
    finally:
        store.close()


# ----------------------------- CLI-level backstop ----------------------------


def _write_config(home: Path, projects_dir: Path) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "rekol.config.yaml").write_text(
        f"embedding_model: test-hashing\nclaude_projects_dir: {projects_dir}\n"
    )


def test_cli_search_warns_loudly_on_stale_session_index(tmp_path: Path, monkeypatch) -> None:
    """End-to-end: a stale transcript index must surface an actionable rebuild
    warning instead of a silent ``"sessions": []``.
    """
    from rekol.cli_search import main as search_main

    home = tmp_path / "home"
    projects = tmp_path / "projects" / "proj"
    projects.mkdir(parents=True)
    # A real transcript so the index is built through the normal ingest path.
    (projects / "session.jsonl").write_text(
        '{"parentUuid":null,"type":"user","message":{"role":"user",'
        '"content":"how do I uninstall the rekol thing"},'
        '"uuid":"u-1","timestamp":"2026-04-24T01:29:01.303Z",'
        '"sessionId":"s-1","cwd":"/tmp/repoA"}\n'
        '{"parentUuid":"u-1","type":"assistant","message":{"role":"assistant",'
        '"content":"to uninstall rekol run the uninstall script"},'
        '"uuid":"u-2","timestamp":"2026-04-24T01:29:02.500Z",'
        '"sessionId":"s-1","cwd":"/tmp/repoA"}\n'
    )
    _write_config(home, projects.parent)
    monkeypatch.setenv("REKOL_HOME", str(home))

    runner = CliRunner()
    built = runner.invoke(session_idx_main, ["--full"])
    assert built.exit_code == 0, built.output

    # Sanity: the healthy index finds the term, no warning.
    healthy = runner.invoke(search_main, ["uninstall", "--source", "sessions"])
    assert healthy.exit_code == 0, healthy.output
    assert "stale or inconsistent" not in healthy.output

    # Now corrupt the on-disk index into the #18 stale state.
    from cache_helpers import cache_dir_for

    db_path = cache_dir_for(home) / "sessions.db"
    store = SessionStore(db_path=db_path, dim=384)
    try:
        _make_fts_index_stale(store)
    finally:
        store.close()

    res = runner.invoke(search_main, ["uninstall", "--source", "sessions"])
    assert res.exit_code == 0, res.output
    # The never-silent invariant: data present, zero hits -> loud, actionable.
    assert "stale or inconsistent" in res.output
    assert "rekol index rebuild" in res.output
    assert "rekol session-index --full" in res.output


def test_cli_search_quiet_when_corpus_truly_has_no_match(tmp_path: Path, monkeypatch) -> None:
    """A healthy index with no match for the query must NOT cry 'stale'."""
    from rekol.cli_search import main as search_main

    home = tmp_path / "home"
    projects = tmp_path / "projects" / "proj"
    projects.mkdir(parents=True)
    (projects / "session.jsonl").write_text(
        '{"parentUuid":null,"type":"user","message":{"role":"user",'
        '"content":"a totally unrelated message about gardening"},'
        '"uuid":"u-1","timestamp":"2026-04-24T01:29:01.303Z",'
        '"sessionId":"s-1","cwd":"/tmp/repoA"}\n'
    )
    _write_config(home, projects.parent)
    monkeypatch.setenv("REKOL_HOME", str(home))

    runner = CliRunner()
    assert runner.invoke(session_idx_main, ["--full"]).exit_code == 0

    res = runner.invoke(search_main, ["xylophone-term-not-in-corpus", "--source", "sessions"])
    assert res.exit_code == 0, res.output
    assert "stale or inconsistent" not in res.output
