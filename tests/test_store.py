from pathlib import Path

import numpy as np
import pytest

from rekol.store import (
    CURATED_SCHEMA_VERSION,
    IndexModelMismatchError,
    IndexStore,
)


@pytest.fixture()
def store(tmp_path: Path) -> IndexStore:
    db_path = tmp_path / "index.db"
    s = IndexStore(db_path=db_path, dim=8, use_sqlite_vec=False)
    s.init_schema()
    yield s
    s.close()


def test_init_schema_creates_tables(store: IndexStore) -> None:
    tables = store.list_tables()
    assert {"files", "chunks"}.issubset(tables)


def test_upsert_and_fetch_file(store: IndexStore, tmp_path: Path) -> None:
    p = tmp_path / "a.md"
    p.write_text("dummy")
    store.upsert_file(path=str(p), mtime=100, content_hash="abc")
    rec = store.get_file(str(p))
    assert rec is not None
    assert rec["content_hash"] == "abc"
    assert rec["mtime"] == 100


def test_upsert_file_is_idempotent_on_same_hash(store: IndexStore, tmp_path: Path) -> None:
    p = tmp_path / "a.md"
    p.write_text("dummy")
    store.upsert_file(path=str(p), mtime=100, content_hash="abc")
    store.upsert_file(path=str(p), mtime=200, content_hash="abc")
    rec = store.get_file(str(p))
    assert rec["mtime"] == 200
    assert rec["content_hash"] == "abc"


def test_replace_chunks_and_search(store: IndexStore, tmp_path: Path) -> None:
    p = tmp_path / "prom.md"
    p.write_text("dummy")
    store.upsert_file(path=str(p), mtime=1, content_hash="h1")

    chunks = [
        dict(
            heading="Prom",
            line_start=1,
            line_end=5,
            text="prometheus url is in iac",
            tags=["prometheus", "urls"],
            aliases=["prom"],
            embedding=np.array([1, 0, 0, 0, 0, 0, 0, 0], dtype=np.float32),
        ),
        dict(
            heading="Reaper",
            line_start=6,
            line_end=10,
            text="reaper schedule is configured via api",
            tags=["reaper"],
            aliases=[],
            embedding=np.array([0, 1, 0, 0, 0, 0, 0, 0], dtype=np.float32),
        ),
    ]
    store.replace_chunks_for_file(str(p), chunks)

    query = np.array([1, 0, 0, 0, 0, 0, 0, 0], dtype=np.float32)
    hits = store.search(query, top_k=2)
    assert len(hits) == 2
    assert hits[0]["heading"] == "Prom"
    assert hits[0]["score"] > hits[1]["score"]


def test_replace_chunks_removes_old_chunks(store: IndexStore, tmp_path: Path) -> None:
    p = tmp_path / "x.md"
    p.write_text("dummy")
    store.upsert_file(path=str(p), mtime=1, content_hash="h")
    v = np.zeros(8, dtype=np.float32)
    v[0] = 1.0
    store.replace_chunks_for_file(
        str(p),
        [
            dict(heading="A", line_start=1, line_end=2, text="a", tags=[], aliases=[], embedding=v),
        ],
    )
    store.replace_chunks_for_file(
        str(p),
        [
            dict(heading="B", line_start=1, line_end=2, text="b", tags=[], aliases=[], embedding=v),
        ],
    )
    all_chunks = store.all_chunks_for_file(str(p))
    assert len(all_chunks) == 1
    assert all_chunks[0]["heading"] == "B"


def test_delete_file_removes_chunks(store: IndexStore, tmp_path: Path) -> None:
    p = tmp_path / "x.md"
    p.write_text("dummy")
    store.upsert_file(path=str(p), mtime=1, content_hash="h")
    v = np.zeros(8, dtype=np.float32)
    v[0] = 1.0
    store.replace_chunks_for_file(
        str(p),
        [
            dict(heading="A", line_start=1, line_end=2, text="a", tags=[], aliases=[], embedding=v),
        ],
    )
    store.delete_file(str(p))
    assert store.get_file(str(p)) is None
    assert store.all_chunks_for_file(str(p)) == []


