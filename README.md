# memory-tools

> Local-first, files-you-own memory for AI assistants. Structured markdown +
> on-device vector search. No cloud, no API key, nothing leaves your machine.

**Status:** v0.1 (pre-release). macOS-first; Claude Code is the reference integration.

## Why
- **100% local & private** — embeddings via `sentence-transformers`, vector
  search via `sqlite-vec`. Your data never leaves the machine.
- **Your memory is human-readable markdown** in a folder you own — browse it in
  Obsidian or any editor. No bespoke UI to babysit.
- **Structured, not a blob** — `always / when / topics / knowledge` layers with
  retrieval triggers, plus dual-source search over curated memory *and* your
  past session transcripts.

## Install (macOS)
```bash
git clone https://github.com/leonkatz/memory-tools
cd memory-tools
export MEMORY_HOME="$HOME/memory"   # any folder you like
./install.sh
```
`install.sh` sets up a venv, the CLIs, the Claude Code hooks + skill, seeds a
starter memory from `template/`, and builds the vector index.

## CLIs
- `memory-search "query" [--top N] [--json]` — semantic + keyword search.
- `memory-index rebuild | update` — (re)build the vector index.
- `memory-capture` — add a new memory.
- `memory-docs-convert <dir>` — import an existing notes/docs tree into search.

## Layout
Memory lives under `$MEMORY_HOME`:
- `always/` — permanent facts, always loaded.
- `when/` — task-triggered rules.
- `topics/` — canonical-source registry.
- `knowledge/` — long-form durable lessons.

The markdown is the source of truth; `.index/` is a disposable, rebuildable
SQLite vector index (never synced).

## Contributing
See [CONTRIBUTING.md](./CONTRIBUTING.md). Licensed under [Apache-2.0](./LICENSE).
