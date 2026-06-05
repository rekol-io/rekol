from pathlib import Path

import pytest

from rekol.embeddings import HashingEmbedder
from rekol.indexer import Indexer
from rekol.store import IndexStore


def _write(
    path: Path, name: str, type_: str, body: str, tags: list[str] = (), aliases: list[str] = ()
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        "---\n"
        f"name: {name}\n"
        f"description: test\n"
        f"type: {type_}\n"
        f"tags: {list(tags)}\n"
        f"aliases: {list(aliases)}\n"
        "---\n\n"
    )
    path.write_text(fm + body)


@pytest.fixture()
def memory_root(tmp_path: Path) -> Path:
    root = tmp_path / "mem"
    _write(
        root / "always" / "identity.md",
        "Identity",
        "always",
        "# Identity\n\nAlex is a senior engineer.\n",
        tags=["identity"],
        aliases=["me"],
    )
    _write(
        root / "topics" / "prometheus.md",
        "Prometheus",
        "topic",
        "# Prometheus\n\nURL lives in the IaC repo.\n",
        tags=["prometheus", "urls"],
        aliases=["prom"],
    )
    _write(
        root / "when" / "when-touching-repos.md",
        "Repos",
        "when",
        "# Repos\n\nAlways check the local symlink folder first.\n",
        tags=["repos"],
        aliases=["repo lookup"],
    )
    return root


def _make_indexer(memory_root: Path) -> Indexer:
    store = IndexStore(db_path=memory_root / ".index" / "index.db", dim=384, use_sqlite_vec=False)
    store.init_schema()
    emb = HashingEmbedder(dim=384)
    return Indexer(memory_root=memory_root, store=store, embedder=emb)


def test_indexer_carries_frontmatter_timestamps(tmp_path: Path) -> None:
    root = tmp_path / "mem"
    (root / "topics").mkdir(parents=True)
    (root / "topics" / "t.md").write_text(
        "---\nname: t\ndescription: d\ntype: topic\n"
        "created: 2026-01-01\nupdated: 2026-02-01\ninvalidated_at: 2026-03-01\n---\nbody\n"
    )
    store = IndexStore(db_path=root / ".index" / "index.db", dim=384, use_sqlite_vec=False)
    store.init_schema()
    Indexer(memory_root=root, store=store, embedder=HashingEmbedder(dim=384)).rebuild()
    row = store.conn.execute(
        "SELECT created, updated, invalidated_at FROM chunks LIMIT 1"
    ).fetchone()
    assert (row["created"], row["updated"], row["invalidated_at"]) == (
        "2026-01-01",
        "2026-02-01",
        "2026-03-01",
    )
    store.close()


def test_rebuild_indexes_all_files(memory_root: Path) -> None:
    idx = _make_indexer(memory_root)
    stats = idx.rebuild()
    assert stats.files_indexed == 3
    assert stats.chunks_written >= 3


def test_update_reindexes_only_changed_files(memory_root: Path) -> None:
    idx = _make_indexer(memory_root)
    idx.rebuild()
    # unchanged update → nothing reindexed
    stats = idx.update()
    assert stats.files_indexed == 0
    # modify one file
    p = memory_root / "topics" / "prometheus.md"
    body = p.read_text() + "\n\nNew paragraph.\n"
    p.write_text(body)
    stats = idx.update()
    assert stats.files_indexed == 1


def test_update_removes_deleted_files_from_index(memory_root: Path) -> None:
    idx = _make_indexer(memory_root)
    idx.rebuild()
    p = memory_root / "topics" / "prometheus.md"
    p.unlink()
    stats = idx.update()
    assert stats.files_removed == 1
    assert idx.store.get_file(str(p)) is None


def test_rebuild_writes_INDEX_md(memory_root: Path) -> None:
    idx = _make_indexer(memory_root)
    idx.rebuild()
    # INDEX.md lives under .index/ to keep the memory_root surface clean —
    # only MEMORY.md (always-on) belongs at the root.
    index_md = (memory_root / ".index" / "INDEX.md").read_text()
    assert "prometheus" in index_md.lower()
    assert "repos" in index_md.lower()
    assert "# Memory Index" in index_md
    # Legacy root-level INDEX.md must not exist after a rebuild
    assert not (memory_root / "INDEX.md").exists()


def test_rebuild_removes_legacy_root_INDEX_md(memory_root: Path) -> None:
    """A pre-existing root-level INDEX.md from older installs must be cleaned
    up so Claude does not speculatively read 6KB of pointers as memory."""
    legacy = memory_root / "INDEX.md"
    legacy.write_text("# Legacy stale content\n")
    idx = _make_indexer(memory_root)
    idx.rebuild()
    assert not legacy.exists()
    assert (memory_root / ".index" / "INDEX.md").is_file()


def test_index_md_follows_cache_dir_not_memory_root(memory_root: Path, tmp_path: Path) -> None:
    # SECURITY/relocation: when the index dir is a separate cache (outside the
    # memory root), INDEX.md is written there too — $REKOL_HOME holds ZERO
    # derived state.
    cache_dir = tmp_path / "cache" / "rekol" / "abc123"
    store = IndexStore(db_path=cache_dir / "index.db", dim=384, use_sqlite_vec=False)
    store.init_schema()
    idx = Indexer(
        memory_root=memory_root,
        store=store,
        embedder=HashingEmbedder(dim=384),
        index_dir=cache_dir,
    )
    idx.rebuild()
    assert (cache_dir / "INDEX.md").is_file()
    # Nothing derived lands under the (syncable) memory root.
    assert not (memory_root / ".index").exists()
    assert not (memory_root / "INDEX.md").exists()