def test_index_store_is_context_manager(tmp_path: Path) -> None:
    db_path = tmp_path / "index.db"
    with IndexStore(db_path=db_path, dim=8, use_sqlite_vec=False) as s:
        s.init_schema()
        assert "files" in s.list_tables()
    # After exit, the underlying connection should be closed.
    # A second operation on s.conn should raise ProgrammingError.
    with pytest.raises(Exception):
        s.conn.execute("SELECT 1")


def test_fresh_schema_has_timestamp_columns(store: IndexStore) -> None:
    cols = {r["name"] for r in store.conn.execute("PRAGMA table_info(chunks)")}
    assert {"created", "updated", "valid_from", "invalidated_at"}.issubset(cols)


def test_fresh_store_needs_no_migration(store: IndexStore) -> None:
    assert store.needs_schema_migration() is False


def test_legacy_schema_needs_migration(tmp_path: Path) -> None:
    db = tmp_path / "legacy.db"
    s = IndexStore(db_path=db, dim=8, use_sqlite_vec=False)
    s.conn.execute("DROP TABLE IF EXISTS chunks")
    s.conn.execute(
        "CREATE TABLE chunks (id INTEGER PRIMARY KEY, file_path TEXT, heading TEXT, "
        "line_start INTEGER, line_end INTEGER, text TEXT, tags_json TEXT, "
        "aliases_json TEXT, embedding BLOB)"
    )
    s.conn.commit()
    assert s.needs_schema_migration() is True
    s.close()


def test_replace_chunks_persists_file_timestamps(store: IndexStore, tmp_path: Path) -> None:
    p = tmp_path / "topics" / "x.md"
    p.parent.mkdir(parents=True)
    p.write_text("dummy")
    store.upsert_file(path=str(p), mtime=1, content_hash="h")
    store.replace_chunks_for_file(
        str(p),
        [
            dict(
                heading=None,
                line_start=1,
                line_end=2,
                text="t",
                tags=[],
                aliases=[],
                embedding=np.ones(8, dtype=np.float32),
            )
        ],
        created="2026-01-01",
        updated="2026-02-01",
        valid_from="2026-01-01",
        invalidated_at=None,
    )
    row = store.conn.execute(
        "SELECT created, updated, valid_from, invalidated_at FROM chunks WHERE file_path=?",
        (str(p),),
    ).fetchone()
    assert (row["created"], row["updated"], row["valid_from"], row["invalidated_at"]) == (
        "2026-01-01",
        "2026-02-01",
        "2026-01-01",
        None,
    )


def test_search_returns_timestamps_and_cosine_score(store: IndexStore, tmp_path: Path) -> None:
    p = tmp_path / "topics" / "y.md"
    p.parent.mkdir(parents=True)
    p.write_text("dummy")
    store.upsert_file(path=str(p), mtime=1, content_hash="h")
    store.replace_chunks_for_file(
        str(p),
        [
            dict(
                heading=None,
                line_start=1,
                line_end=2,
                text="t",
                tags=[],
                aliases=[],
                embedding=np.ones(8, dtype=np.float32),
            )
        ],
        created="2026-01-01",
        updated="2026-02-01",
        valid_from="2026-01-01",
        invalidated_at="2026-03-01",
    )
    hits = store.search(np.ones(8, dtype=np.float32), top_k=1)
    h = hits[0]
    assert "cosine_score" in h
    assert h["created"] == "2026-01-01" and h["invalidated_at"] == "2026-03-01"


