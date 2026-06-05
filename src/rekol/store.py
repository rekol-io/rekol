"""SQLite-backed index store. Uses sqlite-vec when available; falls back to numpy cosine."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import numpy as np

# 1 = original schema
# 2 = with curated timestamp columns (created/updated/valid_from/invalidated_at)
# 3 = with the `metadata` identity table (embedding_model/embedding_dim/schema_version).
#     Bumped here because an index built by version 2 has no recorded provenance —
#     we cannot tell which embedding model produced its vectors, so it cannot be
#     trusted for a model-identity check and must be rebuilt (see Migration below).
# 4 = with the `files.name` column (the file's frontmatter `name`). INDEX.md is
#     now derived from the store (C5) instead of a second filesystem walk, so the
#     display name must live in the index. A version-3 index has no `name` column,
#     so it is rebuilt rather than read with a missing field.
CURATED_SCHEMA_VERSION = 4


class CuratedSchemaOutdatedError(RuntimeError):
    """Curated index predates the timestamp columns; a rebuild is required."""


class IndexModelMismatchError(RuntimeError):
    """Raised when the curated index was built by a different embedding model/dim.

    Mirrors :class:`rekol.sessions.store.SessionStoreDimMismatchError`. Carries
    the conflicting identities so callers can render an actionable rebuild
    message instead of returning confidently-wrong results from a mixed-model
    index — the silent-WRONG failure the #22 emptiness backstop cannot catch
    (a query against vectors from another model still returns *some* nearest
    neighbours, just meaningless ones).
    """

    def __init__(
        self,
        db_path: Path,
        stored_model: str | None,
        stored_dim: int | None,
        wanted_model: str,
        wanted_dim: int,
    ) -> None:
        self.db_path = db_path
        self.stored_model = stored_model
        self.stored_dim = stored_dim
        self.wanted_model = wanted_model
        self.wanted_dim = wanted_dim
        stored_desc = (
            f"{stored_model!r} ({stored_dim}-dim)"
            if stored_model is not None
            else "an unknown embedding model"
        )
        super().__init__(
            f"curated index at {db_path} was built with {stored_desc}, but the "
            f"configured model is {wanted_model!r} ({wanted_dim}-dim). Searching a "
            f"mixed-model index returns confidently-wrong results. Restore the "
            f"previous embedding_model, or rebuild the curated index:\n"
            f"  rekol index rebuild"
        )


SCHEMA_FILES = """
CREATE TABLE IF NOT EXISTS files (
    path          TEXT PRIMARY KEY,
    mtime         INTEGER NOT NULL,
    content_hash  TEXT NOT NULL,
    name          TEXT NOT NULL DEFAULT '',
    indexed_at    INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
);
"""

SCHEMA_CHUNKS = """
CREATE TABLE IF NOT EXISTS chunks (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path    TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
    heading      TEXT,
    line_start   INTEGER NOT NULL,
    line_end     INTEGER NOT NULL,
    text         TEXT NOT NULL,
    tags_json    TEXT NOT NULL DEFAULT '[]',
    aliases_json TEXT NOT NULL DEFAULT '[]',
    created        TEXT,
    updated        TEXT,
    valid_from     TEXT,
    invalidated_at TEXT,
    embedding    BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_file ON chunks(file_path);
"""

# Index identity (C4): records WHICH embedding model + schema version produced
# the on-disk vectors, so a later run with a different model is caught loudly
# instead of silently mixing incompatible vectors into one index.
SCHEMA_METADATA = """
CREATE TABLE IF NOT EXISTS metadata (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class IndexStore:
    """SQLite-backed vector index. Fallback path (numpy cosine) is always available."""

    def __init__(
        self,
        db_path: Path,
        dim: int = 384,
        use_sqlite_vec: bool = True,
        embedding_model: str | None = None,
    ) -> None:
        # ``embedding_model`` is the configured model name (from the embedder/
        # config building this index). It is stamped into ``metadata`` on init
        # so a later open with a different model can be caught. Callers that only
        # read may pass None; the identity check below still detects a mismatch
        # because they instead supply the configured model to ``check_model_identity``.
        self.db_path = Path(db_path)
        self.dim = dim
        self.embedding_model = embedding_model
        # Retained so the atomic rebuild (indexer.rebuild) can build a temp store
        # with the SAME vec-extension intent as this one, then swap it in.
        self.use_sqlite_vec = use_sqlite_vec
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        try:
            self.conn.row_factory = sqlite3.Row
            # WAL + busy_timeout (C3): the PostToolUse auto-reindex writer runs
            # in the background while a `search` reader may be live on the same
            # index. WAL lets readers proceed during a write; busy_timeout makes
            # a writer wait out a transient lock instead of crashing with
            # "database is locked". Mirrors SessionStore.
            self.conn.execute("PRAGMA journal_mode = WAL;")
            self.conn.execute("PRAGMA busy_timeout = 30000;")
            self.conn.execute("PRAGMA foreign_keys = ON;")
            self._vec_loaded = False
            if use_sqlite_vec:
                self._try_load_vec()
        except Exception:
            self.conn.close()
            raise

    def reopen(self) -> None:
        """Close and re-open the connection against the current ``db_path``.

        Used after an atomic rebuild ``os.replace``-swaps a freshly-built DB over
        ``db_path``: the old file handle points at the now-unlinked inode, so the
        caller's live store must re-attach to the swapped-in file to see the new
        content. Mirrors the connection setup in ``__init__``.
        """
        self.conn.close()
        self.conn = sqlite3.connect(self.db_path)
        try:
            self.conn.row_factory = sqlite3.Row
            # Re-apply the concurrency pragmas (C3): journal_mode is persisted on
            # the file, but busy_timeout is per-connection, so a fresh connection
            # after the atomic-rebuild swap needs it set again.
            self.conn.execute("PRAGMA journal_mode = WAL;")
            self.conn.execute("PRAGMA busy_timeout = 30000;")
            self.conn.execute("PRAGMA foreign_keys = ON;")
            self._vec_loaded = False
            if self.use_sqlite_vec:
                self._try_load_vec()
        except Exception:
            self.conn.close()
            raise

    def _try_load_vec(self) -> None:
        try:
            import sqlite_vec

            self.conn.enable_load_extension(True)
            sqlite_vec.load(self.conn)
            self.conn.enable_load_extension(False)
            self._vec_loaded = True
        except Exception:
            # sqlite-vec extension unavailable; cosine fallback will be used instead
            self._vec_loaded = False

    def init_schema(self) -> None:
        """Create the files, chunks, and metadata tables if they do not yet exist.

        The version + identity stamp is written ONLY when the schema is being
        created fresh (no ``files`` table yet). Opening an existing — possibly
        older — DB must NOT re-stamp ``user_version``: doing so would overwrite a
        stale-but-truthful version with the current one, so
        :meth:`needs_schema_migration` would report a confusing "stored=N,
        current=N" while the column backstop still (correctly) forces a rebuild,
        and the file's recorded version would be silently wrong. A genuine upgrade
        happens via :meth:`rebuild` (a fresh temp DB), never in place here.

        For the same reason the embedding identity is stamped only on a fresh DB:
        overwriting an existing identity would erase the evidence
        :meth:`check_model_identity` relies on to detect a model swap, turning a
        loud mismatch into a silent-wrong index. The write path
        (:meth:`replace_file_and_chunks`) keeps the identity *current* once a
        (re)build actually produces new vectors.
        """
        is_fresh = "files" not in self.list_tables()
        self.conn.executescript(SCHEMA_FILES + SCHEMA_CHUNKS + SCHEMA_METADATA)
        if is_fresh:
            self.conn.execute(f"PRAGMA user_version = {CURATED_SCHEMA_VERSION}")
            self._set_metadata_no_commit("schema_version", str(CURATED_SCHEMA_VERSION))
            if self.embedding_model is not None and self.get_metadata("embedding_model") is None:
                self.stamp_identity_no_commit(self.embedding_model, self.dim)
        self.conn.commit()

    def list_tables(self) -> list[str]:
        """Return the names of all tables in the database."""
        rows = self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        return [r["name"] for r in rows]

    def needs_schema_migration(self) -> bool:
        """True when the on-disk curated index is older than the current schema.

        Primary signal is ``PRAGMA user_version`` vs :data:`CURATED_SCHEMA_VERSION`
        — a stamped version below the current one means the schema (now including
        the ``metadata`` identity table) is outdated and the index must be rebuilt.

        The original column-presence check is kept as a backstop for indexes built
        *before* versioning existed (those carry ``user_version == 0`` but may still
        have the timestamp columns). Critically, a version-2 index — timestamp
        columns present but NO ``metadata`` table, so unknown embedding provenance —
        is now caught by the version comparison and treated as needing a rebuild,
        rather than being silently trusted.
        """
        user_version = int(self.conn.execute("PRAGMA user_version").fetchone()[0])
        if user_version != 0 and user_version < CURATED_SCHEMA_VERSION:
            return True
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(chunks)")}
        if "created" not in cols:
            return True
        # The `files.name` column carries the display name INDEX.md derives (C5).
        # A pre-versioning index without it cannot produce a faithful INDEX.md, so
        # it needs a rebuild even when the metadata table happens to be present.
        file_cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(files)")}
        if "name" not in file_cols:
            return True
        # Pre-versioning index (user_version == 0) that nonetheless has the
        # timestamp columns: the absence of the metadata table means unknown
        # provenance, so it still needs a rebuild rather than a blind trust.
        return "metadata" not in self.list_tables()

    def reset_schema(self) -> None:
        """Drop and recreate the curated tables so a rebuild picks up schema changes."""
        self.conn.executescript(
            "DROP TABLE IF EXISTS chunks; DROP TABLE IF EXISTS files; "
            "DROP TABLE IF EXISTS metadata;"
        )
        self.conn.commit()
        self.init_schema()

    # ------------------------- metadata / index identity -------------------------

    def _set_metadata_no_commit(self, key: str, value: str) -> None:
        """Upsert a single ``metadata`` row WITHOUT committing.

        Deferred commit so identity stamping can ride along with the surrounding
        write transaction (e.g. ``replace_file_and_chunks``) instead of forcing an
        extra fsync per file.
        """
        self.conn.execute(
            "INSERT INTO metadata(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def get_metadata(self, key: str) -> str | None:
        """Return the ``metadata`` value for ``key``, or None if unset/absent.

        Returns None (rather than raising) when the ``metadata`` table itself is
        missing, so callers can treat a pre-C4 index as "no recorded identity".
        """
        if "metadata" not in self.list_tables():
            return None
        row = self.conn.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def stamp_identity_no_commit(self, embedding_model: str, dim: int) -> None:
        """Record the embedding model + dimension that produced this index (no commit).

        Called on init and kept current on every write so the recorded identity
        always reflects the model actually used. Bundled into the caller's
        transaction; the caller commits.
        """
        self._set_metadata_no_commit("embedding_model", embedding_model)
        self._set_metadata_no_commit("embedding_dim", str(dim))

    def check_model_identity(self, embedding_model: str, dim: int) -> None:
        """Verify the configured model/dim matches what built the index, or fail loudly.

        Raises :class:`IndexModelMismatchError` when the index already records a
        DIFFERENT embedding model or dimension. An index with NO recorded
        identity (no ``metadata`` rows) is NOT decided here — that "unknown
        provenance" case is handled as a needs-rebuild by
        :meth:`needs_schema_migration` (the ``metadata`` table is absent on any
        pre-C4 index). An empty index that simply hasn't been stamped yet (e.g.
        a freshly created DB about to be populated by the same model) passes.
        """
        stored_model = self.get_metadata("embedding_model")
        stored_dim_str = self.get_metadata("embedding_dim")
        if stored_model is None and stored_dim_str is None:
            return
        stored_dim = int(stored_dim_str) if stored_dim_str is not None else None
        if stored_model != embedding_model or stored_dim != dim:
            raise IndexModelMismatchError(
                self.db_path, stored_model, stored_dim, embedding_model, dim
            )

    def distinct_file_timestamps(self) -> list[dict[str, Any]]:
        """One row per indexed file: ``{file_path, updated, created}``."""
        rows = self.conn.execute(
            "SELECT file_path, MAX(updated) AS updated, MAX(created) AS created "
            "FROM chunks GROUP BY file_path"
        ).fetchall()
        return [
            dict(file_path=r["file_path"], updated=r["updated"], created=r["created"]) for r in rows
        ]

    def _upsert_file_no_commit(
        self, path: str, mtime: int, content_hash: str, *, name: str = ""
    ) -> None:
        """Insert/update a file row WITHOUT committing.

        The commit is deferred so the caller can bundle the files-row write with
        the chunk writes into one transaction (see ``replace_file_and_chunks``).
        The invariant that depends on this: ``content_hash`` must not be visible
        to a future incremental run until the matching chunks have also landed.

        ``name`` is the file's frontmatter display name, recorded so INDEX.md can
        be derived from the store rather than a second filesystem walk (C5).
        """
        self.conn.execute(
            "INSERT INTO files(path, mtime, content_hash, name) VALUES(?,?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET mtime=excluded.mtime, "
            "content_hash=excluded.content_hash, name=excluded.name, "
            "indexed_at=strftime('%s','now')",
            (path, mtime, content_hash, name),
        )

    def upsert_file(self, path: str, mtime: int, content_hash: str, *, name: str = "") -> None:
        """Insert or update a file's mtime, content hash, name, and indexed timestamp.

        Standalone committing wrapper. The indexing path does NOT use this — it
        goes through ``replace_file_and_chunks`` so the hash and chunks commit
        together. Kept for callers (and tests) that write a files row directly.
        """
        self._upsert_file_no_commit(path, mtime, content_hash, name=name)
        self.conn.commit()

    def get_file(self, path: str) -> dict[str, Any] | None:
        """Return the file row for ``path``, or None if it is not indexed."""
        row = self.conn.execute(
            "SELECT path, mtime, content_hash, name, indexed_at FROM files WHERE path=?",
            (path,),
        ).fetchone()
        return dict(row) if row else None

    def all_files(self) -> list[dict[str, Any]]:
        """Return every indexed file's path, mtime, content hash, and name."""
        rows = self.conn.execute("SELECT path, mtime, content_hash, name FROM files").fetchall()
        return [dict(r) for r in rows]

    def all_files_for_index_md(self) -> list[dict[str, Any]]:
        """Return one record per indexed file for deriving INDEX.md (C5).

        Each record carries ``path``, ``name`` (frontmatter display name), and the
        file's ``tags`` / ``aliases`` (decoded from any one of its chunks — they
        are identical across a file's chunks). Deriving INDEX.md from this instead
        of re-walking and re-``parse_file``-ing the disk closes the TOCTOU window
        where INDEX.md could disagree with the index it is supposed to describe:
        a file the indexer skipped (bad frontmatter) is absent from the store, so
        it is absent from INDEX.md too — by construction, not by a second guess.

        A file whose chunks somehow carry no tag/alias rows still appears (with
        empty lists), so it is never silently dropped from the listing.
        """
        rows = self.conn.execute(
            "SELECT f.path AS path, f.name AS name, "
            "       (SELECT c.tags_json FROM chunks c WHERE c.file_path = f.path LIMIT 1) "
            "         AS tags_json, "
            "       (SELECT c.aliases_json FROM chunks c WHERE c.file_path = f.path LIMIT 1) "
            "         AS aliases_json "
            "FROM files f "
            "ORDER BY f.path"
        ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            out.append(
                dict(
                    path=r["path"],
                    name=r["name"],
                    tags=json.loads(r["tags_json"]) if r["tags_json"] else [],
                    aliases=json.loads(r["aliases_json"]) if r["aliases_json"] else [],
                )
            )
        return out

    def delete_file(self, path: str) -> None:
        """Delete a file row; its chunks are removed via ON DELETE CASCADE."""
        # ON DELETE CASCADE (enforced via PRAGMA foreign_keys=ON) removes associated chunks
        self.conn.execute("DELETE FROM files WHERE path=?", (path,))
        self.conn.commit()

    def _replace_chunks_no_commit(
        self,
        file_path: str,
        chunks: list[dict[str, Any]],
        *,
        created: str | None = None,
        updated: str | None = None,
        valid_from: str | None = None,
        invalidated_at: str | None = None,
    ) -> None:
        """Delete-and-reinsert a file's chunks WITHOUT committing.

        The commit is deferred so the caller can bundle this with the files-row
        upsert into one transaction (see ``replace_file_and_chunks``). If the
        inserts fail partway, the enclosing ``with conn:`` rolls the whole thing
        back — the old chunks AND the old hash survive intact.
        """
        cur = self.conn.cursor()
        cur.execute("DELETE FROM chunks WHERE file_path=?", (file_path,))
        for c in chunks:
            emb: np.ndarray = c["embedding"]
            if emb.dtype != np.float32:
                emb = emb.astype(np.float32)
            cur.execute(
                "INSERT INTO chunks(file_path, heading, line_start, line_end, "
                "text, tags_json, aliases_json, created, updated, valid_from, "
                "invalidated_at, embedding) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    file_path,
                    c.get("heading"),
                    int(c["line_start"]),
                    int(c["line_end"]),
                    c["text"],
                    json.dumps(c.get("tags", [])),
                    json.dumps(c.get("aliases", [])),
                    created,
                    updated,
                    valid_from,
                    invalidated_at,
                    emb.tobytes(),
                ),
            )

    def replace_chunks_for_file(
        self,
        file_path: str,
        chunks: list[dict[str, Any]],
        *,
        created: str | None = None,
        updated: str | None = None,
        valid_from: str | None = None,
        invalidated_at: str | None = None,
    ) -> None:
        """Replace a file's chunks; the four file-level timestamps go on each row.

        Standalone committing wrapper. The indexing path does NOT use this — it
        goes through ``replace_file_and_chunks`` so chunks commit together with
        the files-row hash. Kept for callers (and tests) that already hold a
        files row and only want to (re)write its chunks.
        """
        self._replace_chunks_no_commit(
            file_path,
            chunks,
            created=created,
            updated=updated,
            valid_from=valid_from,
            invalidated_at=invalidated_at,
        )
        self.conn.commit()

    def replace_file_and_chunks(
        self,
        path: str,
        mtime: int,
        content_hash: str,
        chunks: list[dict[str, Any]],
        *,
        name: str = "",
        created: str | None = None,
        updated: str | None = None,
        valid_from: str | None = None,
        invalidated_at: str | None = None,
    ) -> None:
        """Atomically (re)write a file's identity row AND its chunks in ONE txn.

        This is the single write path the indexer uses per file. The files-row
        UPSERT, the chunk DELETE, and the chunk INSERTs all run inside one
        ``with self.conn:`` block, so they commit together or not at all.

        Why this matters (the #18 root cause): ``content_hash`` is the marker an
        incremental ``update()`` uses to decide "unchanged → skip". If the hash
        were committed before the chunks landed, a crash/kill in between would
        leave a files row advertising the NEW hash but with STALE or MISSING
        chunks — and the next run would skip it forever (silent corruption).
        Bundling them means a mid-write crash rolls back to the OLD hash + OLD
        chunks, which the next run sees as "changed" and cleanly re-indexes.

        Mirrors ``sessions/ingest.py::ingest_file``, which already records the
        file-seen marker only after the messages+embeddings commit.
        """
        with self.conn:
            self._upsert_file_no_commit(path, mtime, content_hash, name=name)
            self._replace_chunks_no_commit(
                path,
                chunks,
                created=created,
                updated=updated,
                valid_from=valid_from,
                invalidated_at=invalidated_at,
            )
            # Keep the recorded identity current with every write, so it always
            # names the model that actually produced the on-disk vectors. Stamped
            # inside the same transaction as the chunks it describes, so the
            # identity can never drift ahead of (or behind) the data.
            if self.embedding_model is not None:
                self.stamp_identity_no_commit(self.embedding_model, self.dim)

    def all_chunks_for_file(self, file_path: str) -> list[dict[str, Any]]:
        """Return all chunks for a file, with tags and aliases decoded from JSON."""
        rows = self.conn.execute(
            "SELECT id, heading, line_start, line_end, text, tags_json, aliases_json "
            "FROM chunks WHERE file_path=?",
            (file_path,),
        ).fetchall()
        return [
            dict(
                id=r["id"],
                heading=r["heading"],
                line_start=r["line_start"],
                line_end=r["line_end"],
                text=r["text"],
                tags=json.loads(r["tags_json"]),
                aliases=json.loads(r["aliases_json"]),
            )
            for r in rows
        ]

    def search(self, query_vec: np.ndarray, top_k: int = 5) -> list[dict[str, Any]]:
        """Return the ``top_k`` chunks most similar to ``query_vec`` by cosine similarity."""
        if query_vec.dtype != np.float32:
            query_vec = query_vec.astype(np.float32)
        rows = self.conn.execute(
            "SELECT id, file_path, heading, line_start, line_end, text, "
            "tags_json, aliases_json, created, updated, valid_from, "
            "invalidated_at, embedding FROM chunks"
        ).fetchall()
        if not rows:
            return []
        vecs = np.vstack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
        # Cosine similarity: dot(v, q) / (||v|| * ||q||); small epsilon avoids division by zero
        norms = np.linalg.norm(vecs, axis=1) + 1e-12
        qnorm = float(np.linalg.norm(query_vec)) + 1e-12
        scores = (vecs @ query_vec) / (norms * qnorm)
        idx = np.argsort(-scores)[:top_k]
        out: list[dict[str, Any]] = []
        for i in idx:
            r = rows[i]
            out.append(
                dict(
                    id=r["id"],
                    file_path=r["file_path"],
                    heading=r["heading"],
                    line_start=r["line_start"],
                    line_end=r["line_end"],
                    text=r["text"],
                    tags=json.loads(r["tags_json"]),
                    aliases=json.loads(r["aliases_json"]),
                    created=r["created"],
                    updated=r["updated"],
                    valid_from=r["valid_from"],
                    invalidated_at=r["invalidated_at"],
                    cosine_score=float(scores[i]),
                    score=float(scores[i]),  # back-compat alias (= cosine; pre-ranking readers)
                )
            )
        return out

    def __enter__(self) -> IndexStore:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying SQLite connection. Always call when done."""
        self.conn.close()
