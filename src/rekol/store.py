"""SQLite-backed index store. Uses sqlite-vec when available; falls back to numpy cosine."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import numpy as np

CURATED_SCHEMA_VERSION = 2  # 1 = original schema; 2 = with curated timestamp columns


class CuratedSchemaOutdatedError(RuntimeError):
    """Curated index predates the timestamp columns; a rebuild is required."""


SCHEMA_FILES = """
CREATE TABLE IF NOT EXISTS files (
    path          TEXT PRIMARY KEY,
    mtime         INTEGER NOT NULL,
    content_hash  TEXT NOT NULL,
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


class IndexStore:
    """SQLite-backed vector index. Fallback path (numpy cosine) is always available."""

    def __init__(
        self,
        db_path: Path,
        dim: int = 384,
        use_sqlite_vec: bool = True,
    ) -> None:
        self.db_path = Path(db_path)
        self.dim = dim
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        try:
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA foreign_keys = ON;")
            self._vec_loaded = False
            if use_sqlite_vec:
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
        """Create the files and chunks tables if they do not yet exist."""
        self.conn.executescript(SCHEMA_FILES + SCHEMA_CHUNKS)
        self.conn.execute(f"PRAGMA user_version = {CURATED_SCHEMA_VERSION}")
        self.conn.commit()

    def list_tables(self) -> list[str]:
        """Return the names of all tables in the database."""
        rows = self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        return [r["name"] for r in rows]

    def needs_schema_migration(self) -> bool:
        """True when the curated index predates the timestamp columns.

        Detected by column presence (robust even for indexes built before
        versioning existed); ``user_version`` is also stamped for future use.
        """
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(chunks)")}
        return "created" not in cols

    def reset_schema(self) -> None:
        """Drop and recreate the curated tables so a rebuild picks up schema changes."""
        self.conn.executescript("DROP TABLE IF EXISTS chunks; DROP TABLE IF EXISTS files;")
        self.conn.commit()
        self.init_schema()

    def distinct_file_timestamps(self) -> list[dict[str, Any]]:
        """One row per indexed file: ``{file_path, updated, created}``."""
        rows = self.conn.execute(
            "SELECT DISTINCT file_path, updated, created FROM chunks"
        ).fetchall()
        return [
            dict(file_path=r["file_path"], updated=r["updated"], created=r["created"]) for r in rows
        ]

    def upsert_file(self, path: str, mtime: int, content_hash: str) -> None:
        """Insert or update a file's mtime, content hash, and indexed timestamp."""
        self.conn.execute(
            "INSERT INTO files(path, mtime, content_hash) VALUES(?,?,?) "
            "ON CONFLICT(path) DO UPDATE SET mtime=excluded.mtime, "
            "content_hash=excluded.content_hash, "
            "indexed_at=strftime('%s','now')",
            (path, mtime, content_hash),
        )
        self.conn.commit()

    def get_file(self, path: str) -> dict[str, Any] | None:
        """Return the file row for ``path``, or None if it is not indexed."""
        row = self.conn.execute(
            "SELECT path, mtime, content_hash, indexed_at FROM files WHERE path=?",
            (path,),
        ).fetchone()
        return dict(row) if row else None

    def all_files(self) -> list[dict[str, Any]]:
        """Return every indexed file's path, mtime, and content hash."""
        rows = self.conn.execute("SELECT path, mtime, content_hash FROM files").fetchall()
        return [dict(r) for r in rows]

    def delete_file(self, path: str) -> None:
        """Delete a file row; its chunks are removed via ON DELETE CASCADE."""
        # ON DELETE CASCADE (enforced via PRAGMA foreign_keys=ON) removes associated chunks
        self.conn.execute("DELETE FROM files WHERE path=?", (path,))
        self.conn.commit()

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
        """Replace a file's chunks; the four file-level timestamps go on each row."""
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
        self.conn.commit()

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