def test_replace_file_and_chunks_is_atomic(store: IndexStore, tmp_path: Path) -> None:
    """C1 (#18 root): the combined file-row + chunks write is ONE transaction.

    Seed a file at hash ``old`` with one chunk, then attempt
    ``replace_file_and_chunks`` to hash ``new`` where the SECOND chunk is
    malformed (no ``line_start``) so the INSERT loop raises *after* the files
    UPSERT and the chunk DELETE have already run inside the transaction. If the
    write were not atomic we would be left with hash ``new`` and zero/partial
    chunks — the exact silent-corruption #18 caused. The assertion proves the
    whole thing rolled back: hash is still ``old`` and the old chunk survives.
    """
    p = tmp_path / "topics" / "atomic.md"
    p.parent.mkdir(parents=True)
    p.write_text("dummy")
    good = np.ones(8, dtype=np.float32)
    store.replace_file_and_chunks(
        str(p),
        mtime=1,
        content_hash="old",
        chunks=[
            dict(
                heading="orig",
                line_start=1,
                line_end=2,
                text="orig",
                tags=[],
                aliases=[],
                embedding=good,
            ),
        ],
    )

    malformed_chunks = [
        dict(
            heading="new1", line_start=1, line_end=2, text="n1", tags=[], aliases=[], embedding=good
        ),
        # Missing line_start → int(c["line_start"]) raises KeyError mid-INSERT,
        # *after* the files UPSERT (to hash "new") and the chunk DELETE.
        dict(heading="new2", line_end=2, text="n2", tags=[], aliases=[], embedding=good),
    ]
    with pytest.raises(KeyError):
        store.replace_file_and_chunks(str(p), mtime=2, content_hash="new", chunks=malformed_chunks)

    # The hash must NOT have advanced — the transaction rolled back entirely.
    rec = store.get_file(str(p))
    assert rec is not None
    assert rec["content_hash"] == "old"
    assert rec["mtime"] == 1
    # The original chunk must still be present (DELETE rolled back too).
    surviving = store.all_chunks_for_file(str(p))
    assert len(surviving) == 1
    assert surviving[0]["heading"] == "orig"


# --------------------------- C4: index identity -----------------------------


def _seed_one_chunk(store: IndexStore, path: str) -> None:
    """Write a single file + chunk so the index is non-empty and stamped."""
    v = np.zeros(8, dtype=np.float32)
    v[0] = 1.0
    store.replace_file_and_chunks(
        path,
        mtime=1,
        content_hash="h",
        chunks=[
            dict(heading="A", line_start=1, line_end=2, text="a", tags=[], aliases=[], embedding=v),
        ],
    )


def test_init_schema_creates_metadata_table(tmp_path: Path) -> None:
    s = IndexStore(db_path=tmp_path / "i.db", dim=8, use_sqlite_vec=False, embedding_model="m-a")
    try:
        s.init_schema()
        assert "metadata" in s.list_tables()
        assert s.get_metadata("schema_version") == str(CURATED_SCHEMA_VERSION)
        assert s.get_metadata("embedding_model") == "m-a"
        assert s.get_metadata("embedding_dim") == "8"
    finally:
        s.close()


def test_user_version_stamped_to_current(store: IndexStore) -> None:
    version = store.conn.execute("PRAGMA user_version").fetchone()[0]
    assert int(version) == CURATED_SCHEMA_VERSION


def test_writes_keep_identity_current(tmp_path: Path) -> None:
    """C4: a write stamps/keeps the embedding identity in metadata."""
    s = IndexStore(db_path=tmp_path / "i.db", dim=8, use_sqlite_vec=False, embedding_model="m-a")
    try:
        s.init_schema()
        _seed_one_chunk(s, str(tmp_path / "x.md"))
        assert s.get_metadata("embedding_model") == "m-a"
        assert s.get_metadata("embedding_dim") == "8"
    finally:
        s.close()


def test_check_model_identity_passes_on_match(tmp_path: Path) -> None:
    s = IndexStore(db_path=tmp_path / "i.db", dim=8, use_sqlite_vec=False, embedding_model="m-a")
    try:
        s.init_schema()
        _seed_one_chunk(s, str(tmp_path / "x.md"))
        # Same model + dim → no raise.
        s.check_model_identity("m-a", 8)
    finally:
        s.close()


