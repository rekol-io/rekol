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

## Quickstart (fresh install)

1. `git clone https://github.com/leonkatz/rekol && cd rekol`
2. Point REKOL at a folder you own: `export REKOL_HOME=~/rekol-memory`
   (sync it via Dropbox/iCloud/git/Syncthing or keep it local — the `.index/`
   directory stays local and is excluded from sync).
3. `./install.sh` — seeds the empty root from `template/`, builds the first
   index, and installs the hook + skill.
4. `rekol init` — indexes any existing Claude Code history and offers to import
   your notes.
5. Edit `always/identity.md`, then try `rekol search "..."` / `rekol capture`.

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

## Sync (optional)
`$REKOL_HOME` is a local folder you own; sync it across machines however you
like — Dropbox, iCloud Drive, a git remote, Syncthing, or not at all. The
vector index under `.index/` stays local and must be excluded from sync (it is
machine-specific and rebuildable). The installer writes `.dropboxignore`; for
other sync tools, exclude `.index/` yourself.

## Contributing
See [CONTRIBUTING.md](./CONTRIBUTING.md). Licensed under [Apache-2.0](./LICENSE).
