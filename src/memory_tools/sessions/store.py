"""SQLite-backed store for Claude Code session transcripts.

Two indexes side by side over the same ``messages`` table:
  - FTS5 (``messages_fts``) — keyword/exact-string queries
  - sqlite-vec virtual table (``messages_vec``) — semantic queries

Schema is intentionally separate from ``IndexStore`` (which holds curated
memory) so the two lifecycles do not interfere — rebuilding one never
risks the other, and the DBs are sized appropriately for their corpora.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import List, Optional


SCHEMA_MESSAGES = """
CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL,
    message_uuid    TEXT NOT NULL,
    parent_uuid     TEXT,
    role            TEXT NOT NULL,
    content         TEXT NOT NULL,
    cwd             TEXT,
    timestamp_iso   TEXT NOT NULL,
    timestamp_unix  INTEGER NOT NULL,
    jsonl_path      TEXT NOT NULL,
    line_number     INTEGER NOT NULL,
    UNIQUE(session_id, message_uuid)
);
CREATE INDEX IF NOT EXISTS idx_messages_session   ON messages(session_id);
CREATE INDEX IF NOT EXISTS idx_messages_timestamp ON messages(timestamp_unix DESC);
CREATE INDEX IF NOT EXISTS idx_messages_cwd       ON messages(cwd);
"""

SCHEMA_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    role UNINDEXED,
    session_id UNINDEXED,
    content='messages',
    content_rowid='id',
    tokenize='porter unicode61'
);
"""

SCHEMA_FTS_TRIGGERS = """
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
  INSERT INTO messages_fts(rowid, content, role, session_id)
  VALUES (new.id, new.content, new.role, new.session_id);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, content, role, session_id)
  VALUES ('delete', old.id, old.content, old.role, old.session_id);
END;
CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
  INSERT INTO messages_fts(messages_fts, rowid, content, role, session_id)
  VALUES ('delete', old.id, old.content, old.role, old.session_id);
  INSERT INTO messages_fts(rowid, content, role, session_id)
  VALUES (new.id, new.content, new.role, new.session_id);
END;
"""

# files_seen — per-JSONL mtime/size tracking so incremental ingest skips
# unchanged files without per-row DB round-trips. Without this, a SessionEnd
# hook on a deep-history machine becomes measurably slow.
SCHEMA_FILES_SEEN = """
CREATE TABLE IF NOT EXISTS files_seen (
    jsonl_path     TEXT PRIMARY KEY,
    mtime_unix     INTEGER NOT NULL,
    size_bytes     INTEGER NOT NULL,
    last_seen_unix INTEGER NOT NULL DEFAULT (strftime('%s', 'now'))
);
"""