def test_skips_files_with_bad_frontmatter(memory_root: Path) -> None:
    bad = memory_root / "topics" / "broken.md"
    bad.write_text("no frontmatter at all\n")
    idx = _make_indexer(memory_root)
    stats = idx.rebuild()
    # 3 good files still indexed; broken skipped
    assert stats.files_indexed == 3
    assert stats.files_skipped == 1


def test_skipped_files_record_path_and_reason(memory_root: Path) -> None:
    """#34: skips must be reportable, not just a count — carry path + reason so
    the CLI can name the offender instead of leaving the user with a silent
    near-empty index."""
    missing_fm = memory_root / "topics" / "broken.md"
    missing_fm.write_text("no frontmatter at all\n")
    missing_name = memory_root / "topics" / "no-name.md"
    missing_name.write_text("---\ndescription: d\ntype: topic\n---\n\nbody\n")
    idx = _make_indexer(memory_root)
    stats = idx.rebuild()

    assert stats.files_skipped == 2
    skipped = dict(stats.skipped_files)
    assert str(missing_fm) in skipped
    assert str(missing_name) in skipped
    # The reason for the no-name file names the missing required field.
    assert "name" in skipped[str(missing_name)]
    # Reason is just the cause, not the path duplicated back in.
    assert str(missing_name) not in skipped[str(missing_name)]


def test_update_records_skipped_files(memory_root: Path) -> None:
    """The incremental path (the auto-reindex hook target) reports skips too."""
    idx = _make_indexer(memory_root)
    idx.rebuild()
    bad = memory_root / "topics" / "later-broken.md"
    bad.write_text("---\nname: x\n---\n\nbody but no description or type\n")
    stats = idx.update()
    assert stats.files_skipped == 1
    assert stats.skipped_files[0][0] == str(bad)


def test_rebuild_rolls_back_file_on_embed_failure(memory_root: Path) -> None:
    """If the embedder raises after the files row is upserted, that file must be removed."""

    class FlakyEmbedder(HashingEmbedder):
        def __init__(self, fail_on_name: str) -> None:
            super().__init__(dim=384)
            self._fail_on_name = fail_on_name
            self._calls = 0

        def embed_batch(self, texts):
            # Raise when embedding the Prometheus file's chunks

            if any(self._fail_on_name in t for t in texts):
                raise RuntimeError("simulated embedder failure")
            return super().embed_batch(texts)

    store = IndexStore(db_path=memory_root / ".index" / "index.db", dim=384, use_sqlite_vec=False)
    store.init_schema()
    emb = FlakyEmbedder(fail_on_name="Prometheus")
    idx = Indexer(memory_root=memory_root, store=store, embedder=emb)

    with pytest.raises(RuntimeError):
        idx.rebuild()

    # The Prometheus file must NOT be in the files table (rolled back)
    prom_path = str(memory_root / "topics" / "prometheus.md")
    assert store.get_file(prom_path) is None
    store.close()


def test_failed_write_does_not_advance_hash_so_update_retries(memory_root: Path) -> None:
    """C1 (#18 root): a crash while writing a CHANGED file must NOT advance its
    content_hash. If it did, the next incremental ``update()`` would see the new
    hash, decide "unchanged", and skip the file forever — leaving stale/missing
    chunks behind. This test simulates the crash by failing the atomic store
    write for one file, then asserts the next update() re-indexes that file.
    """
    idx = _make_indexer(memory_root)
    idx.rebuild()

    prom = memory_root / "topics" / "prometheus.md"
    prom_path = str(prom)
    original = idx.store.get_file(prom_path)
    assert original is not None
    original_hash = original["content_hash"]

    # Edit the file so update() will try to re-index it.
    prom.write_text(prom.read_text() + "\n\nAn edit that changes the hash.\n")

    # Simulate a crash/kill during the chunk write for this one file: the atomic
    # method raises *after* it would have upserted the new hash. Because the
    # whole write is one transaction, the raise must roll the hash back too.
    real_replace = idx.store.replace_file_and_chunks

    def boom(path, *args, **kwargs):  # type: ignore[no-untyped-def]
        if path == prom_path:
            raise RuntimeError("simulated crash mid-write")
        return real_replace(path, *args, **kwargs)

    idx.store.replace_file_and_chunks = boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        idx.update()

    # The hash must be unchanged — the failed write left the OLD hash in place.
    after_crash = idx.store.get_file(prom_path)
    assert after_crash is not None
    assert after_crash["content_hash"] == original_hash

    # Restore the real method and run update() again: because the on-disk file
    # no longer matches the stored (old) hash, the file is RE-INDEXED rather than
    # silently skipped — proving the crash was retried cleanly.
    idx.store.replace_file_and_chunks = real_replace  # type: ignore[method-assign]
    stats = idx.update()
    assert stats.files_indexed == 1
    reindexed = idx.store.get_file(prom_path)
    assert reindexed is not None
    assert reindexed["content_hash"] != original_hash

    idx.store.close()
