# REKOL

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
git clone https://github.com/leonkatz/rekol
cd rekol
export REKOL_HOME="$HOME/memory"   # any folder you like
./install.sh
```
`install.sh` sets up a venv, the `rekol` CLI, the Claude Code hooks + skill,
seeds a starter memory from `template/`, and builds the vector index.

## CLI
A single `rekol` command with subcommands:
- `rekol search "query" [--top N] [--json]` — semantic + keyword search.
- `rekol index rebuild | update` — (re)build the vector index.
- `rekol capture` — add a new memory.
- `rekol import <dir>` — import an existing notes/docs tree into search.

## Layout
Memory lives under `$REKOL_HOME` (`$MEMORY_HOME` is accepted as a fallback):
- `always/` — permanent facts, always loaded.
- `when/` — task-triggered rules.
- `topics/` — canonical-source registry.
- `knowledge/` — long-form durable lessons.

The markdown is the source of truth; `.index/` is a disposable, rebuildable
SQLite vector index (never synced).

## Contributing
See [CONTRIBUTING.md](./CONTRIBUTING.md). Licensed under [Apache-2.0](./LICENSE).
