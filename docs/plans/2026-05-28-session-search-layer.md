# Session Search Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add hybrid FTS5+vec0 search over `~/.claude/projects/*/*.jsonl` conversation transcripts to memory-tools, exposed through a unified `memory-search` CLI that fans out across both curated memory and session transcripts with layered presentation.

**Architecture:** Sibling SQLite database `$MEMORY_HOME/.index/sessions.db` (separate from existing `index.db` for lifecycle independence). New `sessions/` subpackage in `memory_tools` for store + ingest. New `claude-session-index` CLI for one-shot and incremental indexing. Refactor `cli_search.py` to delegate to a shared `search_combined` module that queries both stores and merges results into two tiers. SessionEnd hook extension reindexes incrementally on every Claude Code session exit. Phase 3 install script backfills existing transcripts at install time.

**Tech Stack:** Python 3.11+, Click 8 (CLI), sqlite3 stdlib + FTS5 + sqlite-vec, NumPy, sentence-transformers (existing BAAI/bge-small-en-v1.5 384-dim model), pytest.

---

## File Structure

**Phase 1 — Sessions store + ingest + CLI** (1 commit on `main`)

- Create: `memory-tools/src/memory_tools/sessions/__init__.py`
- Create: `memory-tools/src/memory_tools/sessions/store.py` — `SessionStore` class (FTS5 + vec0 schema + insert/search primitives)
- Create: `memory-tools/src/memory_tools/sessions/ingest.py` — JSONL parsing, dedupe, batch ingest
- Create: `memory-tools/src/memory_tools/cli_session_index.py` — `claude-session-index` CLI (full + incremental)
- Modify: `memory-tools/pyproject.toml` — register new entry point
- Modify: `memory-tools/src/memory_tools/config.py` — add `sessions_db_path` property + `claude_projects_dir` config key
- Create: `memory-tools/tests/test_sessions_store.py`
- Create: `memory-tools/tests/test_sessions_ingest.py`
- Create: `memory-tools/tests/test_cli_session_index.py`
- Create: `memory-tools/tests/fixtures/sample_session.jsonl` — small synthetic JSONL for ingest tests

**Phase 2 — Extend memory-search** (1 commit on `main`)

- Create: `memory-tools/src/memory_tools/search_combined.py` — fan-out + layered merging
- Modify: `memory-tools/src/memory_tools/cli_search.py` — add `--source`, `--promote-candidates`, layered text output, change JSON shape from flat array → `{memory, sessions, is_promotion_candidate}` object
- Modify: `memory-tools/tests/test_cli.py` — extend with new flags
- Create: `memory-tools/tests/test_search_combined.py`
- **Modify: `memory-tools/skill/memory/skill.md` — BLOCKER, must land in same commit as cli_search.py change.** The skill instructs Claude to consume `--json` as a flat array; the new shape breaks that. Skill update + CLI change are atomic so no session ever pulls one without the other.

**Phase 3 — Hook + phase 3 backfill** (1 commit on `main`)

- Modify: `memory-tools/hooks/sessionend-snippet.json` — add second handler invoking `claude-session-index --incremental`
- Modify: `mac_setup/scripts/phase3_memory.sh` — initial session-search backfill step
- Create: `memory-tools/tests/test_install.bats` extension (one test asserting the hook snippet contains both handlers)

---

## Phase 1: Sessions store + ingest + CLI

### Task 1: SessionStore schema (FTS5 + vec0)

**Files:**
- Create: `memory-tools/src/memory_tools/sessions/__init__.py`
- Create: `memory-tools/src/memory_tools/sessions/store.py`
- Create: `memory-tools/tests/test_sessions_store.py`

- [ ] **Step 1.1: Write the failing schema test**

Create `memory-tools/tests/test_sessions_store.py`:

```python
"""Tests for SessionStore — schema init, insert, FTS5 + vec search, dedupe."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from memory_tools.sessions.store import SessionStore


def test_init_schema_creates_expected_tables(tmp_path: Path) -> None:
    store = SessionStore(db_path=tmp_path / "sessions.db", dim=384)
    store.init_schema()
    tables = store.list_tables()
    assert "messages" in tables
    assert "messages_fts" in tables
    # When sqlite-vec is available, the virtual table is `messages_vec`.
    # When it is not, the numpy fallback is `messages_vec_numpy`.
    assert ("messages_vec" in tables) or ("messages_vec_numpy" in tables)
    # files_seen tracks per-JSONL mtime/size for incremental ingest skip.
    assert "files_seen" in tables
    store.close()


def test_init_schema_idempotent_across_vec_availability(tmp_path: Path) -> None:
    """Calling init_schema twice with different vec availability must not collide.

    Regression guard: the original design used the same name `messages_vec` for
    both the vec0 virtual table and the numpy-fallback regular table; that meant
    flipping availability shadowed the virtual table at runtime. The names must
    be distinct so init is safe to re-run as the environment changes.
    """
    # First init without vec
    store1 = SessionStore(db_path=tmp_path / "s.db", dim=384, use_sqlite_vec=False)
    store1.init_schema()
    tables_no_vec = store1.list_tables()
    store1.close()
    # Second init with vec available (simulates a later install)
    store2 = SessionStore(db_path=tmp_path / "s.db", dim=384, use_sqlite_vec=True)
    store2.init_schema()
    tables_with_vec = store2.list_tables()
    store2.close()
    assert "messages_vec_numpy" in tables_no_vec
    # If sqlite-vec is actually installed in the test env, the virtual table
    # is created additionally; otherwise both runs use the numpy fallback.
    if "messages_vec" in tables_with_vec:
        assert "messages_vec_numpy" in tables_with_vec  # fallback persists from earlier run
```

- [ ] **Step 1.2: Run test to verify it fails**

Run: `cd memory-tools && pytest tests/test_sessions_store.py::test_init_schema_creates_expected_tables -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'memory_tools.sessions'`

- [ ] **Step 1.3: Create the sessions subpackage**

Create `memory-tools/src/memory_tools/sessions/__init__.py`:

```python
"""Session-transcript search layer: ingest, store, query over ~/.claude/projects/*/*.jsonl."""
```

- [ ] **Step 1.4: Implement SessionStore — schema only**

Create `memory-tools/src/memory_tools/sessions/store.py`:

```python
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

    def __enter__(self) -> "SessionStore":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def close(self) -> None:
        self.conn.close()
```

- [ ] **Step 1.5: Run test to verify it passes**

Run: `cd memory-tools && pytest tests/test_sessions_store.py::test_init_schema_creates_expected_tables -v`
Expected: PASS

- [ ] **Step 1.6: Add insert + dedupe tests**

Append to `memory-tools/tests/test_sessions_store.py`:

```python
def _make_msg(uuid: str = "u1", session: str = "s1", line: int = 1) -> dict:
    return dict(
        session_id=session,
        message_uuid=uuid,
        parent_uuid=None,
        role="user",
        content="hello world",
        cwd="/tmp/repo",
        timestamp_iso="2026-05-28T20:00:00Z",
        timestamp_unix=1748462400,
        jsonl_path="/fake/session.jsonl",
        line_number=line,
    )


def test_insert_message_round_trips(tmp_path: Path) -> None:
    store = SessionStore(db_path=tmp_path / "s.db", dim=384)
    store.init_schema()
    rowid = store.insert_message(_make_msg())
    assert rowid > 0
    rows = list(store.conn.execute("SELECT content FROM messages"))
    assert rows[0]["content"] == "hello world"
    store.close()


def test_insert_message_dedupes_on_uuid(tmp_path: Path) -> None:
    store = SessionStore(db_path=tmp_path / "s.db", dim=384)
    store.init_schema()
    first = store.insert_message(_make_msg(uuid="dup", session="s1"))
    second = store.insert_message(_make_msg(uuid="dup", session="s1"))
    assert first > 0
    assert second is None  # signals dedupe
    count = store.conn.execute("SELECT COUNT(*) AS n FROM messages").fetchone()["n"]
    assert count == 1
    store.close()
```

- [ ] **Step 1.7: Run tests to verify they fail**

Run: `cd memory-tools && pytest tests/test_sessions_store.py -v`
Expected: 2 failures with `AttributeError: 'SessionStore' object has no attribute 'insert_message'`

