"""SQLite-backed store for Claude Code session transcripts.

Two indexes side by side over the same ``messages`` table:
  - FTS5 (``messages_fts``) — keyword/exact-string queries
  - sqlite-vec virtual table (``messages_vec``) — semantic queries

Schema is intentionally separate from ``IndexStore`` (which holds curated
memory) so the two lifecycles do not interfere — rebuilding one never
risks the other, and the DBs are sized appropriately for their corpora.
"""

from __future__ import annotations

import re
import sqlite3
import warnings
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import numpy as np


# Matches a token that carries at least one letter or digit (Unicode-aware).
# ``[^\W_]`` is "word char but not underscore" — i.e. alphanumeric — so a lone
# operator token like ``-`` or ``_`` has no match and gets dropped.
_FTS_HAS_ALNUM = re.compile(r"[^\W_]", re.UNICODE)


class SessionStoreDimMismatch(RuntimeError):
    """Raised when the on-disk vector index width can't hold the model's vectors.

    Carries the conflicting dimensions so callers can render an actionable
    message instead of letting a raw sqlite-vec width error surface on the first
    read or write.
    """

    def __init__(self, db_path: Path, existing_dim: int, wanted_dim: int) -> None:
        self.db_path = db_path
        self.existing_dim = existing_dim
        self.wanted_dim = wanted_dim
        super().__init__(
            f"sessions index at {db_path} was built with {existing_dim}-dim embeddings, "
            f"but the configured model produces {wanted_dim}-dim vectors. Restore the "
            f"previous embedding_model, or rebuild the transcript index:\n"
            f"  rm '{db_path}' && rekol session-index --full"
        )