def test_search_after_model_change_raises_not_wrong_results(tmp_path: Path) -> None:
    """TDD (a): build an index with one model, then a different configured model
    must raise the loud mismatch error rather than serve confidently-wrong hits.
    """
    db = tmp_path / "i.db"
    builder = IndexStore(db_path=db, dim=8, use_sqlite_vec=False, embedding_model="model-a")
    try:
        builder.init_schema()
        _seed_one_chunk(builder, str(tmp_path / "x.md"))
    finally:
        builder.close()

    # Reopen as a different configured model (same dim) — the search-path guard.
    reader = IndexStore(db_path=db, dim=8, use_sqlite_vec=False, embedding_model="model-b")
    try:
        reader.init_schema()
        # init must NOT have overwritten the recorded identity (that would mask
        # the mismatch). The stored model is still the one that built the index.
        assert reader.get_metadata("embedding_model") == "model-a"
        with pytest.raises(IndexModelMismatchError) as excinfo:
            reader.check_model_identity("model-b", 8)
        assert excinfo.value.stored_model == "model-a"
        assert excinfo.value.wanted_model == "model-b"
    finally:
        reader.close()


def test_check_model_identity_detects_dim_change(tmp_path: Path) -> None:
    db = tmp_path / "i.db"
    builder = IndexStore(db_path=db, dim=8, use_sqlite_vec=False, embedding_model="model-a")
    try:
        builder.init_schema()
        _seed_one_chunk(builder, str(tmp_path / "x.md"))
    finally:
        builder.close()
    reader = IndexStore(db_path=db, dim=16, use_sqlite_vec=False, embedding_model="model-a")
    try:
        reader.init_schema()
        with pytest.raises(IndexModelMismatchError) as excinfo:
            reader.check_model_identity("model-a", 16)
        assert excinfo.value.stored_dim == 8
        assert excinfo.value.wanted_dim == 16
    finally:
        reader.close()


def test_user_version_bump_triggers_migration(tmp_path: Path) -> None:
    """TDD (b): an index whose user_version is below current → needs migration."""
    s = IndexStore(db_path=tmp_path / "i.db", dim=8, use_sqlite_vec=False, embedding_model="m-a")
    try:
        s.init_schema()
        assert s.needs_schema_migration() is False
        # Simulate an index stamped by an older schema version.
        s.conn.execute(f"PRAGMA user_version = {CURATED_SCHEMA_VERSION - 1}")
        s.conn.commit()
        assert s.needs_schema_migration() is True
    finally:
        s.close()


def test_index_missing_metadata_table_needs_rebuild(tmp_path: Path) -> None:
    """TDD (c): a pre-C4 index (timestamp columns present, NO metadata table,
    user_version 0 = pre-versioning) is unknown provenance → needs a rebuild.
    """
    db = tmp_path / "legacy.db"
    s = IndexStore(db_path=db, dim=8, use_sqlite_vec=False)
    try:
        # Build a version-2-shaped index by hand: chunks WITH the timestamp
        # columns but no metadata table, and user_version left at 0.
        s.conn.executescript(
            "CREATE TABLE files (path TEXT PRIMARY KEY, mtime INTEGER, content_hash TEXT, "
            "indexed_at INTEGER);"
            "CREATE TABLE chunks (id INTEGER PRIMARY KEY, file_path TEXT, heading TEXT, "
            "line_start INTEGER, line_end INTEGER, text TEXT, tags_json TEXT, aliases_json TEXT, "
            "created TEXT, updated TEXT, valid_from TEXT, invalidated_at TEXT, embedding BLOB);"
        )
        s.conn.commit()
        assert "metadata" not in s.list_tables()
        assert int(s.conn.execute("PRAGMA user_version").fetchone()[0]) == 0
        # Unknown provenance must be treated as needs-rebuild, never silently trusted.
        assert s.needs_schema_migration() is True
    finally:
        s.close()


