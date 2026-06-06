# Durable transcript archiving — design (#8)

**Status:** approved design, 2026-06-05 · branch `feat/transcript-archiving` · implements [#8](https://github.com/rekol-io/rekol/issues/8)
**Scope:** v1 = core durability + archive-side exclude slice. Index purge, export, and edit/delete-indexed-data are explicitly **deferred** (see Deferred work).

## Problem

rekol indexes Claude Code session transcripts (`~/.claude/projects/**/*.jsonl`) into
`sessions.db`, but keeps **no durable, rekol-owned copy** of them. Transcript text
exists in exactly two places, and rekol reliably owns neither:

| Copy | Owner | Durable? |
|---|---|---|
| `~/.claude/projects/**/*.jsonl` | Claude Code | No — CC may rotate/clean/relocate these at will. |
| `sessions.db` | rekol | No — local cache, **disposable by design**; a rebuild repopulates *only* from `.jsonl` files still on disk. |

**Failure mode:** Claude Code cleans up an old `.jsonl` → those conversations live only
in `sessions.db` → the next index rebuild (`rm sessions.db && rekol session-index --full`)
**loses them permanently**, because there is no third copy to rebuild from. This is a
data-loss path on the core "remembers what you did" capability.

## Goal

Insert a durable, rekol-owned archive *between* the ephemeral source and the disposable
index, and make the index **always rebuild from the archive**. Live `.jsonl` becomes
nothing more than a *source feeding the archive*.

```
~/.claude/projects/*.jsonl  ──(1) archive-sync──▶  ~/.local/share/rekol/archive/  ──(2) ingest──▶  sessions.db
   (Claude Code, ephemeral)     copy-if-changed        (rekol-owned, durable)        existing path     (cache)
```

Even if Claude Code deletes the originals and the cache is wiped, the archive survives and
the index can always be rebuilt losslessly from it.

## Non-goals (v1)

- **Index purge** — destructive removal of already-indexed content from the live
  `sessions.db`/`index.db` (FTS5 + vec stores). Deferred to its own issue. The index is
  disposable; exclude works forward + via rebuild, so a destructive purge is not required
  to honor an exclude.
- **Edit/delete individual indexed session data** — deferred (TBD).
- **Export** (`rekol export`, incl. markdown rendering) — deferred; ships as the **first**
  fast-follow (it's the "no lock-in / it's yours" proof).
- **Auto-compaction / retention policy** — deferred fast-follow; v1 keeps everything and
  offers only a *manual* prune.

## The four-location model

| Location | Trait | Holds |
|---|---|---|
| `$REKOL_HOME` (e.g. `~/Dropbox/memory`) | synced · user-authored · durable | hand-written markdown memory (curated) |
| `${XDG_CACHE_HOME:-~/.cache}/rekol/<sha256(home)[:16]>/` | local · disposable · rebuildable | `index.db`, `sessions.db`, `INDEX.md` |
| `~/.claude/projects/` | Claude-Code-owned · ephemeral | source `*.jsonl` transcripts |
| **`${XDG_DATA_HOME:-~/.local/share}/rekol/archive/`** | **local · durable · NOT synced · NOT a cache** | **NEW: the durable, rekol-owned transcript copy** |

The archive sits next to the venv under `~/.local/share/rekol/` on every machine. It is
**machine-level, not hashed per `$REKOL_HOME`** — transcripts come from `~/.claude/projects`
regardless of which memory home is active, so one archive per machine is correct (splitting
per-home would only duplicate).

### Archive layout

```
<archive>/<project-slug>/<session-id>.jsonl          # 1:1 mirror of ~/.claude/projects
<archive>/<project-slug>/<session-id>.<shorthash>.jsonl   # divergence sidecar (see below)
<archive>/.manifest.json                              # live-relpath → (mtime,size) last archived
<archive>/.backfilled-from-index                      # one-time backfill guard marker
```

## Configuration & resolution

New config keys (`config.py` `DEFAULTS` + `Config` + `load_config`):

| Key | Default | Meaning |
|---|---|---|
| `archive_enabled` | `true` | Master on/off for the archive sink. The off-switch. |
| `archive_dir` | `null` | Explicit archive path; `null` → resolve default. |
| `exclude_paths` | `[]` | Glob list of project/cwd paths never archived/indexed (forward). |

**`resolve_archive_dir()`** (mirrors `resolve_index_dir`):
1. `REKOL_ARCHIVE_DIR` env (verbatim, expanded)
2. `archive_dir` config key
3. `${XDG_DATA_HOME:-~/.local/share}/rekol/archive`

`Config.archive_dir` property returns the resolved path. Per-folder `.rekolignore`
(gitignore-style) is honored in addition to `exclude_paths`.

## Components

| Unit | Responsibility | Depends on |
|---|---|---|
| `config.resolve_archive_dir()` + `archive_dir` property + new keys | path + flag resolution | env, config |
| `config.path_is_excluded(path, patterns)` + `.rekolignore` discovery | the exclude matcher (shared foundation; full #5 reuses it later) | stdlib `fnmatch`/`pathlib` |
| `sessions/archive.py` | the archive sink — **DB-free**, pure file ops + JSON manifest | filesystem only |
| `cli_archive.py` → `rekol archive` | manual sync; `--from-index` backfill; `--prune`/`--clear` manual retention | archive.py, config |
| `cli_session_index.py` (edit) | archive-sync first, then ingest **from the archive**; soft-fail → live | archive.py |
| `cli_doctor.py` (edit) | archive health line | archive.py |
| `install.sh` / `uninstall.sh` (edit) | `--no-archive`, `--archive-dir`, disclosure, `--help`; uninstall prompt-before-delete | — |

### `sessions/archive.py` API (sketch)

- `archive_directory(live_root, archive_dir, exclude_patterns) -> ArchiveStats`
  — reconcile the archive to *(live ∩ not-excluded)*: copy new/changed non-excluded files,
  remove already-archived files that now match an exclude, skip unchanged via manifest.
- `archive_file(live_path, archive_path, manifest) -> ArchiveFileResult`
  — the copy-if-changed primitive + divergence guard.
- `backfill_from_index(store, archive_dir, exclude_patterns) -> BackfillStats`
  — reconstruct minimal `.jsonl` for sessions present in `sessions.db` but absent from the
  archive (exclude-aware).
- `prune(archive_dir, ...) -> PruneStats` — manual clear/trim of the archive (flat-file rm).

## Data flow

### SessionEnd hook (`rekol session-index --incremental`, command unchanged)
1. **Archive-sync** — `archive_directory(claude_projects_dir, archive_dir, excludes)`.
   The just-ended session is copied here *before* step 2 reads it.
2. **Ingest** — `ingest_directory(archive_dir, …)` (existing code, repointed at the archive).
   Message-level `UNIQUE(session_id, message_uuid)` dedupe makes the repoint duplicate-free.

If `archive_enabled` is false, skip step 1 and ingest from live (today's behavior).

### Rebuild (`rekol session-index --full` / `rekol index rebuild`)
Archive-sync first (catch anything new), then ingest **all** archive files. Lossless even if
Claude Code deleted the live originals — the payoff.

### Backfill on upgrade
On the first archive operation after upgrade, if `archive/.backfilled-from-index` is absent:
reconstruct archive entries for sessions in `sessions.db` but missing from the archive
(exclude-aware, text-only/lossy — the DB never stored tool_use/thinking rows; a lossy copy
beats a lost session), write the marker, and **emit one non-blocking notice line**
(*"rekol built a local archive of your past sessions so they're not lost — here's where,
here's the off-switch"*). Also runnable explicitly via `rekol archive --from-index`.

## The copy-if-changed primitive (`archive_file`)

For each live file vs its archived counterpart:

- **No archived copy** → copy it.
- **Unchanged** (live mtime+size == manifest) → skip (steady-state cheap path).
- **Live grew AND archived content is a true prefix of live** (normal append) → replace,
  update manifest.
- **Live shorter OR diverged** (archived is *not* a prefix — the compaction/rewrite
  signature) → **do not overwrite.** Keep the existing archive, write the new version beside
  it as `<session-id>.<shorthash>.jsonl` (divergence sidecar). Both get ingested; DB-level
  uuid dedupe folds them. Counted + logged, never silently lost.

This keeps the simple "whole-file copy, human-navigable" model while turning the one
data-loss case (compaction/rewrite) into "we kept both copies."

## Exclude (archive-side slice of #5)

Organizing principle — **deletion difficulty**:

> Easy-to-delete (our archive = flat files) → exclude cleans it up, even retroactively.
> Hard/risky-to-delete (the index = FTS5 + vec) → forward-looking only; purge is a separate
> deferred feature.

- **Forward:** archive-sync and backfill skip any path matching `exclude_paths` /
  `.rekolignore`. Excluded sessions are never copied/reconstructed.
- **Retroactive (archive only):** archive-sync `rm`s any already-archived file that now
  matches an exclude — a safe flat-file delete in rekol's own folder.
- **Index follows for free:** because the index rebuilds *from* the archive, excluded
  content drops out of `sessions.db` on the next full rebuild — no destructive DB surgery.
- **Not in scope:** immediate purge of already-indexed excluded content from the live
  `sessions.db`/`index.db`. That is the deferred purge feature.

This is the reusable foundation (config + matcher) plus one consumer (archive-sync). Full
#5 later adds the other consumers (curated indexer) + the purge; **no rework** of this slice.

## Error handling, soft-fail, locking

- **Archiving never blocks indexing.** If archive-sync raises `OSError` (disk full, dir
  unwritable), log a warning and **fall back to ingesting from live** for that run (degrades
  to today's behavior). The next successful run catches up (copy-if-changed is idempotent).
  No bare `except`.
- **Hook soft-fail.** Consistent with the other hook subcommands: a broken archive step
  exits 0 and never stalls a session.
- **Locking.** `session-index` writes `sessions.db`, a **separate** database from the curated
  `index.db`. The existing `index_write_lock` (#24/#25) guards the *curated* index's
  rebuild↔update — it exists specifically because the curated rebuild does an atomic temp-DB
  swap — and **must NOT be reused here**: coupling the two would let a curated rebuild block
  transcript indexing (and vice versa) for no correctness benefit. `sessions.db` concurrency
  is already handled at the SQLite layer (WAL + 30s `busy_timeout`), and archive-sync only
  writes flat files (idempotent reconcile; the bash `auto-reindex.sh` mutex already coalesces
  hook bursts). If two concurrent `session-index` runs ever need serializing, add a
  **dedicated** `.session-index.lock` — never the curated lock — but that is YAGNI for v1.
  Residual race — a `--full` rebuild reading the archive while a SessionEnd writes a new file
  — merely defers the newest session to the next incremental run. Acceptable; documented.

## Install / uninstall / docs

- `install.sh`: add `--no-archive` (seeds `archive_enabled:false`) and `--archive-dir P`
  (mirrors the `--tools-home` precedent), a `--help` usage block, and a one-line disclosure
  in install output. After the existing `session-index --full` backfill, run
  `rekol archive --from-index`.
- **Disclosure** (default-ON honesty): one plain line at install + a README line. No
  per-session SessionStart nag (keeps the pull-only / non-intrusive brand stance). Example:
  *"rekol keeps a local copy of your sessions so your memory survives — it's on your disk,
  never uploaded. Turn it off anytime: `<command>`."*
- **README:** add an "Install options" section listing every flag, including the archive
  flags + the disclosure.
- `uninstall.sh`: treat the archive **like the index cache** — preserve it when removing the
  tools-home; only delete on an interactive prompt or explicit `--purge-archive`
  (`--yes` keeps it). **Markdown memory is never touched.** Rationale: verbatim transcripts
  argue for cleanup, but "it's yours / no lock-in" + no export in v1 means deletion is
  irreversible loss — so prompt, don't silently nuke.
- `doctor`: archive health line — dir present/writable, N sessions archived, last-archive
  time; **warn if `archive_dir` resolves to a cloud/placeholder mount** (Dropbox/GDrive/iCloud
  heuristics), since on-demand/streaming sync can dehydrate files and a synced archive puts
  verbatim secrets in the cloud.

## Security

- Default archive location is **local, non-synced, non-cache** — preserves the posture that
  moved `sessions.db` out of `$REKOL_HOME` (#10/#13).
- Relocating to a synced dir is a documented opt-in with a **loud secrets warning** + the
  `doctor` placeholder-mount warning.
- The archive contains verbatim prompts (and any pasted secrets); the exclude slice is the
  day-one control so sensitive projects are never archived.

## Testing

- **Unit (`archive.py`)** — copy-if-changed matrix (new / unchanged-skip / grown-prefix-
  replace / shorter→sidecar / diverged→sidecar); `archive_directory` reconcile (copy new,
  skip unchanged, rm now-excluded); `path_is_excluded` + `.rekolignore`; `backfill_from_index`
  round-trips through `iter_messages_in_file` and is exclude-aware; `resolve_archive_dir`
  precedence (env > config > default).
- **Integration — headline regression:** archive a session → **delete the live `.jsonl`** →
  `session-index --full` → assert the session is still searchable. (Regression test for the
  data-loss bug itself.)
- **Integration:** soft-fail — unwritable archive dir → still ingests from live, exit 0.
- **Integration:** exclude — excluded project never archived; an already-archived file that
  becomes excluded is removed on next sync; backfill skips excluded.
- **Integration:** backfill auto-once — marker guards re-run; the notice line is emitted once.
- **`doctor`** archive line; **bats** `install.sh --no-archive` / `--archive-dir`; **bats**
  `uninstall.sh` preserves the archive by default and removes it with `--purge-archive`.
- Hermetic per the test-gate discipline (conftest clears `REKOL_HOME`/`MEMORY_HOME`;
  archive dir → tmp). CI gate: ruff · ruff format · mypy · pytest · bats install.

## Deferred work (separate issues, draft)

1. **Index purge** — destructive removal of already-indexed content from `sessions.db` /
   `index.db` (FTS5 + vec), for excluded paths or arbitrary selections. The risky part;
   intentionally out of the data-loss-fix PR.
2. **Export** (`rekol export`, jsonl + markdown) — portability / "no lock-in" proof. First
   fast-follow.
3. **Edit/delete individual indexed session data** — TBD.
4. **Auto-compaction / retention** — once real archive sizes are observed.

## References

- Issue [#8](https://github.com/rekol-io/rekol/issues/8) (this), [#5](https://github.com/rekol-io/rekol/issues/5) (exclude — full version), #10/#13 (index→local-cache secrets posture).
- Business strategy read: coordination channel `from-business/20260605-0009-strategy-read-8-archiving.md`
  (disclosure + control; archive-honors-excludes as the hard ship gate).
- Concurrency/lock: PRs #23 (index integrity), #25/#24 (rebuild↔update lock).