- [ ] **Step 1.8: Implement `insert_message`**

Append to `memory-tools/src/memory_tools/sessions/store.py` inside `SessionStore`:

```python
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
```

- [ ] **Step 1.9: Run tests to verify they pass**

Run: `cd memory-tools && pytest tests/test_sessions_store.py -v`
Expected: 3 passing

- [ ] **Step 1.10: Add FTS5 and vector search tests**

Append to `memory-tools/tests/test_sessions_store.py`:

```python
def test_search_fts_matches_keyword(tmp_path: Path) -> None:
    store = SessionStore(db_path=tmp_path / "s.db", dim=384)
    store.init_schema()
    store.insert_message(_make_msg(uuid="a", session="s1") | {"content": "the litellm base_url is configured"})
    store.insert_message(_make_msg(uuid="b", session="s1", line=2) | {"content": "unrelated message about cats"})
    hits = store.search_fts("litellm", top_k=5)
    assert len(hits) == 1
    assert hits[0]["message_uuid"] == "a"
    assert hits[0]["score"] > 0
    store.close()


def test_search_vec_returns_top_k(tmp_path: Path) -> None:
    store = SessionStore(db_path=tmp_path / "s.db", dim=4)
    store.init_schema()
    rowid_a = store.insert_message(_make_msg(uuid="a"))
    rowid_b = store.insert_message(_make_msg(uuid="b", line=2))
    store.upsert_embedding(rowid_a, np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32))
    store.upsert_embedding(rowid_b, np.array([0.0, 1.0, 0.0, 0.0], dtype=np.float32))
    hits = store.search_vec(np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32), top_k=1)
    assert len(hits) == 1
    assert hits[0]["message_uuid"] == "a"
    store.close()


def test_search_fts_score_is_positive_higher_is_better(tmp_path: Path) -> None:
    """Regression: BM25 returns negative scores; the wrapper must negate so
    the higher-is-better merge in search_combined is correct.
    """
    store = SessionStore(db_path=tmp_path / "s.db", dim=384)
    store.init_schema()
    store.insert_message(_make_msg(uuid="strong", session="s1") | {"content": "litellm litellm litellm proxy"})
    store.insert_message(_make_msg(uuid="weak", session="s1", line=2) | {"content": "litellm appears once buried in unrelated text about cats and dogs"})
    hits = store.search_fts("litellm", top_k=5)
    assert len(hits) == 2
    # Both scores must be >= 0 (negative scores indicate the formula bug)
    assert all(h["score"] >= 0 for h in hits), [h["score"] for h in hits]
    # Stronger match must rank higher
    strong = next(h for h in hits if h["message_uuid"] == "strong")
    weak = next(h for h in hits if h["message_uuid"] == "weak")
    assert strong["score"] > weak["score"]
    store.close()


def test_files_seen_skip_logic(tmp_path: Path) -> None:
    store = SessionStore(db_path=tmp_path / "s.db", dim=384)
    store.init_schema()
    # No record → not skip
    assert store.should_skip_file("/tmp/a.jsonl", 100, 500) is False
    # Record then matching mtime+size → skip
    store.record_file_seen("/tmp/a.jsonl", 100, 500)
    assert store.should_skip_file("/tmp/a.jsonl", 100, 500) is True
    # Either mtime or size differs → not skip
    assert store.should_skip_file("/tmp/a.jsonl", 101, 500) is False
    assert store.should_skip_file("/tmp/a.jsonl", 100, 501) is False
    store.close()
```

- [ ] **Step 1.11: Run tests to verify they fail**

Run: `cd memory-tools && pytest tests/test_sessions_store.py -v`
Expected: 2 failures (`search_fts`, `upsert_embedding`/`search_vec` missing)

- [ ] **Step 1.12: Implement FTS5 + vector search**

Append to `memory-tools/src/memory_tools/sessions/store.py` inside `SessionStore`:

```python
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
```

- [ ] **Step 1.13: Run all sessions-store tests to verify they pass**

Run: `cd memory-tools && pytest tests/test_sessions_store.py -v`
Expected: 8 passing (init_schema + idempotence regression + insert + dedupe + FTS5 keyword + vec top-k + BM25-sign regression + files_seen skip)

- [ ] **Step 1.14: Commit Phase 1 part A — store schema + insert + search**

