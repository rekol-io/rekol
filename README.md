# memory-tools

Layered, cross-indexed memory system with local vector search, for use with Claude Code.

See the design spec at [`docs/persistent-memory-system-design.md`](./docs/persistent-memory-system-design.md) for the full rationale.

## Install

This component is installed by `mac_setup`'s phase-3 script:

```
cd ~/mac_setup
./setup.sh --profile work --phase 3       # or --profile personal
```

## Data model

Memory files live under `$MEMORY_HOME` in four layers:

- `always/` — permanent facts (identity, PRD codes, envs). Small (<8 KB total), always loaded.
- `when/` — task-triggered rules (`when-touching-repos.md`, etc.).
- `topics/` — canonical-source registry (`prometheus.md`, etc.).
- `knowledge/` — long-form durable lessons.

Each file has YAML frontmatter with `name`, `description`, `type`, `tags`, `aliases`, `see_also`, `created`, `updated`.

## CLIs

- `memory-index rebuild | update` — rebuild or incrementally update the vector index.
- `memory-search "query" [--top N] [--json]` — semantic search over memory.
- `memory-capture` — interactive capture of a new memory (layer, file, frontmatter, reindex).

## Data vs. code

Files in `$MEMORY_HOME` are the source of truth. `index.db` is a disposable, rebuildable SQLite+vec0 index. The markdown syncs via Dropbox; the index does not.
