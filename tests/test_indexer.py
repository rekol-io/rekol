from pathlib import Path

import numpy as np
import pytest

from rekol.embeddings import HashingEmbedder
from rekol.indexer import Indexer
from rekol.store import CURATED_SCHEMA_VERSION, IndexStore


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


def test_index_md_derives_from_store_not_a_second_disk_walk(memory_root: Path) -> None:
    """C5: INDEX.md is derived from the just-built store, so it lists exactly the
    files (and names/tags/aliases) the index holds — never a second, possibly
    divergent, filesystem walk.

    We assert this by mutating the FILE ON DISK after the index is built and
    regenerating INDEX.md directly: the regenerated content must reflect the
    STORE (the old name/tags), not the freshly-edited disk file.
    """
    idx = _make_indexer(memory_root)
    idx.rebuild()
    index_md = (memory_root / ".index" / "INDEX.md").read_text()
    # The store's recorded name + tag + alias for prometheus.md show up.
    assert "Prometheus" in index_md
    assert "`prometheus`" in index_md  # tag
    assert "`prom`" in index_md  # alias

    # Now change the on-disk file's frontmatter name/tag, but do NOT re-index.
    prom = memory_root / "topics" / "prometheus.md"
    prom.write_text(
        "---\nname: RenamedOnDisk\ndescription: test\ntype: topic\n"
        "tags: ['disk-only-tag']\naliases: ['disk-only-alias']\n---\n\nbody\n"
    )
    idx._write_index_md()
    regenerated = (memory_root / ".index" / "INDEX.md").read_text()
    # Derived from the STORE: the disk-only edits must NOT appear; the indexed
    # name/tag/alias must persist.
    assert "RenamedOnDisk" not in regenerated
    assert "disk-only-tag" not in regenerated
    assert "disk-only-alias" not in regenerated
    assert "Prometheus" in regenerated


def test_index_md_omits_files_skipped_at_index_time(memory_root: Path) -> None:
    """C5: a file the indexer skips (bad frontmatter) is absent from the store and
    therefore consistently absent from INDEX.md — no second walk re-discovers it.
    """
    bad = memory_root / "topics" / "broken.md"
    bad.write_text("---\nname: ShouldNotAppear\n---\n\nbody but missing description/type\n")
    idx = _make_indexer(memory_root)
    stats = idx.rebuild()
    assert stats.files_skipped == 1

    index_md = (memory_root / ".index" / "INDEX.md").read_text()
    # The skipped file is in neither the store nor INDEX.md.
    assert "ShouldNotAppear" not in index_md
    assert "broken.md" not in index_md
    indexed_paths = {f["path"] for f in idx.store.all_files()}
    assert str(bad) not in indexed_paths


def test_index_md_projects_grouping_from_store(memory_root: Path) -> None:
    """C5: project-scoped files (projects/<slug>/<layer>/*.md) still group by slug
    when INDEX.md is derived from the store."""
    proj = memory_root / "projects" / "acme" / "topics" / "deploy.md"
    proj.parent.mkdir(parents=True, exist_ok=True)
    proj.write_text(
        "---\nname: AcmeDeploy\ndescription: how acme deploys\ntype: topic\n"
        "tags: ['deploy']\naliases: []\n---\n\n# Deploy\n\nSteps here.\n"
    )
    idx = _make_indexer(memory_root)
    idx.rebuild()
    index_md = (memory_root / ".index" / "INDEX.md").read_text()
    assert "## projects/" in index_md
    assert "### acme" in index_md
    assert "AcmeDeploy" in index_md


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


# --------------------------- C2: atomic rebuild ---------------------------


def _searchable_paths(store: IndexStore, embedder: HashingEmbedder) -> set[str]:
    """Run a real search against ``store`` and return the file paths it surfaces.

    Used by the interrupt tests to prove the live index is not just non-empty in
    the ``files`` table but actually *queryable* — chunks + vectors are present
    and a cosine search returns hits.
    """
    vec = embedder.embed("alex senior engineer prometheus repos")
    hits = store.search(np.asarray(vec, dtype=np.float32), top_k=10)
    return {h["file_path"] for h in hits}


def test_killed_rebuild_leaves_old_index_intact_and_searchable(memory_root: Path) -> None:
    """C2: a rebuild that dies partway must leave the OLD ``index.db`` untouched
    and still searchable — never an empty/partial index.

    Today's wipe-then-readd rebuild would, on a kill, leave the live DB empty.
    The fix builds the new index into a temp DB next to the real one and only
    ``os.replace``-swaps it in once it is complete; a crash before the swap is
    a no-op on the live file. This test simulates the kill by raising partway
    through the per-file build (a flaky embedder), then asserts the live index
    still contains the original three files and returns them from search.
    """
    db_path = memory_root / ".index" / "index.db"
    store = IndexStore(db_path=db_path, dim=384, use_sqlite_vec=False, embedding_model="hash-1")
    store.init_schema()
    emb = HashingEmbedder(dim=384)
    idx = Indexer(memory_root=memory_root, store=store, embedder=emb)

    # First, a clean rebuild so the live index has real, searchable content.
    idx.rebuild()
    original_files = {f["path"] for f in store.all_files()}
    assert len(original_files) == 3
    before = _searchable_paths(store, emb)
    assert before == original_files

    # Now change a file so a second rebuild would produce DIFFERENT content, and
    # add a brand-new file whose presence in the (post-kill) index would prove
    # the swap leaked through.
    prom = memory_root / "topics" / "prometheus.md"
    prom.write_text(prom.read_text().replace("IaC repo", "the brand new sentinel location"))

    class KillMidRebuild(HashingEmbedder):
        """Raise on the SECOND file embedded to mimic a kill mid-rebuild."""

        def __init__(self) -> None:
            super().__init__(dim=384)
            self._files_seen = 0

        def embed_batch(self, texts):  # type: ignore[no-untyped-def]
            self._files_seen += 1
            if self._files_seen >= 2:
                raise RuntimeError("simulated kill mid-rebuild")
            return super().embed_batch(texts)

        def embed(self, text):  # type: ignore[no-untyped-def]
            return super().embed(text)

    idx.embedder = KillMidRebuild()
    with pytest.raises(RuntimeError, match="simulated kill mid-rebuild"):
        idx.rebuild()

    # The live index must be UNCHANGED: same three files, all still searchable,
    # and crucially still carrying the OLD content (no sentinel text leaked in).
    assert {f["path"] for f in store.all_files()} == original_files
    assert _searchable_paths(store, emb) == original_files
    chunk_text = "\n".join(c["text"] for c in store.all_chunks_for_file(str(prom)))
    assert "sentinel" not in chunk_text
    assert "IaC repo" in chunk_text
    store.close()