def build_fts_match(query: str) -> str | None:
    """Turn a raw user query into a safe FTS5 ``MATCH`` expression.

    FTS5 ``MATCH`` has its own query grammar: a leading ``-`` means NOT, ``:``
    is a column filter, ``*`` is a prefix operator, and so on. Passing user text
    straight in lets an ordinary query like ``slack-daemon ANTHROPIC_API_KEY``
    raise ``OperationalError: no such column: ...`` (or silently mis-parse).

    We defuse the grammar by emitting each whitespace-separated token as a
    double-quoted FTS5 phrase (embedded quotes doubled), AND-combined with
    spaces — preserving the existing implicit-AND keyword semantics while
    stripping all operator meaning.

    Tokens with no alphanumeric content (e.g. a lone ``-``) are dropped, because
    a quoted phrase that tokenises to nothing is itself an FTS5 syntax error.
    Returns ``None`` when nothing searchable remains, so callers can skip the
    query rather than issue an empty ``MATCH`` (which FTS5 rejects).
    """
    phrases: list[str] = []
    for token in query.split():
        if not _FTS_HAS_ALNUM.search(token):
            continue
        escaped = token.replace('"', '""')
        phrases.append(f'"{escaped}"')
    if not phrases:
        return None
    return " ".join(phrases)


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
        except ImportError:
            # sqlite-vec genuinely absent; numpy fallback path is silent-by-design
            self._vec_loaded = False
            return
        try:
            self.conn.enable_load_extension(True)
            sqlite_vec.load(self.conn)
            self.conn.enable_load_extension(False)
            self._vec_loaded = True
        except Exception as exc:
            # sqlite-vec installed but extension loading failed (e.g., Python
            # built without SQLITE_ENABLE_LOAD_EXTENSION). Warn so a future run
            # that succeeds doesn't silently create a split-brain index.
            warnings.warn(
                f"sqlite-vec is installed but could not be loaded ({exc!r}); "
                "falling back to numpy cosine search. If this becomes consistent, "
                "either remove sqlite-vec from the env or fix the loader.",
                stacklevel=2,
            )
            self._vec_loaded = False

    def init_schema(self) -> None:
        """Create the messages, FTS, and files-seen tables, plus the vec table if loaded."""
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

    def list_tables(self) -> list[str]:
        """Return all tables (including virtual tables) by name."""
        rows = self.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        return [r["name"] for r in rows]

    def insert_message(self, msg: dict) -> int | None:
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

    def insert_message_no_commit(self, msg: dict) -> int | None:
        """Same as insert_message but caller controls the transaction.

        Used by ``ingest_file`` which wraps an entire file in one BEGIN/COMMIT
        for performance. Duplicate uuid still returns None (no error).
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
        return cur.lastrowid

    def upsert_embedding(self, rowid: int, vec: np.ndarray) -> None:
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

    def upsert_embedding_no_commit(self, rowid: int, vec: np.ndarray) -> None:
        """Same as ``upsert_embedding`` but the caller controls the transaction.

        Used by ``ingest_file`` so a whole file's message embeddings are written
        in the same single BEGIN/COMMIT as the message inserts — avoiding the
        per-row fsync that would otherwise dominate a deep-history backfill, and
        keeping message rows and their embeddings atomic (a crash leaves the
        file un-recorded in ``files_seen`` and the next run reingests cleanly).
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

    def _vec_table(self) -> str:
        """Name of the embedding table in use (vec0 vs numpy fallback).

        A single internal constant, never user input, so it is safe to splice
        into the SQL below.
        """
        return "messages_vec" if self._vec_loaded else "messages_vec_numpy"

    def count_messages(self) -> int:
        """Total message rows. Used as a cheap guard for the self-heal pass."""
        return int(self.conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"])

    def count_embeddings(self) -> int:
        """Total embedding rows.

        Embeddings are a subset of message rowids (never orphaned: messages are
        never deleted here), so ``count_embeddings() == count_messages()`` iff
        every message is embedded — letting the repair pass skip the (more
        expensive) anti-join in the steady state.
        """
        return int(
            self.conn.execute(f"SELECT COUNT(*) AS n FROM {self._vec_table()}").fetchone()["n"]
        )

    def fetch_unembedded(self, limit: int) -> list[tuple[int, str]]:
        """Return up to ``limit`` (rowid, content) pairs for unembedded messages.

        Drives the self-heal pass that backfills embeddings for an index built
        FTS-only (or with ``--no-embed``): the mtime+size skip gate would never
        revisit those files, so this is the only path to full semantic coverage
        short of a destructive rebuild.
        """
        rows = self.conn.execute(
            f"SELECT m.id AS id, m.content AS content FROM messages m "
            f"WHERE NOT EXISTS (SELECT 1 FROM {self._vec_table()} v WHERE v.rowid = m.id) "
            f"ORDER BY m.id LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [(int(r["id"]), r["content"]) for r in rows]

    def existing_vec_dim(self) -> int | None:
        """Embedding dimension already stored on disk, or None if undetermined.

        ``init_schema`` creates ``messages_vec`` with ``CREATE ... IF NOT
        EXISTS``, so re-opening a DB that was built at a different dimension is a
        silent no-op — the table keeps its original width and the mismatch only
        surfaces later as a cryptic sqlite-vec error on the first insert. This
        lets callers detect the conflict up front and tell the user how to fix
        it.

        vec0 path: parse the declared ``float[N]`` from the table's schema.
        numpy fallback: infer N from the byte length of any stored row (float32,
        4 bytes each); returns None when the table is empty (no width yet).
        """
        if self._vec_loaded:
            row = self.conn.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'messages_vec'"
            ).fetchone()
            if row is None or not row["sql"]:
                return None
            match = re.search(r"float\s*\[\s*(\d+)\s*\]", row["sql"])
            return int(match.group(1)) if match else None
        row = self.conn.execute(
            "SELECT LENGTH(embedding) AS nbytes FROM messages_vec_numpy LIMIT 1"
        ).fetchone()
        if row is None or row["nbytes"] is None:
            return None
        return int(row["nbytes"]) // 4  # float32 → 4 bytes per dimension

    def reconcile_embedding_dim(self, wanted_dim: int) -> None:
        """Make the vector index able to hold ``wanted_dim`` vectors, or fail loudly.

        Owns the "requested dim must match on-disk dim" invariant for every
        caller that reads or writes vectors (ingest and search alike), instead
        of each command re-checking. Three cases:

        - matching, or no vectors stored yet → no-op.
        - a stale **empty** vec0 table at a different width (e.g. one created by
          a ``--no-embed`` run before any model wrote to it) → drop and recreate
          at ``wanted_dim``; nothing is lost.
        - a **populated** table at a different width → raise
          :class:`SessionStoreDimMismatch` so the caller can surface remediation
          rather than crashing on the first read/write.
        """
        existing = self.existing_vec_dim()
        if existing is None or existing == wanted_dim:
            self.dim = wanted_dim
            return
        if self.count_embeddings() == 0:
            self._recreate_empty_vec(wanted_dim)
            return
        raise SessionStoreDimMismatch(self.db_path, existing, wanted_dim)

    def _recreate_empty_vec(self, dim: int) -> None:
        """Drop and recreate the (empty) vec0 table at ``dim``.

        Only the vec0 path declares a width up front, so only it can be "empty
        but wrong width"; the numpy fallback has no declared width and reports
        ``existing_vec_dim() is None`` when empty, so it never reaches here.
        """
        self.dim = dim
        if self._vec_loaded:
            self.conn.execute("DROP TABLE IF EXISTS messages_vec")
            self.conn.execute(
                f"CREATE VIRTUAL TABLE messages_vec USING vec0(embedding float[{dim}])"
            )
            self.conn.commit()

    def search_fts(self, query: str, top_k: int = 5) -> list[dict]:
        """FTS5 keyword search.

        SQLite FTS5 ``bm25()`` returns **negative** values, where a stronger
        match returns a more-negative score. We negate to get a positive
        higher-is-better score suitable for cross-modal merging with vector
        similarities. Inversion formulas like ``1/(1+bm25)`` are unsafe — they
        produce negative outputs for strong matches and divide-by-zero at
        ``bm25 == -1.0``.

        The raw query is sanitised into quoted FTS5 phrases first (see
        :func:`build_fts_match`); a query with no searchable tokens returns no
        hits rather than issuing an invalid empty ``MATCH``.
        """
        match_query = build_fts_match(query)
        if match_query is None:
            return []
        rows = self.conn.execute(
            "SELECT m.id, m.session_id, m.message_uuid, m.role, m.content, "
            "       m.cwd, m.timestamp_iso, m.timestamp_unix, m.jsonl_path, m.line_number, "
            "       bm25(messages_fts) AS bm25_score "
            "FROM messages_fts JOIN messages m ON m.id = messages_fts.rowid "
            "WHERE messages_fts MATCH ? "
            # ASC because raw bm25() is negative — most-negative is the strongest match.
            "ORDER BY bm25_score ASC LIMIT ?",
            (match_query, top_k),
        ).fetchall()
        out: list[dict] = []
        for r in rows:
            d = dict(r)
            # Negate: more-negative bm25 (stronger match) becomes higher score.
            d["score"] = -float(d.pop("bm25_score"))
            d["source_kind"] = "fts"
            out.append(d)
        return out

    def search_vec(self, query_vec: np.ndarray, top_k: int = 5) -> list[dict]:
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
            out: list[dict] = []
            for r in rows:
                d = dict(r)
                # vec0 distance is cosine distance in [0, 2]; convert to similarity in [-1, 1]
                d["score"] = 1.0 - float(d.pop("distance"))
                d["source_kind"] = "vec"
                out.append(d)
            return out
        # Numpy cosine fallback over the renamed fallback table.
        # NOTE: loads all embeddings into memory and computes cosine in numpy.
        # Acceptable up to ~50k messages; beyond that the per-query cost grows
        # linearly and a real ANN structure (sqlite-vec, faiss) is the right fix.
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
        out = []
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
        return int(row["mtime_unix"]) == int(mtime_unix) and int(row["size_bytes"]) == int(
            size_bytes
        )

    def __enter__(self) -> SessionStore:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying database connection."""
        self.conn.close()
