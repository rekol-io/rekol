from pathlib import Path

import numpy as np
import pytest

from memory_tools.store import IndexStore


@pytest.fixture()
def store(tmp_path: Path) -> IndexStore:
    db_path = tmp_path / "index.db"
    s = IndexStore(db_path=db_path, dim=8, use_sqlite_vec=False)
    s.init_schema()
    return s


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
    v = np.zeros(8, dtype=np.float32); v[0] = 1.0
    store.replace_chunks_for_file(str(p), [
        dict(heading="A", line_start=1, line_end=2, text="a", tags=[], aliases=[], embedding=v),
    ])
    store.replace_chunks_for_file(str(p), [
        dict(heading="B", line_start=1, line_end=2, text="b", tags=[], aliases=[], embedding=v),
    ])
    all_chunks = store.all_chunks_for_file(str(p))
    assert len(all_chunks) == 1
    assert all_chunks[0]["heading"] == "B"


def test_delete_file_removes_chunks(store: IndexStore, tmp_path: Path) -> None:
    p = tmp_path / "x.md"
    p.write_text("dummy")
    store.upsert_file(path=str(p), mtime=1, content_hash="h")
    v = np.zeros(8, dtype=np.float32); v[0] = 1.0
    store.replace_chunks_for_file(str(p), [
        dict(heading="A", line_start=1, line_end=2, text="a", tags=[], aliases=[], embedding=v),
    ])
    store.delete_file(str(p))
    assert store.get_file(str(p)) is None
    assert store.all_chunks_for_file(str(p)) == []
