from pathlib import Path

import pytest

from memory_tools.embeddings import HashingEmbedder
from memory_tools.indexer import Indexer
from memory_tools.store import IndexStore


def _write(path: Path, name: str, type_: str, body: str,
           tags: list[str] = (), aliases: list[str] = ()) -> None:
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
    _write(root / "always" / "identity.md", "Identity", "always",
           "# Identity\n\nLeon is a senior manager.\n",
           tags=["identity"], aliases=["me"])
    _write(root / "topics" / "prometheus.md", "Prometheus", "topic",
           "# Prometheus\n\nURL lives in the IaC repo.\n",
           tags=["prometheus", "urls"], aliases=["prom"])
    _write(root / "when" / "when-touching-repos.md", "Repos", "when",
           "# Repos\n\nAlways check the local symlink folder first.\n",
           tags=["repos"], aliases=["repo lookup"])
    return root


def _make_indexer(memory_root: Path) -> Indexer:
    store = IndexStore(db_path=memory_root / ".index" / "index.db",
                       dim=384, use_sqlite_vec=False)
    store.init_schema()
    emb = HashingEmbedder(dim=384)
    return Indexer(memory_root=memory_root, store=store, embedder=emb)


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
    index_md = (memory_root / "INDEX.md").read_text()
    assert "prometheus" in index_md.lower()
    assert "repos" in index_md.lower()
    assert "# Memory Index" in index_md


def test_skips_files_with_bad_frontmatter(memory_root: Path, tmp_path: Path) -> None:
    bad = memory_root / "topics" / "broken.md"
    bad.write_text("no frontmatter at all\n")
    idx = _make_indexer(memory_root)
    stats = idx.rebuild()
    # 3 good files still indexed; broken skipped
    assert stats.files_indexed == 3
    assert stats.files_skipped == 1