class SessionStore:
    """SQLite store for transcript messages, with FTS5 + vec0 indexes."""

    def __init__(self, db_path: Path, dim: int = 384, use_sqlite_vec: bool = True) -> None:
        self.db_path = Path(db_path)
        self.dim = dim
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path)
        try:
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA journal_mode = WAL;")
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
            # sqlite-vec extension unavailable; vector search will be disabled
            self._vec_loaded = False

    def init_schema(self) -> None:
        self.conn.executescript(
            SCHEMA_MESSAGES + SCHEMA_FTS + SCHEMA_FTS_TRIGGERS + SCHEMA_FILES_SEEN
        )
        if self._vec_loaded:
            # vec0 virtual table — separate so non-vec environments still work.
            # Name is reserved exclusively for the vec0 virtual table so that
            # turning sqlite-vec on/off across runs can never collide with the
            # numpy fallback table below.
            self.conn.execute(
                f"CREATE VIRTUAL TABLE IF NOT EXISTS messages_vec USING vec0("
                f"embedding float[{self.dim}])"
            )
        else:
            # Non-vec environments: create a regular fallback table under a
            # DIFFERENT name. If sqlite-vec is later installed, this lets the
            # vec0 virtual table be created without shadowing.
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS messages_vec_numpy ("
                "rowid INTEGER PRIMARY KEY, embedding BLOB NOT NULL)"
            )
        self.conn.commit()

    def list_tables(self) -> List[str]:
        """Return all tables (including virtual tables) by name."""
        rows = self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        return [r["name"] for r in rows]

    def insert_message(self, msg: dict) -> Optional[int]:
        """Insert a single message. Returns rowid, or None if duplicate.

        Dedupe is via the UNIQUE(session_id, message_uuid) constraint —
        a clash is the normal incremental-reindex case and not an error.
        """
        cur = self.conn.cursor()
        try:
            cur.execute(
                "INSERT INTO messages(session_id, message_uuid, parent_uuid, role, "
                "content, cwd, timestamp_iso, timestamp_unix, jsonl_path, line_number) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    msg["session_id"],
                    msg["message_uuid"],
                    msg.get("parent_uuid"),
                    msg["role"],
                    msg["content"],
                    msg.get("cwd"),
                    msg["timestamp_iso"],
                    int(msg["timestamp_unix"]),
                    msg["jsonl_path"],
                    int(msg["line_number"]),
                ),
            )
        except sqlite3.IntegrityError:
            return None
        self.conn.commit()
        return cur.lastrowid

    def upsert_embedding(self, rowid: int, vec) -> None:
        """Attach an embedding to an existing message row.

        Routes to ``messages_vec`` (vec0 virtual table) when sqlite-vec is
        loaded, otherwise to ``messages_vec_numpy`` (regular fallback table).
        The two destinations have distinct names so an environment flip never
        causes silent shadowing.
        """
        import numpy as np

        if vec.dtype != np.float32:
            vec = vec.astype(np.float32)
        if self._vec_loaded:
            self.conn.execute("DELETE FROM messages_vec WHERE rowid = ?", (rowid,))
            self.conn.execute(
                "INSERT INTO messages_vec(rowid, embedding) VALUES(?, ?)",
                (rowid, vec.tobytes()),
            )
        else:
            self.conn.execute(
                "INSERT OR REPLACE INTO messages_vec_numpy(rowid, embedding) VALUES(?, ?)",
                (rowid, vec.tobytes()),
            )
        self.conn.commit()

    def search_fts(self, query: str, top_k: int = 5) -> List[dict]:
        """FTS5 keyword search.

        SQLite FTS5 ``bm25()`` returns **negative** values, where a stronger
        match returns a more-negative score. We negate to get a positive
        higher-is-better score suitable for cross-modal merging with vector
        similarities. Inversion formulas like ``1/(1+bm25)`` are unsafe — they
        produce negative outputs for strong matches and divide-by-zero at
        ``bm25 == -1.0``.
        """
        rows = self.conn.execute(
            "SELECT m.id, m.session_id, m.message_uuid, m.role, m.content, "
            "       m.cwd, m.timestamp_iso, m.timestamp_unix, m.jsonl_path, m.line_number, "
            "       bm25(messages_fts) AS bm25_score "
            "FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid "
            "WHERE messages_fts MATCH ? "
            "ORDER BY bm25_score LIMIT ?",
            (query, top_k),
        ).fetchall()
        out: List[dict] = []
        for r in rows:
            d = dict(r)
            # Negate: more-negative bm25 (stronger match) becomes higher score.
            d["score"] = -float(d.pop("bm25_score"))
            d["source_kind"] = "fts"
            out.append(d)
        return out

    def search_vec(self, query_vec, top_k: int = 5) -> List[dict]:
        """Vector search via sqlite-vec when available; numpy cosine fallback otherwise."""
        import numpy as np

        if query_vec.dtype != np.float32:
            query_vec = query_vec.astype(np.float32)
        if self._vec_loaded:
            rows = self.conn.execute(
                "SELECT m.id, m.session_id, m.message_uuid, m.role, m.content, "
                "       m.cwd, m.timestamp_iso, m.timestamp_unix, m.jsonl_path, m.line_number, "
                "       v.distance AS distance "
                "FROM messages_vec v JOIN messages m ON m.id = v.rowid "
                "WHERE v.embedding MATCH ? AND k = ? "
                "ORDER BY v.distance",
                (query_vec.tobytes(), top_k),
            ).fetchall()
            out: List[dict] = []
            for r in rows:
                d = dict(r)
                # vec0 distance is cosine distance in [0, 2]; convert to similarity in [-1, 1]
                d["score"] = 1.0 - float(d.pop("distance"))
                d["source_kind"] = "vec"
                out.append(d)
            return out
        # Numpy cosine fallback over the renamed fallback table.
        rows = self.conn.execute(
            "SELECT m.id, m.session_id, m.message_uuid, m.role, m.content, "
            "       m.cwd, m.timestamp_iso, m.timestamp_unix, m.jsonl_path, m.line_number, "
            "       v.embedding "
            "FROM messages_vec_numpy v JOIN messages m ON m.id = v.rowid"
        ).fetchall()
        if not rows:
            return []
        vecs = np.vstack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
        norms = np.linalg.norm(vecs, axis=1) + 1e-12
        qnorm = float(np.linalg.norm(query_vec)) + 1e-12
        scores = (vecs @ query_vec) / (norms * qnorm)
        idx = np.argsort(-scores)[:top_k]
        out: List[dict] = []
        for i in idx:
            r = rows[i]
            d = {k: r[k] for k in r.keys() if k != "embedding"}
            d["score"] = float(scores[i])
            d["source_kind"] = "vec"
            out.append(d)
        return out

    # ------- files_seen: per-JSONL mtime tracking for incremental skip -------

    def record_file_seen(self, jsonl_path: str, mtime_unix: int, size_bytes: int) -> None:
        """Record that ``jsonl_path`` was ingested at the given mtime/size."""
        self.conn.execute(
            "INSERT INTO files_seen(jsonl_path, mtime_unix, size_bytes) VALUES(?,?,?) "
            "ON CONFLICT(jsonl_path) DO UPDATE SET mtime_unix=excluded.mtime_unix, "
            "size_bytes=excluded.size_bytes, last_seen_unix=strftime('%s','now')",
            (jsonl_path, int(mtime_unix), int(size_bytes)),
        )
        self.conn.commit()

    def should_skip_file(self, jsonl_path: str, mtime_unix: int, size_bytes: int) -> bool:
        """Return True when this file's mtime+size match what was last ingested.

        Mtime alone is unreliable on copies and rsync; combining with size catches
        most real changes without reading content. Hash-checking is overkill for
        an append-only JSONL stream.
        """
        row = self.conn.execute(
            "SELECT mtime_unix, size_bytes FROM files_seen WHERE jsonl_path = ?",
            (jsonl_path,),
        ).fetchone()
        if row is None:
            return False
        return int(row["mtime_unix"]) == int(mtime_unix) and int(row["size_bytes"]) == int(size_bytes)

    def __enter__(self) -> "SessionStore":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def close(self) -> None:
        self.conn.close()