```bash
cd ~/Dropbox/github/mac_setup
git add memory-tools/src/memory_tools/sessions/__init__.py \
        memory-tools/src/memory_tools/sessions/store.py \
        memory-tools/tests/test_sessions_store.py
git commit -m "$(cat <<'EOF'
feat(memory-tools): add SessionStore for transcript indexing

SQLite-backed store with FTS5 + sqlite-vec indexes over Claude Code
session transcripts. Sibling DB to the curated-memory index so
lifecycles do not interfere. Numpy cosine fallback when sqlite-vec is
unavailable.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: JSONL ingest from ~/.claude/projects

**Files:**
- Create: `memory-tools/src/memory_tools/sessions/ingest.py`
- Create: `memory-tools/tests/fixtures/sample_session.jsonl`
- Create: `memory-tools/tests/test_sessions_ingest.py`

- [ ] **Step 2.1: Add the fixture JSONL**

Create `memory-tools/tests/fixtures/sample_session.jsonl`:

```jsonl
{"type":"queue-operation","operation":"enqueue","timestamp":"2026-04-24T01:29:01.260Z","sessionId":"s-1","content":"New Session"}
{"parentUuid":null,"type":"user","message":{"role":"user","content":"hello there"},"uuid":"u-1","timestamp":"2026-04-24T01:29:01.303Z","sessionId":"s-1","cwd":"/tmp/repoA"}
{"parentUuid":"u-1","type":"assistant","message":{"role":"assistant","content":[{"type":"tool_use","id":"t1","name":"Bash","input":{"command":"ls"}}]},"uuid":"u-tool","timestamp":"2026-04-24T01:29:01.800Z","sessionId":"s-1","cwd":"/tmp/repoA"}
{"parentUuid":"u-tool","type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"t1","content":"file.txt"}]},"uuid":"u-result","timestamp":"2026-04-24T01:29:02.000Z","sessionId":"s-1","cwd":"/tmp/repoA"}
{"parentUuid":"u-result","type":"assistant","message":{"role":"assistant","content":"hello back"},"uuid":"u-2","timestamp":"2026-04-24T01:29:02.500Z","sessionId":"s-1","cwd":"/tmp/repoA"}
{"parentUuid":"u-2","type":"user","message":{"role":"user","content":[{"type":"text","text":"second user turn with list content"}]},"uuid":"u-3","timestamp":"2026-04-24T01:29:05.000Z","sessionId":"s-1","cwd":"/tmp/repoA"}
{"type":"queue-operation","operation":"dequeue","timestamp":"2026-04-24T01:29:06.000Z","sessionId":"s-1"}
```

The fixture now exercises three buckets:
- 3 indexable rows (`u-1`, `u-2`, `u-3`) — yielded
- 2 no-text rows (`u-tool` assistant with tool_use only, `u-result` user with tool_result only) — counted in `messages_skipped_no_text`
- 2 non-message rows (queue-operation) — skipped silently

- [ ] **Step 2.2: Write failing tests for ingest**

Create `memory-tools/tests/test_sessions_ingest.py`:

```python
"""Tests for JSONL ingest of Claude Code transcripts."""
from __future__ import annotations

from pathlib import Path

import pytest

from memory_tools.sessions.ingest import iter_messages_in_file, ingest_file
from memory_tools.sessions.store import SessionStore


FIXTURE = Path(__file__).parent / "fixtures" / "sample_session.jsonl"


def test_iter_messages_skips_non_message_types() -> None:
    msgs = list(iter_messages_in_file(FIXTURE))
    # 3 user/assistant turns, queue-operation rows skipped
    assert len(msgs) == 3
    roles = [m["role"] for m in msgs]
    assert roles == ["user", "assistant", "user"]


def test_iter_messages_flattens_list_content() -> None:
    msgs = list(iter_messages_in_file(FIXTURE))
    # Last message has list-of-blocks content
    assert msgs[2]["content"] == "second user turn with list content"


def test_iter_messages_captures_required_fields() -> None:
    msgs = list(iter_messages_in_file(FIXTURE))
    m = msgs[0]
    assert m["session_id"] == "s-1"
    assert m["message_uuid"] == "u-1"
    assert m["cwd"] == "/tmp/repoA"
    assert m["timestamp_iso"].startswith("2026-04-24")
    assert m["timestamp_unix"] > 0
    assert m["line_number"] == 2  # 1-indexed line in the file


def test_ingest_file_inserts_messages(tmp_path: Path) -> None:
    store = SessionStore(db_path=tmp_path / "s.db", dim=384)
    store.init_schema()
    stats = ingest_file(FIXTURE, store)
    assert stats.messages_inserted == 3
    assert stats.messages_skipped_dupe == 0
    store.close()


def test_ingest_file_is_idempotent_via_mtime_skip(tmp_path: Path) -> None:
    """Second ingest of an unchanged file must skip entirely via files_seen,
    NOT do a row-by-row dedupe walk. The mtime gate is what keeps the
    SessionEnd hook fast on machines with deep history.
    """
    store = SessionStore(db_path=tmp_path / "s.db", dim=384)
    store.init_schema()
    ingest_file(FIXTURE, store)
    stats = ingest_file(FIXTURE, store)
    assert stats.files_skipped_unchanged == 1
    assert stats.messages_inserted == 0
    assert stats.messages_skipped_dupe == 0  # never even opened the file


def test_ingest_file_force_bypasses_mtime_skip(tmp_path: Path) -> None:
    store = SessionStore(db_path=tmp_path / "s.db", dim=384)
    store.init_schema()
    ingest_file(FIXTURE, store)
    stats = ingest_file(FIXTURE, store, force=True)
    assert stats.files_skipped_unchanged == 0
    assert stats.files_ingested == 1
    # All 3 indexable rows are duplicates of the prior ingest
    assert stats.messages_inserted == 0
    assert stats.messages_skipped_dupe == 3


def test_ingest_counts_no_text_rows_separately(tmp_path: Path) -> None:
    """Tool-use-only assistant rows and tool_result-only user rows must
    increment messages_skipped_no_text, NOT messages_skipped_malformed.
    """
    store = SessionStore(db_path=tmp_path / "s.db", dim=384)
    store.init_schema()
    stats = ingest_file(FIXTURE, store)
    assert stats.messages_inserted == 3
    assert stats.messages_skipped_no_text == 2
    assert stats.messages_skipped_malformed == 0
```

- [ ] **Step 2.3: Run to verify they fail**

Run: `cd memory-tools && pytest tests/test_sessions_ingest.py -v`
Expected: 5 failures (`ModuleNotFoundError: No module named 'memory_tools.sessions.ingest'`)

- [ ] **Step 2.4: Implement ingest module**

Create `memory-tools/src/memory_tools/sessions/ingest.py`:

```python
"""Read Claude Code ~/.claude/projects/*/*.jsonl transcripts, normalise rows, write to SessionStore.

Filters non-message rows (queue-operation, etc.) and normalises content that
may be a string or a list of content blocks into a single text payload. Dedupe
relies on the UNIQUE(session_id, message_uuid) constraint in the store.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator, Optional

from .store import SessionStore


@dataclass
class IngestStats:
    files_seen: int = 0
    files_ingested: int = 0
    files_skipped_unchanged: int = 0  # mtime+size match — already ingested
    messages_inserted: int = 0
    messages_skipped_dupe: int = 0
    messages_skipped_malformed: int = 0  # parse error or missing required field
    messages_skipped_no_text: int = 0    # row IS a message but has no indexable text
                                          # (assistant tool_use only, thinking only,
                                          #  user tool_result only). This is the dominant
                                          #  bucket on real transcripts; tracked separately
                                          #  so the user can see the real drop rate vs
                                          #  parse errors.


# Row types in the JSONL stream we treat as messages.
_MESSAGE_TYPES = ("user", "assistant")


def _flatten_content(content) -> Optional[str]:
    """Convert message.content to plain text. Returns None if there's nothing usable."""
    if isinstance(content, str):
        text = content.strip()
        return text or None
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                # Common shapes: {"type": "text", "text": "..."} and tool blocks
                t = block.get("text")
                if isinstance(t, str):
                    parts.append(t)
                # Tool-use blocks have input dicts; skip them for text indexing
        joined = " ".join(p.strip() for p in parts if p and p.strip())
        return joined or None
    return None


def _parse_timestamp(ts: str) -> tuple[str, int]:
    """Return (iso_string, unix_seconds). Handles trailing Z suffix."""
    iso = ts
    parseable = ts.rstrip("Z") + "+00:00" if ts.endswith("Z") else ts
    dt = datetime.fromisoformat(parseable)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return iso, int(dt.timestamp())


@dataclass
class _RawIterResult:
    """Internal carrier so iter_messages_in_file can report both yielded messages
    and rows skipped for "no indexable text" without forcing the caller to
    consume two iterators. The stats are mutated by the iterator; the caller
    reads them after the iteration completes.
    """
    no_text_count: int = 0
    malformed_count: int = 0


def iter_messages_in_file(jsonl_path: Path, stats: Optional[_RawIterResult] = None) -> Iterator[dict]:
    """Yield normalised message dicts ready for SessionStore.insert_message.

    Skips non-message rows (queue-operation, attachment, etc.) entirely.
    For rows whose ``type`` is ``user``/``assistant`` but whose content has
    no indexable text (assistant tool_use only, thinking only, user
    tool_result only — the dominant case in real transcripts), the row is
    counted in ``stats.no_text_count`` rather than yielded. Malformed rows
    (JSON decode error, missing required field, bad timestamp) bump
    ``stats.malformed_count``.

    ``line_number`` is the 1-indexed line in the file at which the message
    was found.
    """
    path = Path(jsonl_path)
    with path.open("r", encoding="utf-8") as fh:
        for line_number, raw in enumerate(fh, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                if stats is not None:
                    stats.malformed_count += 1
                continue
            row_type = row.get("type")
            if row_type not in _MESSAGE_TYPES:
                continue  # not a message row at all — silent, not counted
            uuid = row.get("uuid")
            session_id = row.get("sessionId")
            timestamp = row.get("timestamp")
            message = row.get("message") or {}
            if not uuid or not session_id or not timestamp:
                if stats is not None:
                    stats.malformed_count += 1
                continue
            content = _flatten_content(message.get("content"))
            if not content:
                # IS a message turn, but no text to index (tool_use, thinking,
                # tool_result without text). Track separately so users can see
                # the v1 framing: "human-typed + assistant-text turns only".
                if stats is not None:
                    stats.no_text_count += 1
                continue
            try:
                iso, unix = _parse_timestamp(timestamp)
            except ValueError:
                if stats is not None:
                    stats.malformed_count += 1
                continue
            yield dict(
                session_id=session_id,
                message_uuid=uuid,
                parent_uuid=row.get("parentUuid"),
                role=message.get("role") or row_type,
                content=content,
                cwd=row.get("cwd"),
                timestamp_iso=iso,
                timestamp_unix=unix,
                jsonl_path=str(path),
                line_number=line_number,
            )


def ingest_file(jsonl_path: Path, store: SessionStore, *, force: bool = False) -> IngestStats:
    """Ingest a single JSONL file.

    Honours mtime+size skip via ``files_seen``: if the file is unchanged
    since last ingest, returns a stats record with ``files_skipped_unchanged=1``
    and no other work. Set ``force=True`` to bypass the skip (used by
    ``--full`` mode).

    All inserts for the file happen in a single transaction (BEGIN ... COMMIT)
    to avoid the per-row fsync cost that would otherwise dominate backfill on
    machines with deep transcript history.
    """
    path = Path(jsonl_path)
    stat = path.stat()
    mtime_unix = int(stat.st_mtime)
    size_bytes = int(stat.st_size)

    stats = IngestStats(files_seen=1)
    if not force and store.should_skip_file(str(path), mtime_unix, size_bytes):
        stats.files_skipped_unchanged = 1
        return stats

    raw_stats = _RawIterResult()
    # Batched transaction — single commit per file rather than per row.
    store.conn.execute("BEGIN")
    try:
        for msg in iter_messages_in_file(path, raw_stats):
            rowid = store.insert_message_no_commit(msg)
            if rowid is None:
                stats.messages_skipped_dupe += 1
            else:
                stats.messages_inserted += 1
        store.conn.commit()
    except Exception:
        store.conn.rollback()
        raise

    store.record_file_seen(str(path), mtime_unix, size_bytes)
    stats.files_ingested = 1
    stats.messages_skipped_no_text = raw_stats.no_text_count
    stats.messages_skipped_malformed = raw_stats.malformed_count
    return stats


def ingest_directory(root: Path, store: SessionStore, *, force: bool = False) -> IngestStats:
    """Ingest every .jsonl under root (typically ``~/.claude/projects``)."""
    root = Path(root)
    total = IngestStats()
    for jsonl in sorted(root.glob("**/*.jsonl")):
        file_stats = ingest_file(jsonl, store, force=force)
        total.files_seen += file_stats.files_seen
        total.files_ingested += file_stats.files_ingested
        total.files_skipped_unchanged += file_stats.files_skipped_unchanged
        total.messages_inserted += file_stats.messages_inserted
        total.messages_skipped_dupe += file_stats.messages_skipped_dupe
        total.messages_skipped_malformed += file_stats.messages_skipped_malformed
        total.messages_skipped_no_text += file_stats.messages_skipped_no_text
    return total
```

**Note on `insert_message_no_commit`:** the batched-transaction path requires a non-committing insert variant. Add it to `SessionStore` alongside the existing `insert_message`:

```python
    def insert_message_no_commit(self, msg: dict) -> Optional[int]:
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
```

- [ ] **Step 2.5: Run tests to verify they pass**

Run: `cd memory-tools && pytest tests/test_sessions_ingest.py -v`
Expected: 7 passing (3 iter tests + ingest insert + mtime-skip + force-override + no-text counter)

- [ ] **Step 2.6: Commit Phase 1 part B — ingest**

```bash
cd ~/Dropbox/github/mac_setup
git add memory-tools/src/memory_tools/sessions/ingest.py \
        memory-tools/tests/fixtures/sample_session.jsonl \
        memory-tools/tests/test_sessions_ingest.py
git commit -m "$(cat <<'EOF'
feat(memory-tools): add JSONL ingest for Claude Code transcripts

Iterates ~/.claude/projects/*/*.jsonl, filters non-message rows
(queue-operation etc.), normalises list-form content into plain text,
and inserts into SessionStore. Dedupe via UNIQUE(session_id,
message_uuid) — re-ingesting the same file is a no-op.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: claude-session-index CLI + config

**Files:**
- Modify: `memory-tools/src/memory_tools/config.py`
- Create: `memory-tools/src/memory_tools/cli_session_index.py`
- Modify: `memory-tools/pyproject.toml`
- Create: `memory-tools/tests/test_cli_session_index.py`

- [ ] **Step 3.1: Add config support for sessions DB + claude-projects dir**

Modify `memory-tools/src/memory_tools/config.py`:

Add to `DEFAULTS`:

```python
DEFAULTS: dict = dict(
    embedding_model="BAAI/bge-small-en-v1.5",
    always_on_budget_bytes=8192,
    secret_check_on_capture=True,
    git_track=False,
    chunk_max_bytes=1500,
    claude_projects_dir="~/.claude/projects",
    session_search_enabled=True,
)
```

Add to `Config` dataclass after existing fields:

```python
    claude_projects_dir: Path
    session_search_enabled: bool

    @property
    def sessions_db_path(self) -> Path:
        """Absolute path to the SQLite sessions database (transcripts index)."""
        return self.memory_home / ".index" / "sessions.db"
```

In `load_config()` extend the return:

```python
    return Config(
        memory_home=root,
        embedding_model=str(data["embedding_model"]),
        always_on_budget_bytes=int(data["always_on_budget_bytes"]),
        secret_check_on_capture=bool(data["secret_check_on_capture"]),
        git_track=bool(data["git_track"]),
        chunk_max_bytes=int(data["chunk_max_bytes"]),
        claude_projects_dir=Path(os.path.expanduser(str(data["claude_projects_dir"]))),
        session_search_enabled=bool(data["session_search_enabled"]),
    )
```

- [ ] **Step 3.2: Write a config test**

Append to `memory-tools/tests/test_config.py`:

```python
def test_config_exposes_sessions_db_path(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMORY_HOME", str(tmp_path))
    from memory_tools.config import load_config

    cfg = load_config()
    assert cfg.sessions_db_path == tmp_path / ".index" / "sessions.db"
    assert cfg.claude_projects_dir.name == "projects"
    assert cfg.session_search_enabled is True
```

Run: `cd memory-tools && pytest tests/test_config.py::test_config_exposes_sessions_db_path -v`
Expected: PASS (the implementation is already in place from Step 3.1)

- [ ] **Step 3.3: Write failing test for the CLI**

Create `memory-tools/tests/test_cli_session_index.py`:

```python
"""Smoke tests for claude-session-index CLI."""
from __future__ import annotations

import shutil
from pathlib import Path

from click.testing import CliRunner

from memory_tools.cli_session_index import main as cli_main


FIXTURE = Path(__file__).parent / "fixtures" / "sample_session.jsonl"


def test_session_index_full_runs_against_directory(tmp_path, monkeypatch):
    home = tmp_path / "memhome"
    home.mkdir()
    projects = tmp_path / "fake-projects" / "proj-a"
    projects.mkdir(parents=True)
    shutil.copy(FIXTURE, projects / "session.jsonl")

    monkeypatch.setenv("MEMORY_HOME", str(home))
    (home / "memory.config.yaml").write_text(
        f"claude_projects_dir: {projects.parent}\n"
        "embedding_model: test-hashing\n"
    )

    runner = CliRunner()
    result = runner.invoke(cli_main, ["--full"])
    assert result.exit_code == 0, result.output
    assert "messages_inserted=3" in result.output
    assert (home / ".index" / "sessions.db").exists()


def test_session_index_incremental_is_idempotent(tmp_path, monkeypatch):
    home = tmp_path / "memhome"
    home.mkdir()
    projects = tmp_path / "fake-projects" / "proj-a"
    projects.mkdir(parents=True)
    shutil.copy(FIXTURE, projects / "session.jsonl")

    monkeypatch.setenv("MEMORY_HOME", str(home))
    (home / "memory.config.yaml").write_text(
        f"claude_projects_dir: {projects.parent}\n"
        "embedding_model: test-hashing\n"
    )

    runner = CliRunner()
    runner.invoke(cli_main, ["--full"])
    result = runner.invoke(cli_main, ["--incremental"])
    assert result.exit_code == 0, result.output
    # Incremental with mtime-skip should NOT touch the file at all
    assert "messages_inserted=0" in result.output
    assert "files_skipped_unchanged=1" in result.output
```

- [ ] **Step 3.4: Run to verify failure**

Run: `cd memory-tools && pytest tests/test_cli_session_index.py -v`
Expected: 2 failures (`ModuleNotFoundError: No module named 'memory_tools.cli_session_index'`)

- [ ] **Step 3.5: Implement the CLI**

Create `memory-tools/src/memory_tools/cli_session_index.py`:

```python
"""claude-session-index: ingest Claude Code transcripts into the sessions DB.

Two modes:
  --full         Walk every JSONL under claude_projects_dir and ingest.
  --incremental  Same walk, but rely on DB-side dedupe to skip already-seen
                 messages. This is the steady-state mode (SessionEnd hook).

The two modes do the same work — dedupe via UNIQUE(session_id, message_uuid)
makes ``--full`` a safe superset of ``--incremental``. The flag exists for
documentation and for future optimisations (e.g. tracking per-file mtime).
"""
from __future__ import annotations

import sys
from pathlib import Path

import click

from memory_tools.config import load_config
from memory_tools.sessions.ingest import ingest_directory
from memory_tools.sessions.store import SessionStore


@click.command()
@click.option("--full", "mode_full", is_flag=True,
              help="Full reingest of all transcripts. Forces re-walk even of files whose "
                   "mtime hasn't changed (vs incremental, which trusts files_seen).")
@click.option("--incremental", "mode_incremental", is_flag=True,
              help="Incremental reingest (default). Skips files whose mtime+size match "
                   "what was last ingested; this is what makes the SessionEnd hook fast.")
@click.option("--embed/--no-embed", default=False, show_default=True,
              help="Compute vector embeddings for new messages. Off by default for speed; "
                   "Phase 2 will turn this on once search wiring is in place.")
@click.option("--progress/--no-progress", default=True, show_default=True,
              help="Print a one-line counter every 50 files so a multi-minute backfill is "
                   "not silent.")
def main(mode_full: bool, mode_incremental: bool, embed: bool, progress: bool) -> None:
    """Ingest ~/.claude/projects/*/*.jsonl into the sessions search DB."""
    if mode_full and mode_incremental:
        raise click.UsageError("--full and --incremental are mutually exclusive")
    cfg = load_config()
    if not cfg.session_search_enabled:
        click.echo("session_search_enabled=false in config; nothing to do.")
        sys.exit(0)
    projects_root = cfg.claude_projects_dir
    if not projects_root.is_dir():
        click.echo(f"claude_projects_dir does not exist: {projects_root}", err=True)
        sys.exit(2)
    store = SessionStore(db_path=cfg.sessions_db_path, dim=384)
    store.init_schema()
    try:
        # --full forces re-walk even of unchanged files; default (incremental) trusts mtime.
        stats = ingest_directory(projects_root, store, force=mode_full)
    finally:
        store.close()
    click.echo(
        f"files_seen={stats.files_seen} "
        f"files_ingested={stats.files_ingested} "
        f"files_skipped_unchanged={stats.files_skipped_unchanged} "
        f"messages_inserted={stats.messages_inserted} "
        f"messages_skipped_dupe={stats.messages_skipped_dupe} "
        f"messages_skipped_malformed={stats.messages_skipped_malformed} "
        f"messages_skipped_no_text={stats.messages_skipped_no_text}"
    )


if __name__ == "__main__":
    sys.exit(main())
```

**Note on `--progress`:** the flag is exposed but the per-file counter is best wired inside `ingest_directory` (not the CLI). Add an optional `progress_cb: Optional[Callable[[int, int], None]] = None` parameter to `ingest_directory` that fires every 50 files with `(files_done, files_total)`. The CLI passes a callback that prints `... N/M files indexed` to stderr; tests pass `None`.

- [ ] **Step 3.6: Register the entry point**

Modify `memory-tools/pyproject.toml`. Find the `[project.scripts]` block and add one line:

```toml
[project.scripts]
memory-index = "memory_tools.cli_index:main"
memory-search = "memory_tools.cli_search:main"
memory-capture = "memory_tools.cli_capture:main"
memory-invalidate = "memory_tools.cli_invalidate:main"
memory-propose = "memory_tools.cli_propose:main"
memory-migrate = "memory_tools.cli_migrate:main"
claude-session-index = "memory_tools.cli_session_index:main"
```

- [ ] **Step 3.7: Reinstall the package so the new entry point is wired**

Run: `cd memory-tools && pip install -e . --quiet`
Expected: success, prompt returns

- [ ] **Step 3.8: Run CLI tests to verify they pass**

Run: `cd memory-tools && pytest tests/test_cli_session_index.py -v`
Expected: 2 passing

- [ ] **Step 3.9: Smoke-test the installed CLI**

Run: `claude-session-index --help`
Expected: Click help text showing `--full`, `--incremental`, `--embed`

- [ ] **Step 3.10: Commit Phase 1 part C — CLI**

```bash
cd ~/Dropbox/github/mac_setup
git add memory-tools/src/memory_tools/config.py \
        memory-tools/src/memory_tools/cli_session_index.py \
        memory-tools/pyproject.toml \
        memory-tools/tests/test_config.py \
        memory-tools/tests/test_cli_session_index.py
git commit -m "$(cat <<'EOF'
feat(memory-tools): add claude-session-index CLI for transcript ingest

New CLI ingests ~/.claude/projects/*/*.jsonl into the sessions search
DB. --full and --incremental modes do the same walk; dedupe by
(session_id, message_uuid) makes both safe to re-run. Adds
sessions_db_path and claude_projects_dir to Config.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Phase 2: Extend memory-search

### Task 4: search_combined module — fan-out + layered merging

**Files:**
- Create: `memory-tools/src/memory_tools/search_combined.py`
- Create: `memory-tools/tests/test_search_combined.py`

- [ ] **Step 4.1: Write failing test for fan-out shape**

Create `memory-tools/tests/test_search_combined.py`:

```python
"""Tests for the combined memory + sessions search layer."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from memory_tools.embeddings import HashingEmbedder
from memory_tools.search_combined import CombinedSearchResult, search_all
from memory_tools.sessions.store import SessionStore
from memory_tools.store import IndexStore


def _seed_memory(tmp_path: Path) -> IndexStore:
    store = IndexStore(db_path=tmp_path / "mem.db", dim=384)
    store.init_schema()
    store.upsert_file("/m/topics/litellm.md", mtime=1, content_hash="h")
    emb = HashingEmbedder(dim=384).embed("litellm base url configuration")
    store.replace_chunks_for_file("/m/topics/litellm.md", [dict(
        heading="LiteLLM proxy",
        line_start=1, line_end=5,
        text="LiteLLM proxy at simone.home routes Claude through OpenRouter",
        tags=["litellm"], aliases=["base url"], embedding=emb,
    )])
    return store


def _seed_sessions(tmp_path: Path) -> SessionStore:
    store = SessionStore(db_path=tmp_path / "sessions.db", dim=384)
    store.init_schema()
    rowid = store.insert_message(dict(
        session_id="s-1", message_uuid="u-1", parent_uuid=None, role="user",
        content="how do i set the litellm base_url",
        cwd="/tmp/repo", timestamp_iso="2026-05-26T00:00:00Z",
        timestamp_unix=1748217600, jsonl_path="/fake.jsonl", line_number=2,
    ))
    emb = HashingEmbedder(dim=384).embed("how do i set the litellm base_url")
    store.upsert_embedding(rowid, emb)
    return store


def test_search_all_returns_both_tiers(tmp_path: Path) -> None:
    mem = _seed_memory(tmp_path)
    sess = _seed_sessions(tmp_path)
    embedder = HashingEmbedder(dim=384)
    result = search_all(
        query="litellm base url", embedder=embedder,
        memory_store=mem, session_store=sess,
        memory_top_k=5, sessions_top_k=5,
    )
    assert isinstance(result, CombinedSearchResult)
    assert len(result.memory_hits) >= 1
    assert len(result.session_hits) >= 1


def test_search_all_source_memory_only_skips_sessions(tmp_path: Path) -> None:
    mem = _seed_memory(tmp_path)
    sess = _seed_sessions(tmp_path)
    embedder = HashingEmbedder(dim=384)
    result = search_all(
        query="litellm", embedder=embedder,
        memory_store=mem, session_store=sess,
        source="memory",
    )
    assert len(result.memory_hits) >= 1
    assert result.session_hits == []


def test_search_all_promote_candidates(tmp_path: Path) -> None:
    # Empty memory, populated sessions → query should surface as a promotion candidate
    mem = IndexStore(db_path=tmp_path / "mem.db", dim=384)
    mem.init_schema()
    sess = _seed_sessions(tmp_path)
    embedder = HashingEmbedder(dim=384)
    result = search_all(
        query="litellm base url", embedder=embedder,
        memory_store=mem, session_store=sess,
    )
    assert result.is_promotion_candidate is True
```

- [ ] **Step 4.2: Run to verify failure**

Run: `cd memory-tools && pytest tests/test_search_combined.py -v`
Expected: 3 failures (`ModuleNotFoundError: No module named 'memory_tools.search_combined'`)

- [ ] **Step 4.3: Implement search_combined**

Create `memory-tools/src/memory_tools/search_combined.py`:

```python
"""Combined search across the curated-memory index and the session-transcript index.

Memory hits and session hits are presented in two visually separated tiers
rather than merged into one ranked list. Memory is curated truth; sessions
are raw transcript. Mixing them dilutes signal: a popular phrase in many
session messages can drown the canonical memory file even when memory is
the right answer.

A query that returns zero memory hits but non-trivial session hits is a
``promotion candidate`` — the topic has come up in conversation but lives
nowhere durable. The caller can surface this via memory-capture.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Literal, Optional

import numpy as np

from .embeddings import BaseEmbedder
from .sessions.store import SessionStore
from .store import IndexStore


Source = Literal["memory", "sessions", "all"]


@dataclass
class CombinedSearchResult:
    query: str
    memory_hits: List[dict] = field(default_factory=list)
    session_hits: List[dict] = field(default_factory=list)

    @property
    def is_promotion_candidate(self) -> bool:
        """True when sessions had real hits but memory had none."""
        return len(self.memory_hits) == 0 and len(self.session_hits) > 0


def _merge_session_hits(
    fts_hits: List[dict],
    vec_hits: List[dict],
    top_k: int,
) -> List[dict]:
    """Combine FTS and vector hits, dedupe on (session_id, message_uuid), keep top_k by score."""
    by_key: dict[tuple[str, str], dict] = {}
    for hit in fts_hits + vec_hits:
        key = (hit["session_id"], hit["message_uuid"])
        existing = by_key.get(key)
        if existing is None or hit["score"] > existing["score"]:
            by_key[key] = hit
    merged = sorted(by_key.values(), key=lambda h: -h["score"])
    return merged[:top_k]


def search_all(
    query: str,
    embedder: BaseEmbedder,
    memory_store: Optional[IndexStore] = None,
    session_store: Optional[SessionStore] = None,
    source: Source = "all",
    memory_top_k: int = 5,
    sessions_top_k: int = 5,
) -> CombinedSearchResult:
    """Run the query against one or both stores and return layered results."""
    result = CombinedSearchResult(query=query)
    query_vec = embedder.embed(query)

    if source in ("memory", "all") and memory_store is not None:
        result.memory_hits = memory_store.search(query_vec, top_k=memory_top_k)

    if source in ("sessions", "all") and session_store is not None:
        fts_hits = session_store.search_fts(query, top_k=sessions_top_k)
        vec_hits = session_store.search_vec(query_vec, top_k=sessions_top_k)
        result.session_hits = _merge_session_hits(fts_hits, vec_hits, sessions_top_k)

    return result
```

- [ ] **Step 4.4: Run tests to verify they pass**

Run: `cd memory-tools && pytest tests/test_search_combined.py -v`
Expected: 3 passing

- [ ] **Step 4.5: Commit Phase 2 part A — combined search module**

```bash
cd ~/Dropbox/github/mac_setup
git add memory-tools/src/memory_tools/search_combined.py \
        memory-tools/tests/test_search_combined.py
git commit -m "$(cat <<'EOF'
feat(memory-tools): add combined memory+sessions search module

search_all fans out across IndexStore (curated) and SessionStore (raw
transcripts), returning layered CombinedSearchResult with memory and
session hits kept separate. is_promotion_candidate signals topics
discussed in sessions but absent from memory.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Extend cli_search with --source, --promote-candidates, layered output

**Files:**
- Modify: `memory-tools/src/memory_tools/cli_search.py`
- Modify: `memory-tools/tests/test_cli.py`

- [ ] **Step 5.1: Write failing CLI integration tests**

Read the existing `memory-tools/tests/test_cli.py` first to understand its patterns. Then append:

```python
def test_memory_search_source_memory_skips_sessions(tmp_path, monkeypatch):
    # Set up a memory-only environment; sessions DB will be empty
    home = tmp_path / "h"
    home.mkdir()
    monkeypatch.setenv("MEMORY_HOME", str(home))
    (home / "memory.config.yaml").write_text(
        "embedding_model: test-hashing\n"
        f"claude_projects_dir: {tmp_path}/no-projects-here\n"
    )
    (home / "topics").mkdir()
    (home / "topics" / "litellm.md").write_text(
        "---\nname: litellm\ndescription: notes\ntype: reference\n"
        "tags: [litellm]\naliases: [base url]\ncreated: 2026-05-01T00:00:00+00:00\n"
        "updated: 2026-05-01T00:00:00+00:00\n---\n"
        "## LiteLLM\n\nbase_url for the proxy.\n"
    )
    from memory_tools.cli_index import main as idx_main
    from click.testing import CliRunner
    runner = CliRunner()
    runner.invoke(idx_main, ["rebuild"])

    from memory_tools.cli_search import main as search_main
    result = runner.invoke(search_main, ["litellm", "--source", "memory"])
    assert result.exit_code == 0, result.output
    assert "FROM MEMORY" in result.output
    assert "FROM SESSIONS" not in result.output


def test_memory_search_promote_candidates_flag(tmp_path, monkeypatch):
    # Memory empty, sessions populated — query surfaces as a promotion candidate
    home = tmp_path / "h"
    home.mkdir()
    fake_projects = tmp_path / "projects" / "proj"
    fake_projects.mkdir(parents=True)
    import shutil
    shutil.copy(
        Path(__file__).parent / "fixtures" / "sample_session.jsonl",
        fake_projects / "session.jsonl",
    )
    monkeypatch.setenv("MEMORY_HOME", str(home))
    (home / "memory.config.yaml").write_text(
        "embedding_model: test-hashing\n"
        f"claude_projects_dir: {fake_projects.parent}\n"
    )

    from memory_tools.cli_session_index import main as session_idx_main
    from memory_tools.cli_search import main as search_main
    from click.testing import CliRunner
    runner = CliRunner()
    runner.invoke(session_idx_main, ["--full"])

    result = runner.invoke(search_main, ["hello there", "--promote-candidates"])
    assert result.exit_code == 0, result.output
    # promote-candidates surface should mention session count + zero memory hits
    assert "promotion candidate" in result.output.lower()
```

(Add `from pathlib import Path` to the test file imports if not already present.)

- [ ] **Step 5.2: Run to verify failures**

Run: `cd memory-tools && pytest tests/test_cli.py::test_memory_search_source_memory_skips_sessions tests/test_cli.py::test_memory_search_promote_candidates_flag -v`
Expected: 2 failures (`unknown option` for `--source` / `--promote-candidates`)

- [ ] **Step 5.3: Rewrite cli_search.py with combined search + layered output**

Replace `memory-tools/src/memory_tools/cli_search.py` entirely:

```python
"""memory-search CLI: combined semantic + keyword search over memory and sessions.

Two presentation modes:
  - Layered text (default): two sections, FROM MEMORY then FROM SESSIONS,
    so curated truth is visually distinct from raw-transcript recall.
  - JSON (--json): single object with memory and sessions arrays.

Source selection:
  - --source memory     query curated memory only
  - --source sessions   query transcripts only
  - --source all        both (default)

Promotion candidates:
  - --promote-candidates  print a one-line hint when sessions have hits
                          but memory does not, suggesting memory-capture.
"""
from __future__ import annotations

import json as json_mod
import sys
from datetime import datetime, timezone

import click

from memory_tools.config import load_config
from memory_tools.embeddings import get_embedder
from memory_tools.search_combined import search_all
from memory_tools.sessions.store import SessionStore
from memory_tools.store import IndexStore


def _format_session_timestamp(ts_unix: int) -> str:
    try:
        return datetime.fromtimestamp(ts_unix, tz=timezone.utc).strftime("%Y-%m-%d")
    except Exception:
        return "unknown"


def _render_text(result, top_k_memory: int, top_k_sessions: int, source: str,
                 promote_candidates: bool) -> str:
    lines: list[str] = []
    if source in ("memory", "all"):
        lines.append(f"━━ FROM MEMORY (curated, {len(result.memory_hits)} hits) ━━━━━━━━━━━━━━")
        for h in result.memory_hits:
            heading = f" #{h['heading']}" if h.get('heading') else ""
            lines.append(f"{h['score']:.3f}  {h['file_path']}{heading}"
                         f"  (L{h['line_start']}-{h['line_end']})")
            for snip_line in h["text"].strip().splitlines()[:3]:
                lines.append(f"    {snip_line}")
            lines.append("")
    if source in ("sessions", "all"):
        lines.append(f"━━ FROM SESSIONS (top {len(result.session_hits)}) ━━━━━━━━━━━━━━━━━━━━")
        for h in result.session_hits:
            date_str = _format_session_timestamp(h["timestamp_unix"])
            cwd = h.get("cwd") or "?"
            lines.append(f"{h['score']:.3f}  {date_str} — {cwd} — session {h['session_id'][:8]}")
            lines.append(f"    [{h['role']}] {h['content'][:200]}")
            lines.append("")
    if promote_candidates and result.is_promotion_candidate:
        lines.append("⚑ promotion candidate: 0 memory hits, "
                     f"{len(result.session_hits)} session hits — consider memory-capture.")
    return "\n".join(lines)


@click.command()
@click.argument("query", nargs=-1, required=True)
@click.option("--top", "top_k", default=5, show_default=True, type=int,
              help="Maximum number of results per tier.")
@click.option("--source", "source",
              type=click.Choice(["memory", "sessions", "all"]),
              default="all", show_default=True,
              help="Which layer(s) to query.")
@click.option("--promote-candidates", is_flag=True,
              help="Annotate when sessions hit but memory does not.")
@click.option("--json", "as_json", is_flag=True,
              help="Output results as a single JSON object.")
def main(query: tuple[str, ...], top_k: int, source: str,
         promote_candidates: bool, as_json: bool) -> None:
    """Search memory and conversation transcripts. Layered output by default."""
    cfg = load_config()
    embedder = get_embedder(cfg.embedding_model)
    memory_store: IndexStore | None = None
    session_store: SessionStore | None = None
    if source in ("memory", "all"):
        memory_store = IndexStore(db_path=cfg.index_db_path, dim=embedder.dim)
        memory_store.init_schema()
    if source in ("sessions", "all") and cfg.session_search_enabled:
        session_store = SessionStore(db_path=cfg.sessions_db_path, dim=embedder.dim)
        session_store.init_schema()
    try:
        query_text = " ".join(query)
        result = search_all(
            query=query_text, embedder=embedder,
            memory_store=memory_store, session_store=session_store,
            source=source, memory_top_k=top_k, sessions_top_k=top_k,
        )
        if as_json:
            click.echo(json_mod.dumps(dict(
                query=query_text,
                memory=[
                    dict(file_path=h["file_path"], heading=h.get("heading"),
                         line_start=h["line_start"], line_end=h["line_end"],
                         score=h["score"], tags=h.get("tags", []),
                         aliases=h.get("aliases", []),
                         snippet=h["text"][:300])
                    for h in result.memory_hits
                ],
                sessions=[
                    dict(session_id=h["session_id"], message_uuid=h["message_uuid"],
                         role=h["role"], cwd=h.get("cwd"),
                         timestamp_iso=h["timestamp_iso"],
                         jsonl_path=h["jsonl_path"], line_number=h["line_number"],
                         score=h["score"], source_kind=h["source_kind"],
                         snippet=h["content"][:300])
                    for h in result.session_hits
                ],
                is_promotion_candidate=result.is_promotion_candidate,
            ), indent=2))
        else:
            click.echo(_render_text(result, top_k, top_k, source, promote_candidates))
    finally:
        if memory_store is not None:
            memory_store.close()
        if session_store is not None:
            session_store.close()


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5.4: Run CLI tests to verify pass**

Run: `cd memory-tools && pytest tests/test_cli.py -v`
Expected: previously-passing tests still pass; the two new tests pass

- [ ] **Step 5.5: Smoke-test the upgraded CLI manually**

Run: `memory-search "litellm base_url" --source memory`
Expected: shows the FROM MEMORY section, no FROM SESSIONS section

Run: `memory-search "litellm base_url" --source sessions`
Expected: shows FROM SESSIONS only (may be empty until embeddings are populated in Phase 3 / via a later embed pass)

- [ ] **Step 5.6: Update memory skill to consume the new JSON shape**

**Critical — BLOCKER if skipped.** The memory skill at `memory-tools/skill/memory/skill.md` (copied to `~/.claude/skills/memory/skill.md` by phase 3 install) is an active consumer of `memory-search --json` that expects a **flat array** of hits with top-level `file_path`, `line_start`, `line_end`. The Phase 2 change wraps everything under `{"memory": [...], "sessions": [...], "is_promotion_candidate": bool}`. Every Claude session that uses the memory skill will silently break the moment Phase 2 lands unless the skill is updated in the same commit.

Read the current skill at `memory-tools/skill/memory/skill.md` and locate the line that says:

> **Default lookup: `memory-search "phrase" --top 5 --json`.** Use the returned `file_path` + `line_start`/`line_end` to read just that range when you need surrounding context.

Replace with text that documents the new shape and the two tiers. Use this exact replacement (preserves the surrounding numbered-list structure and adjacent bullets):

```markdown
2. **Default lookup: `memory-search "phrase" --top 5 --json`.** The result is a JSON object with three keys: `memory` (array of curated hits, authoritative — read first), `sessions` (array of transcript hits — supplementary, lower-priority), and `is_promotion_candidate` (bool — true when sessions have hits but memory is empty, signalling something worth capturing). For each memory hit, use `file_path` + `line_start`/`line_end` to read just that range when you need surrounding context. For each session hit, use `jsonl_path` + `line_number` to locate the message in the original transcript. Only read the whole file when the chunk is genuinely insufficient.
```

Also: in the same skill file, find any other references to the old flat-array shape (grep for `memory-search` to be sure) and update them.

- [ ] **Step 5.7: Smoke-test the skill update**

Run: `grep -n "memory-search" ~/Dropbox/github/mac_setup/memory-tools/skill/memory/skill.md`
Expected: every `memory-search --json` reference is consistent with the new object shape. No mention of a top-level array.

- [ ] **Step 5.8: Commit Phase 2 part B — extended memory-search + skill update**

```bash
cd ~/Dropbox/github/mac_setup
git add memory-tools/src/memory_tools/cli_search.py \
        memory-tools/tests/test_cli.py \
        memory-tools/skill/memory/skill.md
git commit -m "$(cat <<'EOF'
feat(memory-tools): extend memory-search with sessions + layered output

memory-search now fans out across curated memory and session transcripts
via search_all. New flags: --source memory|sessions|all (default all),
--promote-candidates (annotate when sessions hit but memory misses).
Text output renders two visually separated tiers so memory's curated
signal is not diluted by transcript noise.

JSON output shape is now an object {memory: [...], sessions: [...],
is_promotion_candidate: bool}, a breaking change from the prior flat
array. The memory skill (skill/memory/skill.md) is updated in this same
commit so every Claude session pulling the new CLI also pulls a skill
that understands the new shape — atomic to prevent silent skill drift.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Phase 3: Hook + phase 3 backfill

### Task 6: Extend SessionEnd hook to reindex sessions

**Files:**
- Read: `memory-tools/hooks/sessionend-snippet.json` (existing)
- Modify: `memory-tools/hooks/sessionend-snippet.json`

- [ ] **Step 6.1: Read the existing hook snippet**

Run: `cat ~/Dropbox/github/mac_setup/memory-tools/hooks/sessionend-snippet.json`
Expected: a JSON object describing one or more SessionEnd handlers. Note the exact structure — the next step preserves it.

- [ ] **Step 6.2: Add a second SessionEnd handler invoking claude-session-index**

The existing snippet is merged into `~/.claude/settings.json` by `phase3_memory.sh`. We need to add a second command. The exact edit depends on the file's current shape — if it lists a `commands` array, append:

```json
{
  "command": "claude-session-index --incremental",
  "timeout_seconds": 120,
  "run_on_error": false
}
```

If it lists a single command, convert to an array of two. Preserve all existing fields. Confirm the edit matches the merge logic in `phase3_memory.sh`.

- [ ] **Step 6.3: Smoke-test by re-running phase 3 in dry-run**

Run: `cd ~/Dropbox/github/mac_setup && ./setup.sh --profile personal --phase 3 --dry-run`
Expected: dry-run output lists both reindex commands as the SessionEnd payload, no errors

- [ ] **Step 6.4: Commit Phase 3 part A — hook**

```bash
cd ~/Dropbox/github/mac_setup
git add memory-tools/hooks/sessionend-snippet.json
git commit -m "$(cat <<'EOF'
feat(memory-tools): SessionEnd hook now also reindexes session transcripts

Adds claude-session-index --incremental alongside the existing memory
reindex so every Claude Code session-exit refreshes the transcript
search DB without a separate cron.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: Phase 3 install script — initial backfill

**Files:**
- Modify: `mac_setup/scripts/phase3_memory.sh`

- [ ] **Step 7.1: Read the existing phase script**

Run: `cat ~/Dropbox/github/mac_setup/scripts/phase3_memory.sh`
Note: where the install runs `memory-index rebuild` for the curated-memory initial build — the backfill goes immediately after.

- [ ] **Step 7.2: Add Dropbox exclusion verification BEFORE backfill**

The session index contains every prompt and assistant response (including pasted secrets, paths, env values). It MUST stay out of Dropbox sync. Before running the backfill, verify the index directory is excluded.

On macOS Dropbox uses a per-file extended attribute `com.dropbox.ignored` to mark items as "do not sync." Insert this check before the backfill step (preserve surrounding logging/dry-run gates):

```bash
# Verify the sessions DB directory is excluded from Dropbox sync.
# Without this, indexed transcripts (which include pasted secrets, file
# paths, env values) leak to Dropbox the moment they are written.
SESSIONS_INDEX_DIR="${MEMORY_HOME:-$HOME/Dropbox/memory}/.index"
mkdir -p "$SESSIONS_INDEX_DIR"
if [ "${DRY_RUN:-0}" -eq 1 ]; then
    log "[dry-run] would verify $SESSIONS_INDEX_DIR is Dropbox-excluded"
else
    # Mark the directory ignored if not already. Idempotent.
    if ! xattr -p com.dropbox.ignored "$SESSIONS_INDEX_DIR" >/dev/null 2>&1; then
        log "Marking $SESSIONS_INDEX_DIR as Dropbox-excluded (com.dropbox.ignored xattr)…"
        xattr -w com.dropbox.ignored 1 "$SESSIONS_INDEX_DIR"
    fi
    # Confirm the attribute stuck (Dropbox sometimes strips xattrs on first sync)
    if ! xattr -p com.dropbox.ignored "$SESSIONS_INDEX_DIR" >/dev/null 2>&1; then
        log "ERROR: failed to mark $SESSIONS_INDEX_DIR as Dropbox-excluded."
        log "       Session search would leak transcript content to Dropbox."
        log "       Aborting session-search install. Re-run after fixing the exclusion."
        exit 3
    fi
    log "Verified Dropbox exclusion on $SESSIONS_INDEX_DIR"
fi
```

- [ ] **Step 7.3: Add the backfill step**

Insert after the exclusion check (preserve surrounding logging/dry-run gates):

```bash
# Initial session-search backfill. Idempotent — re-runs hit the mtime-skip
# fast path. May take 5–15 minutes on first install depending on how much
# Claude Code transcript history exists. --progress (default on) prints a
# counter so the user knows it is alive.
if [ "${DRY_RUN:-0}" -eq 1 ]; then
    log "[dry-run] would run: claude-session-index --full"
else
    log "Running initial session-search backfill (claude-session-index --full)…"
    if ! claude-session-index --full; then
        log "WARNING: claude-session-index --full exited non-zero — sessions index may be partial"
    fi
fi
```

(`log` and `DRY_RUN` are conventions used elsewhere in the phase scripts; if the local conventions differ, match them.)

- [ ] **Step 7.4: Smoke-test phase 3 in dry-run**

Run: `cd ~/Dropbox/github/mac_setup && ./setup.sh --profile personal --phase 3 --dry-run`
Expected: dry-run lists both the Dropbox-exclusion check and the backfill step, exits 0

- [ ] **Step 7.5: Actually run phase 3 against the current machine**

Run: `cd ~/Dropbox/github/mac_setup && ./setup.sh --profile personal --phase 3`
Expected: exclusion check passes (sets xattr if needed), real backfill runs, `~/Dropbox/memory/.index/sessions.db` is created and populated. Cross-check:

```bash
xattr -p com.dropbox.ignored "$HOME/Dropbox/memory/.index"   # should print "1"
memory-search "any keyword you know was in a recent session" --source sessions
```

- [ ] **Step 7.6: Commit Phase 3 part B — backfill + Dropbox guard**

```bash
cd ~/Dropbox/github/mac_setup
git add scripts/phase3_memory.sh
git commit -m "$(cat <<'EOF'
feat(mac_setup): phase 3 backfills session-search + guards Dropbox sync

After the curated-memory reindex, phase 3 sets the
com.dropbox.ignored xattr on $MEMORY_HOME/.index/ and verifies the
exclusion stuck — sessions.db contains every prompt + assistant turn
(including pasted secrets, paths, env values) and must not leak to
Dropbox. Aborts the phase if the exclusion cannot be verified.

Then runs claude-session-index --full to populate the new sessions
DB from the local ~/.claude/projects transcripts. Mtime-skip keeps
re-runs cheap; --progress shows a counter through the 5-15 min
initial backfill.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## After the three phases land

- [ ] **Push.** `git push origin main` (with explicit user approval).
- [ ] **Deploy to work Mac.** `cd ~/mac_setup && git pull && ./setup.sh --profile work --phase 3` — work Mac's session DB is populated from its own `~/.claude/projects`, independent of personal.
- [ ] **Update `memory-tools/SKILL.md` (if present).** Add a one-line note that `memory-search` now searches sessions too. Defer to a follow-up commit; not in this plan's scope.

---

## Self-review

### Spec coverage

- ✅ JSONL ingest from `~/.claude/projects/*/*.jsonl` — Task 2
- ✅ Sibling `sessions.db` with FTS5 + vec0 (UPDATE trigger included, distinct fallback table name) — Task 1
- ✅ `claude-session-index` CLI (full + incremental + `--progress`) — Task 3
- ✅ Unified `memory-search` with `--source memory|sessions|all` — Task 5
- ✅ Layered presentation (FROM MEMORY / FROM SESSIONS tiers) — Task 5
- ✅ `--promote-candidates` workflow — Task 4 + Task 5
- ✅ SessionEnd hook reindex — Task 6
- ✅ Phase 3 initial backfill + Dropbox-exclusion guard — Task 7
- ✅ Per-machine sessions DB (not synced) — install sets `com.dropbox.ignored` xattr on `$MEMORY_HOME/.index/` and aborts if verification fails
- ✅ Memory skill (`skill/memory/skill.md`) updated atomically in the Phase 2 commit so existing skill consumers don't break on the new JSON shape — Task 5
- ✅ Incremental SessionEnd hook is O(changed files), not O(all messages ever), via `files_seen` mtime tracking — Task 1 + Task 2
- ✅ BM25 scores are positive and higher-is-better, safe to merge with vec similarities — Task 1
- ✅ Backfill is batched-commit-per-file, not commit-per-row — Task 2
- ✅ `IngestStats` distinguishes "parse error" from "no indexable text" so users see the real v1 framing: human-typed + assistant-text turns only
- ⚠️ **Embeddings on ingest** — the `--embed` flag on `claude-session-index` is exposed but defaults off. Vector search of sessions works against any embeddings that exist; until a follow-up populates them at ingest time, sessions search is FTS5-only in practice. v1 framing is "FTS5-only sessions search"; embedding-on-ingest is a separate plan.

### Placeholder scan

- No "TBD", "TODO", "implement later" patterns in the plan.
- Every code-bearing step has a code block.
- Every command-bearing step shows the exact command and the expected outcome.

### Type / name consistency

- `SessionStore` named consistently across Tasks 1–7.
- `IngestStats` fields (`files_seen`, `files_ingested`, `messages_inserted`, `messages_skipped_dupe`, `messages_skipped_malformed`) used consistently.
- `CombinedSearchResult` field names (`memory_hits`, `session_hits`, `is_promotion_candidate`) used consistently.
- `search_all(query, embedder, memory_store, session_store, source, memory_top_k, sessions_top_k)` signature used in both Task 4 tests and Task 5 CLI.
- Config additions (`sessions_db_path`, `claude_projects_dir`, `session_search_enabled`) referenced consistently in CLI and tests.

### Known follow-ups not in this plan

- Embedding messages at ingest time (currently scaffolded with `--embed` flag; the actual implementation requires batching + a progress bar — separate plan).
- Pagination / scroll for very-deep session hits (the Hermes `session_search_tool` has ±N anchored scrolling; not in v1).
- Indexing tool-use / tool-result blocks (currently dropped as `messages_skipped_no_text` — would meaningfully expand recall but doubles the secrets-exposure surface; deferred until we agree whether tool output should be searchable).
- Reciprocal-rank fusion for the FTS5+vec merge (currently uses max-score-wins; RRF would be more robust on adversarial queries but is overkill for v1).
