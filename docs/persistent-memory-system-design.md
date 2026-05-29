# Persistent Memory System — Design

> **Living document.** Captures the current intent and behavior of the memory-tools subsystem. Update in place as the system evolves; record dated entries in [Revision history](#revision-history) at the bottom.

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
- CLI surface (`memory-*`, plus session indexing/search folded into `memory-search`)
- Claude Code hooks that drive auto-injection, auto-reindex, and proactive capture
- Capture and autonomy policy
- Install model (deploys via `mac_setup` phase 3)

Out of scope: cross-machine memory sharing (per-machine memory plus an optional shared Dropbox folder, decided separately); ADR-style immutable decision records (this is a living spec, not an ADR — see [`when-writing-specs`](../../../memory/when/when-writing-specs.md)).

## Data model

### Storage root

All memory lives under `$MEMORY_HOME`, a Dropbox-synced directory. Default: `~/Dropbox/memory/`.

Index files (SQLite + vec0) live under `$MEMORY_HOME/.index/` and are explicitly **not** synced — they are disposable and per-machine. The markdown is the source of truth; the index is rebuildable from it at any time.

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

SQLite with the `sqlite-vec` extension (`vec0` virtual table) for vector storage and ANN search. Single-file database at `$MEMORY_HOME/.index/memory.db`. WAL mode for safe concurrent reads.

### Embedding model

Local embedding (no API calls). Default: a sentence-transformers model installed via `pyproject.toml` extras. Embedding happens at index-build time; queries embed once per call.

### Chunking

Files are chunked by Markdown heading. Each chunk gets a vector plus metadata: `file_path`, `heading`, `line_start`, `line_end`, frontmatter `tags`, `aliases`. This makes `memory-search` results citable to a specific heading and line range, so the caller can read the precise chunk instead of the whole file.

### Lifecycle

- `memory-index rebuild` — full rebuild from disk
- `memory-index update` — incremental, hashes-per-file change detection
- Auto-run by the PostToolUse hook after Edit/Write on any `$MEMORY_HOME` file
- Auto-run by the SessionEnd hook as a safety net

The index is disposable. Blowing it away and rebuilding takes seconds to a minute depending on corpus size. Never check it into git.

## Session search layer

### Motivation

Curated memory is small, durable, and authoritative. Conversation transcripts are the *raw* layer underneath — every thing Claude ever figured out, including the things never promoted into curated memory. Without an index over transcripts, the agent loses recall of recent problem-solving the moment a session ends.

Session search adds a parallel layer of recall over `~/.claude/projects/*/*.jsonl` (Claude Code's per-project conversation transcripts) without changing how curated memory works.

### Storage

Sibling SQLite database at `$MEMORY_HOME/.index/sessions.db`. Separate from the memory index because:
- Session content is per-machine and never synced (confidentiality between work and personal)
- Schema lifecycles are independent — rebuilding sessions doesn't risk the curated index
- Sessions data dwarfs memory data by 2–3 orders of magnitude; keeping them separate makes the memory index cheap to rebuild

### Hybrid retrieval

Both FTS5 (keyword) and vec0 (semantic) tables, populated together. Queries fan out to both and merge results. FTS5 wins on exact-string queries (file paths, env names); vec0 wins on concept queries ("rate limiting", "auth flow"). Combining keeps the agent from losing recall under either phrasing pattern.

### Ingest

`claude-session-index --incremental` reads `~/.claude/projects/*/*.jsonl`, normalizes each message (role, content, tool calls, timestamp), dedupes against the existing index by `(session_id, message_id)`, and inserts new rows. Run by the SessionEnd hook after each Claude Code session terminates. Cost: ~0.1–2 sec per session-end.

Initial backfill happens during phase 3 install. Estimated 5–15 min depending on transcript history depth. No `--skip-backfill` flag — the common path is the correct path.

### Search surface — unified, layered presentation

`memory-search "query"` searches **both** curated memory and session transcripts in one call, but presents the results in two visually separated tiers:

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

When a query returns **zero memory hits but multiple session hits**, that is a promotion signal: the concept has come up repeatedly in conversations but lives nowhere durable. `memory-search --promote-candidates` enumerates topics that match this pattern (high session-hit count, no memory hit), giving the user a queue of things worth capturing with `memory-capture`.

This is the only new workflow the session layer unlocks beyond improved recall.

## CLI surface

| Command | Purpose |
|---|---|
| `memory-search "query" [--top N] [--source memory\|sessions\|all] [--json] [--promote-candidates]` | Hybrid search over memory + sessions. Layered output. |
| `memory-index rebuild \| update` | Manage the curated-memory vector index |
| `memory-capture --layer L --file F --name N --description D [--tags ...] [--aliases ...]` | Interactive capture flow (stdin = body) |
| `memory-invalidate` | Mark entries stale without deletion |
| `memory-propose` | Suggest captures based on observed conversation patterns |
| `claude-session-index [--incremental \| --rebuild]` | Manage the session-transcript index |

All CLIs install to `$MEMORY_HOME/../bin` (added to `$PATH` by `mac_setup`).

## Hooks

Registered in `~/.claude/settings.json` by `phase3_memory.sh`. Snippets live at `memory-tools/hooks/*.json`.

| Hook | Event | Purpose |
|---|---|---|
| `auto-reindex.sh` | PostToolUse (filter on Edit/Write to `$MEMORY_HOME/**`) | Incremental reindex after manual memory edits |
| `sessionstart-snippet.json` | SessionStart | Re-inject `MEMORY.md` into the session context |
| `sessionend-snippet.json` | SessionEnd | Reindex memory (safety net) + run `claude-session-index --incremental` |
| `posttooluse-snippet.json` | PostToolUse | Wraps `auto-reindex.sh` for the harness |

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
1. Creates `$MEMORY_HOME` if absent (default `~/Dropbox/memory`)
2. Installs the Python package (`pip install -e .` against `memory-tools/`)
3. Installs CLIs into the user's `$PATH`
4. Merges the three hook snippets into `~/.claude/settings.json` (with timestamped backup)
5. Runs initial vector index build over existing memory markdown
6. Runs initial session-search backfill over `~/.claude/projects/*/*.jsonl`
7. Writes an install journal to `$MEMORY_HOME/.install-journal-<timestamp>.log`

The install is **idempotent**. Existing memory files are never modified. `~/.claude/settings.json` is backed up before any edit.

## Per-machine vs synced state

| Artifact | Synced? | Why |
|---|---|---|
| `$MEMORY_HOME/{always,when,topics,knowledge}/*.md` | Yes (Dropbox) | Source of truth, hand-editable, shared across machines |
| `$MEMORY_HOME/.index/memory.db` | No | Disposable, per-machine, rebuildable |
| `$MEMORY_HOME/.index/sessions.db` | No | Per-machine confidentiality (work transcripts stay on work Mac) |
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