def test_killed_rebuild_via_failed_swap_leaves_old_index(memory_root: Path, monkeypatch) -> None:
    """C2: even if the build of the temp DB SUCCEEDS but the ``os.replace`` swap
    dies (power loss exactly at the swap), the old ``index.db`` must survive.

    Simulates the kill by making ``os.replace`` raise; asserts the live index is
    still the original content and a leftover temp DB does not become the live one.
    """
    import rekol.indexer as indexer_mod

    db_path = memory_root / ".index" / "index.db"
    store = IndexStore(db_path=db_path, dim=384, use_sqlite_vec=False, embedding_model="hash-1")
    store.init_schema()
    emb = HashingEmbedder(dim=384)
    idx = Indexer(memory_root=memory_root, store=store, embedder=emb)
    idx.rebuild()
    original_files = {f["path"] for f in store.all_files()}
    assert len(original_files) == 3

    prom = memory_root / "topics" / "prometheus.md"
    prom.write_text(prom.read_text().replace("IaC repo", "sentinel-after-swap-fail"))

    def boom(src, dst):  # type: ignore[no-untyped-def]
        raise OSError("simulated power loss at swap")

    monkeypatch.setattr(indexer_mod.os, "replace", boom)
    with pytest.raises(OSError, match="simulated power loss at swap"):
        idx.rebuild()

    # Old index intact and searchable; no sentinel text leaked through.
    assert {f["path"] for f in store.all_files()} == original_files
    assert _searchable_paths(store, emb) == original_files
    chunk_text = "\n".join(c["text"] for c in store.all_chunks_for_file(str(prom)))
    assert "sentinel-after-swap-fail" not in chunk_text
    store.close()


def test_successful_rebuild_atomically_swaps_new_content_and_stamps_identity(
    memory_root: Path,
) -> None:
    """C2: a clean rebuild atomically replaces the live index with the NEW content
    and the swapped-in DB is a complete, valid, identity-stamped index (C4 carried
    through): metadata embedding_model/dim + the correct ``user_version``.
    """
    db_path = memory_root / ".index" / "index.db"
    store = IndexStore(db_path=db_path, dim=384, use_sqlite_vec=False, embedding_model="hash-1")
    store.init_schema()
    emb = HashingEmbedder(dim=384)
    idx = Indexer(memory_root=memory_root, store=store, embedder=emb)
    idx.rebuild()

    # Add a new file, then rebuild — the swapped-in DB must contain it.
    new_file = memory_root / "knowledge" / "sentinel.md"
    new_file.parent.mkdir(parents=True, exist_ok=True)
    new_file.write_text(
        "---\nname: Sentinel\ndescription: a brand new note\ntype: knowledge\n"
        "tags: []\naliases: []\n---\n\n# Sentinel\n\nUnique marker text here.\n"
    )
    stats = idx.rebuild()
    assert stats.files_indexed == 4

    paths = {f["path"] for f in store.all_files()}
    assert str(new_file) in paths

    # The swapped-in DB is identity-stamped (C4) and schema-versioned correctly.
    assert store.get_metadata("embedding_model") == "hash-1"
    assert store.get_metadata("embedding_dim") == "384"
    assert store.get_metadata("schema_version") == str(CURATED_SCHEMA_VERSION)
    user_version = int(store.conn.execute("PRAGMA user_version").fetchone()[0])
    assert user_version == CURATED_SCHEMA_VERSION
    # A configured-model identity check passes against the swapped-in DB.
    store.check_model_identity("hash-1", 384)
    # The new file is actually searchable through the swapped-in store.
    vec = emb.embed("unique marker sentinel")
    hits = store.search(np.asarray(vec, dtype=np.float32), top_k=5)
    assert any(h["file_path"] == str(new_file) for h in hits)
    store.close()


def test_rebuild_cleans_up_stale_temp_db(memory_root: Path) -> None:
    """C2: a temp DB left behind by a previously-killed rebuild must be removed on
    the next rebuild, not accumulate or get mistaken for the live index.
    """
    db_path = memory_root / ".index" / "index.db"
    store = IndexStore(db_path=db_path, dim=384, use_sqlite_vec=False, embedding_model="hash-1")
    store.init_schema()
    idx = Indexer(memory_root=memory_root, store=store, embedder=HashingEmbedder(dim=384))

    # Plant a stale temp file mimicking a killed rebuild's leftover.
    stale = db_path.with_name(db_path.name + ".tmp-999999")
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("garbage from a dead rebuild")

    idx.rebuild()

    # No temp files survive a completed rebuild.
    leftovers = list(db_path.parent.glob("index.db.tmp-*"))
    assert leftovers == [], f"stale temp files left behind: {leftovers}"
    assert not stale.exists()
    store.close()
