# Persistent Memory System — Design

> **Living document.** Captures the current intent and behavior of the REKOL memory subsystem. Update in place as the system evolves; record dated entries in [Revision history](#revision-history) at the bottom.

## Purpose

A persistent, file-based memory system for Claude Code that survives across sessions and machines. Two design objectives:

1. **Curated, hand-editable, durable.** Memory is Markdown on disk, syncs via Dropbox, and is readable without any tool. The agent reads and writes the same files a human reads and writes.
2. **Searchable beyond keyword.** A local vector index over the same files provides semantic recall, so the agent finds the right rule even when the user's phrasing differs from the file's wording.

The system explicitly avoids opaque memory stores, third-party SaaS dependencies, and per-application memory silos.

## Scope

Covers:
- The four-layer memory data model (`always/`, `when/`, `topics/`, `knowledge/`)
- Frontmatter schema
- Vector index over memory markdown (sqlite-vec0)
- Session-search layer over Claude Code conversation transcripts (hybrid FTS5 + vec0)
- CLI surface (the unified `rekol` command, with session indexing/search folded into `rekol search`)
- Claude Code hooks that drive auto-injection, auto-reindex, and proactive capture
- Capture and autonomy policy
- Install model (deploys via `mac_setup` phase 3)

Out of scope: cross-machine memory sharing (per-machine memory plus an optional shared Dropbox folder, decided separately); ADR-style immutable decision records (this is a living spec, not an ADR — see [`when-writing-specs`](../../../memory/when/when-writing-specs.md)).

## Data model

### Storage root

All memory lives under `$REKOL_HOME` (`$MEMORY_HOME` is accepted as a fallback), a Dropbox-synced directory. Default: `~/Dropbox/memory/`.

Index files (SQLite + vec0) live under `$REKOL_HOME/.index/` and are explicitly **not** synced — they are disposable and per-machine. The markdown is the source of truth; the index is rebuildable from it at any time.

### Four layers

| Layer | Purpose | Loaded when | Budget |
|---|---|---|---|
| `always/*.md` | Permanent foundational facts (identity, env names, PRD codes) | Every session (auto-injected via SessionStart hook) | Hard cap 8 KB total |
| `when/when-<activity>.md` | Task-triggered rules (`when-touching-repos.md`, `when-writing-specs.md`) | When the activity matches the user's request | None |
| `topics/<noun>.md` | Canonical-source registry per noun (`prometheus.md`, `simone_provider.md`) | When the noun appears in the user's message | None |
| `knowledge/*.md` | Long-form durable reference docs (`infrastructure.md`, `user_leon.md`) | On demand via vector search | None |

The split is by **trigger** (when does Claude need this?), not by topic. A rule for handling spec files lives in `when/`; the canonical facts about Prometheus live in `topics/`; the deep architecture of the homelab lives in `knowledge/`.

### Frontmatter schema

Every memory file begins with YAML frontmatter:

```yaml
---
name: short-kebab-case-slug
description: One-line summary used for relevance matching during search.
type: feedback | project | reference | user
tags: [a, b, c]
aliases: [other phrasings, lookup keywords]
see_also: [other-memory-name]
created: 2026-04-27T15:30:00-04:00
updated: 2026-05-28T15:42:00-04:00
valid_from: 2026-04-27
---
```

`created`/`updated` are auto-stamped ISO-8601 with offset. `valid_from` is date-only and optional (used for facts that have a defined start date).

### Index file

`MEMORY.md` at the root is the always-on entry point. It contains identity, activity triggers (one line per `when/` file), topic noun pointers, and the capture protocol. Re-injected by the SessionStart hook every session. Hard cap on length — long content lives in the layer files, not in `MEMORY.md`.

## Vector index

### Engine

SQLite with the `sqlite-vec` extension (`vec0` virtual table) for vector storage and ANN search. Single-file database at `$REKOL_HOME/.index/memory.db`. WAL mode for safe concurrent reads.

### Embedding model

Local embedding (no API calls). Default: a sentence-transformers model installed via `pyproject.toml` extras. Embedding happens at index-build time; queries embed once per call.

### Chunking

Files are chunked by Markdown heading. Each chunk gets a vector plus metadata: `file_path`, `heading`, `line_start`, `line_end`, frontmatter `tags`, `aliases`. This makes `rekol search` results citable to a specific heading and line range, so the caller can read the precise chunk instead of the whole file.

### Lifecycle

- `rekol index rebuild` — full rebuild from disk
- `rekol index update` — incremental, hashes-per-file change detection
- Auto-run by the PostToolUse hook after Edit/Write on any `$REKOL_HOME` file
- Auto-run by the SessionEnd hook as a safety net

The index is disposable. Blowing it away and rebuilding takes seconds to a minute depending on corpus size. Never check it into git.

## Session search layer

### Motivation

Curated memory is small, durable, and authoritative. Conversation transcripts are the *raw* layer underneath — every thing Claude ever figured out, including the things never promoted into curated memory. Without an index over transcripts, the agent loses recall of recent problem-solving the moment a session ends.

Session search adds a parallel layer of recall over `~/.claude/projects/*/*.jsonl` (Claude Code's per-project conversation transcripts) without changing how curated memory works.

### Storage

Sibling SQLite database at `$REKOL_HOME/.index/sessions.db`. Separate from the memory index because:
- Session content is per-machine and never synced (confidentiality between work and personal)
- Schema lifecycles are independent — rebuilding sessions doesn't risk the curated index
- Sessions data dwarfs memory data by 2–3 orders of magnitude; keeping them separate makes the memory index cheap to rebuild

### Hybrid retrieval

Both FTS5 (keyword) and vec0 (semantic) tables, populated together. Queries fan out to both and merge results. FTS5 wins on exact-string queries (file paths, env names); vec0 wins on concept queries ("rate limiting", "auth flow"). Combining keeps the agent from losing recall under either phrasing pattern.

### Ingest

`rekol session-index --incremental` reads `~/.claude/projects/*/*.jsonl`, normalizes each message (role, content, tool calls, timestamp), dedupes against the existing index by `(session_id, message_uuid)`, and inserts new rows. Embeddings are on by default (`--embed`), so each new message is also written to the vec0 index — making transcript search semantic, not keyword-only — using the same local model as curated-memory search (`embedding_model`). Run by the SessionEnd hook after each Claude Code session terminates. Cost: ~1–4 sec per session-end, dominated by the one-time embedding-model load; inference over a single session's new turns is sub-second. Pass `--no-embed` for a faster FTS5-only ingest.

Initial backfill happens during phase 3 install. Estimated 5–15 min depending on transcript history depth. No `--skip-backfill` flag — the common path is the correct path.

### Search surface — unified, layered presentation

`rekol search "query"` searches **both** curated memory and session transcripts in one call, but presents the results in two visually separated tiers:

```
━━ FROM MEMORY (curated) ━━━━━━━━━━━━━━━━━━━━
  score  file:heading  (line range)
  ...

━━ FROM SESSIONS (last 30 days, top 5) ━━━━━━
  score  date — repo — session title
  [msg id] user/assistant excerpt
  ...
```

Memory hits are listed first because they are curated truth. Session hits are capped (top N, configurable, default 5) so transcript noise cannot drown the curated section. `--source memory|sessions|all` flag toggles which layer is searched; default is `all`.

### Promotion-candidate workflow

When a query returns **zero memory hits but multiple session hits**, that is a promotion signal: the concept has come up repeatedly in conversations but lives nowhere durable. `rekol search --promote-candidates` enumerates topics that match this pattern (high session-hit count, no memory hit), giving the user a queue of things worth capturing with `rekol capture`.

This is the only new workflow the session layer unlocks beyond improved recall.

## CLI surface

A single `rekol` command with subcommands:

| Command | Purpose |
|---|---|
| `rekol search "query" [--top N] [--source memory\|sessions\|all] [--json] [--promote-candidates]` | Hybrid search over memory + sessions. Layered output. |
| `rekol index rebuild \| update` | Manage the curated-memory vector index |
| `rekol capture --layer L --file F --name N --description D [--tags ...] [--aliases ...]` | Interactive capture flow (stdin = body) |
| `rekol invalidate` | Mark entries stale without deletion |
| `rekol propose` | Suggest captures based on observed conversation patterns |
| `rekol session-index [--incremental \| --rebuild]` | Manage the session-transcript index |

The `rekol` command installs to `$REKOL_HOME/../bin` (added to `$PATH` by `mac_setup`).

## Hooks

Registered in `~/.claude/settings.json` by `install.sh`. Snippets live at `rekol/hooks/*.json`.

| Hook | Event | Purpose |
|---|---|---|
| `auto-reindex.sh` | PostToolUse (filter on Edit/Write to `$REKOL_HOME/**`) | Incremental reindex after manual memory edits |
| `sessionstart-snippet.json` | SessionStart | Re-inject `MEMORY.md` into the session context |
| `sessionend-snippet.json` | SessionEnd | Reindex memory (safety net) + run `rekol session-index --incremental` |
| `posttooluse-snippet.json` | PostToolUse | Wraps `auto-reindex.sh` for the harness |
| `userpromptsubmit-snippet.json` | UserPromptSubmit | Inject an `<env-time>` block (local/UTC + elapsed-since-last-user/assistant) via `rekol _hook time-context` |
| `stop-snippet.json` | Stop | Record the assistant-completion timestamp via `rekol _hook record-stop` |

## Capture & autonomy policy

### Autonomous (no permission needed)

The agent captures facts as a side effect of getting work done when they would benefit future sessions:
- User details (role, preferences, knowledge) → `user_*.md`
- Corrections or validated approaches → `when-*.md` or `topics/*.md`
- Project facts, decisions, deadlines → `topics/*.md`
- Canonical external sources → `topics/<noun>.md`

The agent always tells the user *what* was captured in one line so it can be audited without going hunting.

### Autonomous (policy 2026-05-23): stale-fact maintenance

When the agent observes that a memory conflicts with reality (path moved, cluster decommissioned, command renamed), it edits the file immediately. No approval round-trip.

### Still requires approval

- Wholesale deletion of a whole memory file
- Restructuring how memory is organized (renaming directories, changing index format)
- Edits to `always/*.md` (identity, durable foundational context — high blast radius)
- Edits to `knowledge/*.md` (deep reference docs — high blast radius)
- Removing more than ~10 lines in a single edit from any memory file
- Any non-memory file edit (the global code-change protocol still applies)

## Install model

Deploys via `mac_setup` phase 3:

```
cd ~/mac_setup
./setup.sh --profile personal --phase 3       # or --profile work
```

The phase script:
1. Creates `$REKOL_HOME` if absent (default `~/Dropbox/memory`)
2. Installs the Python package (`pip install -e .` against `rekol/`)
3. Installs the `rekol` CLI into the user's `$PATH`
4. Merges the hook snippets (SessionStart, PostToolUse, SessionEnd, UserPromptSubmit, Stop) into `~/.claude/settings.json` (with timestamped backup)
5. Runs initial vector index build over existing memory markdown
6. Runs initial session-search backfill over `~/.claude/projects/*/*.jsonl`
7. Writes an install journal to `$REKOL_HOME/.install-journal-<timestamp>.log`

The install is **idempotent**. Existing memory files are never modified. `~/.claude/settings.json` is backed up before any edit.

## Per-machine vs synced state

| Artifact | Synced? | Why |
|---|---|---|
| `$REKOL_HOME/{always,when,topics,knowledge}/*.md` | Yes (Dropbox) | Source of truth, hand-editable, shared across machines |
| `$REKOL_HOME/.index/memory.db` | No | Disposable, per-machine, rebuildable |
| `$REKOL_HOME/.index/sessions.db` | No | Per-machine confidentiality (work transcripts stay on work Mac) |
| `~/.claude/projects/*/*.jsonl` | No (Claude Code-owned) | Per-machine session log |
| Install journals | No | Per-machine artifact |

If the user later wants cross-machine session search, the resolution is **not** to sync `sessions.db` (would mingle work and personal). Instead, add a separate shared Dropbox folder both machines can read and write, and treat it as a third layer in the search hierarchy.

## Non-goals

- Multi-tenant memory backends (Honcho, mem0, Supermemory, etc.) — single-user system, plugin abstraction is pure overhead
- Real-time bidirectional memory between concurrent sessions — sessions are durable but eventually consistent; cache invalidation is via SessionStart re-injection
- IDE-specific memory layers — Claude Code is the only integration target

## Revision history

- **2026-04-27** — Initial design drafted in `cassandra-team-workspace/docs/superpowers/specs/2026-04-27-persistent-memory-system-design.md`. Four-layer model, vector index, `memory-*` CLI surface, phase 3 install via mac_setup.
- **2026-05-28** — Spec moved out of cassandra-team-workspace (retired) to live with the code in `memory-tools/docs/`. Filename de-dated per [`when-writing-specs`](../../../memory/when/when-writing-specs.md). Added session-search layer: hybrid FTS5+vec0 over `~/.claude/projects/*/*.jsonl`, layered presentation in `memory-search`, sibling `sessions.db`, incremental on SessionEnd, promotion-candidate workflow. Established per-machine vs synced state policy explicitly.
- **2026-05-31** — Closed the gap between this spec and the code: session embeddings are now actually computed on ingest (the `--embed` flag was previously a no-op stub), so transcript search is genuinely hybrid FTS5+vec0 rather than keyword-only, sharing the curated-memory embedding model. `install.sh` now wires the SessionEnd transcript-index hook (Step 7D) and runs the initial session backfill at install (Step 9.5) — both were described here as done but were not yet implemented.
- **2026-05-31** — Temporal grounding (build): curated retrieval is now time-aware (timestamps carried to the index; invalidated excluded by default, `valid_from` respected, layer-aware recency that treats `always/`+`knowledge/` as always-current). REKOL ships its own time hook — `rekol _hook time-context`/`record-stop` (UserPromptSubmit + Stop, install Steps 7E/7F) — replacing the external `mac_setup` time component; install warns and skips if the legacy hook is still present (run the mac_setup uninstall, then re-run `rekol install`). A durable-memory re-confirmation loop (`rekol review` + SessionEnd nudge + inline `[review?]` tag) is specified as Workstream D. See [`temporal-grounding-design.md`](temporal-grounding-design.md).
- **2026-05-30** — Rebranded `memory-tools` → **REKOL**. Python package renamed `memory_tools` → `rekol`; the eight `memory-*` console scripts unified under a single `rekol` command (`rekol search`, `rekol index`, `rekol capture`, `rekol session-index`, `rekol import`, etc.). Data-directory env var is now `REKOL_HOME`, with `MEMORY_HOME` retained as a fallback. Data-level filenames (`MEMORY.md`, `memory.config.yaml`, the `skill/memory/` dir) are held stable for safety and deferred to a later genericization pass.
