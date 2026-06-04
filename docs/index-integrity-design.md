# Index Integrity — Design

**Status:** Proposed (2026-06-04). Implements the index-subsystem architecture
review (4 `architect-critic` lenses + the #18 debug). Builds on #20
(index → local cache) and #22 (never-silent backstop — a stop-gap, not the cure).

## Problem

rekol's **derived** stores — curated `chunks` + vector blobs (`index.db`),
session FTS5 + vec0 (`sessions.db`), and `INDEX.md` — are derived from the
markdown source-of-truth but lack a consistent **transaction / identity /
observability** discipline. The sessions store partially has it (WAL,
single-transaction ingest, an embedding-dim guard); the curated `IndexStore`
never adopted it. This produces a recurring class of silent-corruption and
concurrency bugs (#18 was the first one caught). The #22 backstop stops *search*
from silently returning nothing, but does not fix the underlying drift.

## Invariants this design enforces

1. **Atomic derivation** — a single file's index state (files-row + chunks +
   vectors) updates all-or-nothing. A crash/kill never leaves a partial state
   that future incremental runs skip.
2. **Identity-checked reads** — the index records which embedding model + schema
   version built it; a mismatch is a loud error, never silent-wrong.
3. **Concurrency-safe** — concurrent reader (search) + the background
   auto-reindex writer never corrupt or block-to-failure; overlapping reindex
   writers are serialized.
4. **Observable** — index health is inspectable (`rekol doctor`); derived
   artifacts (`INDEX.md`) never disagree with the store.

## Changes by cluster

### C1 — Atomic per-file write  ·  Critical (the #18 root)
- `IndexStore`: add `replace_file_and_chunks(path, mtime, content_hash, chunks, *, created, updated, model, dim)` running the `files` UPSERT + chunk DELETE + chunk INSERTs in **one** `with self.conn:` transaction (commit last). Demote `upsert_file` / `replace_chunks_for_file` to `_no_commit` helpers.
- `indexer.py`: rebuild/update/`_index_one` call the single atomic method per file; remove eager per-method commits. `content_hash` is written only after chunks commit → a crash leaves old-hash + old-chunks → retried cleanly. Mirrors `sessions/ingest.py::ingest_file`.

### C2 — Atomic rebuild  ·  High
- `Indexer.rebuild` / `cli_index rebuild`: build into a temp DB in the cache dir, then `os.replace()` over `index.db` atomically. A killed rebuild leaves the old index intact (today it wipes-first → killed rebuild = empty index). Apply the same to `session-index --full` if it wipes.

### C3 — Concurrency  ·  High
- `IndexStore.__init__`: `PRAGMA journal_mode = WAL` + `PRAGMA busy_timeout = 30000` (mirror `SessionStore`). Add `busy_timeout` to `SessionStore` too (it has WAL but no timeout).
- `hooks/auto-reindex.sh`: serialize with `flock` on a lockfile in the cache dir (if locked, exit 0 — the running update covers the change); add a short debounce to coalesce burst edits.
- `hooks/posttooluse-snippet.json`: matcher `Write|Edit|MultiEdit` (covers `MultiEdit`, currently missed → silent drift).

### C4 — Index identity  ·  High (the silent-WRONG fix)
- New `metadata(key TEXT PRIMARY KEY, value TEXT)` table in `index.db`; stamp `embedding_model`, `embedding_dim`, `schema_version` on write/init.
- `IndexStore`: on init / first `search`, compare configured model+dim vs stored → raise a loud `IndexModelMismatchError` (mirror `SessionStoreDimMismatchError`) with a `rekol index rebuild` remedy. Wire into `cli_search` + `cli_index update`. This kills the "change embedding model → mixed-model index → confidently-wrong results" failure (which the #22 emptiness backstop cannot catch).
- Schema versioning: make `needs_schema_migration()` compare `PRAGMA user_version` vs `CURATED_SCHEMA_VERSION` (keep column-presence as a backstop for pre-versioning indexes). Bump the version for this change.

### C5 — Observability + derivation correctness  ·  Medium
- `rekol doctor` (new `cli_doctor.py`, registered in `cli.py`): report schema version, chunk-count vs file-count, session embedding coverage (`count_messages` vs `count_embeddings`), FTS desync (`fts_index_is_stale`), model-identity match, last-rebuild time, cache location. Exit 0 healthy / 1 degraded with remediation.
- `INDEX.md`: derive `_write_index_md` from `store.all_files()` (the just-built DB), **not** a second filesystem walk → no TOCTOU drift, no double I/O. Add per-file tags/aliases to `all_files()` if needed.
- FTS desync **at build time**: keep the session FTS5 external-content in sync on write (triggers or explicit rebuild), so the #22 read-time backstop becomes belt-and-suspenders rather than the only guard.

## Migration

The index is disposable/rebuildable. On the first run after this lands: if
`metadata` is absent or `user_version` < the new version, read/update paths
instruct or trigger a **rebuild** (never silently use a mismatched index). #20
already established "rebuild when the schema is outdated"; extend it. No data
loss — markdown is the source of truth; only the derived cache is rebuilt.

## Test plan

- **C1:** assert a partial write rolls back (the atomic method makes
  "files-row with no chunks" impossible); a crash-injection test that the
  hash is not advanced unless chunks landed.
- **C2:** interrupt a rebuild (temp file present, swap not done) → old index intact + queryable.
- **C3:** WAL pragma set on `index.db`; a concurrent reader+writer don't error; `flock` serializes two hook invocations into one run.
- **C4:** change `embedding_model` → search/update raises the loud error (not wrong results); a `user_version` bump → `needs_schema_migration()` true.
- **C5:** `rekol doctor` reports healthy vs degraded correctly; `INDEX.md` matches store contents; session FTS stays in sync after an incremental run.
- **Gate:** ruff/format/mypy/pytest + bats (install/uninstall still green) + the existing #22 never-silent tests still pass.

## Non-goals

- **MCP adapter** — off the roadmap (per Business, 2026-06-04). But keep the
  Python API entry points (`search_all`, the stores) robust and identity-checked
  so the future **per-LLM integrations** (Claude Code today, ChatGPT/Codex next,
  via hooks + markdown + skill) inherit the guards — the robustness must not be
  CLI-only.

## Sequencing

Built on `main` (post #20/#22). Keep **C1 (atomic write) + C4 (identity)**
together (they're the data-correctness core). C5's `doctor` can split out if the
PR grows too large. Multi-agent adversarial review before the PR; PR is the
review gate (Leon merges).