def test_distinct_file_timestamps_one_row_per_file(store: IndexStore, tmp_path: Path) -> None:
    p = tmp_path / "topics" / "multi.md"
    p.parent.mkdir(parents=True)
    p.write_text("x")
    store.upsert_file(path=str(p), mtime=1, content_hash="h")
    store.replace_chunks_for_file(
        str(p),
        [
            dict(
                heading=None,
                line_start=1,
                line_end=1,
                text="a",
                tags=[],
                aliases=[],
                embedding=np.ones(8, dtype=np.float32),
            ),
            dict(
                heading=None,
                line_start=2,
                line_end=2,
                text="b",
                tags=[],
                aliases=[],
                embedding=np.ones(8, dtype=np.float32),
            ),
        ],
        created="2026-01-01",
        updated="2026-02-01",
    )
    rows = store.distinct_file_timestamps()
    assert len([r for r in rows if r["file_path"] == str(p)]) == 1


# --------------------------- C3: concurrency ---------------------------


def test_index_opens_in_wal_mode(tmp_path: Path) -> None:
    """The curated index must run in WAL so the background auto-reindex writer
    and a concurrent search reader don't block each other to failure (C3).
    """
    s = IndexStore(db_path=tmp_path / "i.db", dim=8, use_sqlite_vec=False)
    try:
        mode = s.conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert str(mode).lower() == "wal"
    finally:
        s.close()


def test_index_sets_busy_timeout(tmp_path: Path) -> None:
    """A 30s busy_timeout lets a reader wait out a writer's lock instead of
    raising 'database is locked' immediately (C3).
    """
    s = IndexStore(db_path=tmp_path / "i.db", dim=8, use_sqlite_vec=False)
    try:
        timeout_ms = int(s.conn.execute("PRAGMA busy_timeout").fetchone()[0])
        assert timeout_ms == 30000
    finally:
        s.close()


def test_reopen_preserves_wal_and_busy_timeout(tmp_path: Path) -> None:
    """The atomic-rebuild swap reopens the connection; the concurrency pragmas
    must survive the reopen, not just the first open.
    """
    db = tmp_path / "i.db"
    s = IndexStore(db_path=db, dim=8, use_sqlite_vec=False)
    s.init_schema()
    try:
        s.reopen()
        mode = s.conn.execute("PRAGMA journal_mode").fetchone()[0]
        timeout_ms = int(s.conn.execute("PRAGMA busy_timeout").fetchone()[0])
        assert str(mode).lower() == "wal"
        assert timeout_ms == 30000
    finally:
        s.close()


def test_concurrent_reader_and_writer_do_not_lock(tmp_path: Path) -> None:
    """A separate connection reading the same index while a writer commits in a
    loop must never raise 'database is locked'. WAL + busy_timeout is what makes
    this hold — the auto-reindex writer and a live search reader overlap in
    production (C3).
    """
    import threading

    db = tmp_path / "i.db"
    writer = IndexStore(db_path=db, dim=8, use_sqlite_vec=False)
    writer.init_schema()
    p = tmp_path / "a.md"
    p.write_text("x")
    writer.upsert_file(path=str(p), mtime=1, content_hash="h0")

    errors: list[Exception] = []
    stop = threading.Event()

    def read_loop() -> None:
        # Open the reader's own IndexStore INSIDE the thread: this models the
        # real production case (the search reader is a separate connection/
        # process from the background auto-reindex writer) and avoids SQLite's
        # cross-thread guard. Without WAL + busy_timeout this loop would raise
        # "database is locked" as the writer commits.
        reader = IndexStore(db_path=db, dim=8, use_sqlite_vec=False)
        try:
            while not stop.is_set():
                reader.conn.execute("SELECT COUNT(*) FROM files").fetchone()
        except Exception as exc:  # noqa: BLE001 — surface ANY lock error to the test
            errors.append(exc)
        finally:
            reader.close()

    t = threading.Thread(target=read_loop)
    t.start()
    try:
        for i in range(50):
            v = np.zeros(8, dtype=np.float32)
            v[0] = 1.0
            writer.replace_file_and_chunks(
                str(p),
                mtime=i,
                content_hash=f"h{i}",
                chunks=[
                    dict(
                        heading=None,
                        line_start=1,
                        line_end=1,
                        text="a",
                        tags=[],
                        aliases=[],
                        embedding=v,
                    )
                ],
            )
    finally:
        stop.set()
        t.join()
        writer.close()

    assert errors == [], f"concurrent read raised: {errors!r}"
