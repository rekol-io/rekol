"""#193: the sessions store validated vector WIDTH only, never model identity."""

from __future__ import annotations

from pathlib import Path

import pytest

REAL = "BAAI/bge-small-en-v1.5"
OTHER_REAL = "sentence-transformers/all-MiniLM-L6-v2"  # also 384-dim
TEST = "test-hashing"  # ALSO 384-dim — which is the whole bug


def _store(tmp_path: Path, dim: int = 384):
    from rekol.sessions.store import SessionStore

    s = SessionStore(db_path=tmp_path / "sessions.db", dim=dim)
    s.init_schema()
    return s


def test_identity_is_stamped_on_a_fresh_store(tmp_path: Path) -> None:
    """A new store records what built it, so later runs can be compared."""
    s = _store(tmp_path)
    s.reconcile_embedder(REAL, 384)
    assert s.stored_embedding_model() == REAL
    s.close()


def test_same_model_reopens_cleanly(tmp_path: Path) -> None:
    s = _store(tmp_path)
    s.reconcile_embedder(REAL, 384)
    s.close()
    s2 = _store(tmp_path)
    s2.reconcile_embedder(REAL, 384)  # must not raise
    s2.close()


def test_DIFFERENT_real_model_at_the_SAME_width_is_refused(tmp_path: Path) -> None:
    """The gap #193 names: equal dimension is not equal semantics.

    Two real models can both be 384-dim. The old guard compared only width, so
    swapping models silently mixed incompatible vectors into one space and search
    returned confidently wrong neighbours.
    """
    from rekol.sessions.store import SessionStoreModelMismatchError

    s = _store(tmp_path)
    s.reconcile_embedder(REAL, 384)
    s.close()
    s2 = _store(tmp_path)
    with pytest.raises(SessionStoreModelMismatchError) as e:
        s2.reconcile_embedder(OTHER_REAL, 384)
    assert REAL in str(e.value) and OTHER_REAL in str(e.value)
    s2.close()


def test_test_embedder_over_a_real_store_is_refused(tmp_path: Path) -> None:
    """`test-hashing` is 384-dim exactly like bge-small, so width caught nothing.

    This is how a leaked REKOL_INDEX_DIR could poison a real sessions corpus
    without any mismatch being raised.
    """
    from rekol.sessions.store import SessionStoreModelMismatchError

    s = _store(tmp_path)
    s.reconcile_embedder(REAL, 384)
    s.close()
    s2 = _store(tmp_path)
    with pytest.raises(SessionStoreModelMismatchError):
        s2.reconcile_embedder(TEST, 384)
    s2.close()


def test_legacy_unstamped_store_adopts_a_REAL_model(tmp_path: Path) -> None:
    """Every existing sessions.db predates this table and must keep working.

    Refusing on unknown provenance would block ingest for every current user, so
    a populated-but-unstamped store adopts the configured model once — but only a
    real one (see the next test).
    """
    s = _store(tmp_path)
    # Realistic legacy shape: init_schema has run so the table exists, but
    # nothing ever stamped it. Dropping the table instead would test a state
    # init_schema makes impossible.
    s.conn.execute("DELETE FROM metadata")
    s.conn.commit()
    s.reconcile_embedder(REAL, 384)
    assert s.stored_embedding_model() == REAL
    s.close()


def test_legacy_unstamped_store_REFUSES_a_test_embedder(tmp_path: Path) -> None:
    """The one-time adoption window must not be usable to poison a legacy store.

    Keyed on EMBEDDINGS, not messages: the harm is a mixed vector space, and
    messages plus FTS are model-independent. A store with rows but no vectors has
    nothing a test embedder could semantically corrupt.
    """
    import numpy as np

    from rekol.sessions.store import SessionStoreModelMismatchError

    s = _store(tmp_path)
    rowid = s.insert_message(
        {
            "session_id": "s1",
            "message_uuid": "m1",
            "parent_uuid": None,
            "role": "user",
            "content": "legacy content",
            "cwd": "/tmp",
            "timestamp_iso": "2026-01-01T00:00:00Z",
            "timestamp_unix": 1767225600,
            "jsonl_path": "/tmp/x.jsonl",
            "line_number": 1,
        }
    )
    assert rowid is not None
    s.upsert_embedding(rowid, np.zeros(384, dtype=np.float32))
    s.conn.execute("DELETE FROM metadata")
    s.conn.commit()
    with pytest.raises(SessionStoreModelMismatchError):
        s.reconcile_embedder(TEST, 384)
    s.close()


def test_width_mismatch_still_raises_its_own_error(tmp_path: Path) -> None:
    """Adding identity must not swallow the pre-existing dim guard."""
    from rekol.sessions.store import SessionStoreDimMismatchError

    s = _store(tmp_path, dim=384)
    s.reconcile_embedder(REAL, 384)
    import numpy as np

    rowid = s.insert_message(
        {
            "session_id": "s1",
            "message_uuid": "m1",
            "parent_uuid": None,
            "role": "user",
            "content": "x",
            "cwd": "/tmp",
            "timestamp_iso": "2026-01-01T00:00:00Z",
            "timestamp_unix": 1767225600,
            "jsonl_path": "/tmp/x.jsonl",
            "line_number": 1,
        }
    )
    assert rowid is not None
    s.upsert_embedding(rowid, np.zeros(384, dtype=np.float32))
    s.close()
    s2 = _store(tmp_path, dim=768)
    with pytest.raises(SessionStoreDimMismatchError):
        s2.reconcile_embedder(REAL, 768)
    s2.close()
