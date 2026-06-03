# REKOL

> **Local-first memory for the AI assistant you already use.** A drop-in memory
> layer — no API key, runs on your machine, and your assistant uses it
> automatically. Your memory is markdown in a folder you own.

**Status:** v0.1 (pre-release). macOS-first; Claude Code is the reference integration.

## Why

You don't run memory commands — you just work, and the assistant already has
your context and uses it. REKOL's specific corner:

- **No API key, fully local.** Embeddings (BAAI `bge-small`) and vector search
  (`sqlite-vec`) run on your machine. No account, no key, no telemetry — the
  memory layer doesn't even need an LLM provider.
- **A drop-in *layer*, not a new app.** REKOL plugs into the assistant you
  already use (Claude Code today, every MCP assistant next) — no agent to adopt,
  no tool to switch.
- **Memory that surfaces itself.** A layered model (`always / when / topics /
  knowledge`) injects the right context at the start of each session, and the
  assistant pulls in more as it works — it just *knows*, instead of being told
  to look.

Your memory is plain **markdown you own** (Obsidian, grep, git), and REKOL
searches your past **session transcripts** alongside your curated notes — these
are table stakes done well, not the headline.

## Install (macOS)
```bash
git clone https://github.com/leonkatz/rekol && cd rekol && ./install.sh
```
The installer asks where to keep your memory (default `~/rekol-memory`,
press Enter to accept). It then sets up a venv, the `rekol` CLI, the Claude
Code hooks + skill, seeds a starter memory from `template/`, and builds the
vector index.

**Optional — choose the folder up front** (skips the prompt):
```bash
export REKOL_HOME="$HOME/rekol-memory"   # any folder you own
```
Sync it via Dropbox/iCloud/git/Syncthing or keep it local — the `.index/`
directory stays local and is excluded from sync.

## Bring in your history

A fresh install starts with an essentially empty store. To seed it from work
you already have:

- **Index past Claude Code sessions** — `rekol session-index --incremental`
  makes your existing transcripts searchable. `rekol init` wraps this (and the
  steps below) in confirm prompts. REKOL never indexes on its own — just open
  Claude and ask it to "set up my rekol memory" or "index my past sessions".
- **Import a notes/docs folder** — `rekol import ~/Documents/ObsidianVault`
  converts a tree of text files into searchable content. This is a mechanical
  conversion (it makes your docs findable) — not an LLM filing notes into the
  `always`/`when`/`topics` layers.
- **Verify it landed** — `rekol search "something you wrote"`.

## Quickstart (fresh install)

1. `git clone https://github.com/leonkatz/rekol && cd rekol && ./install.sh`
   — answer the memory-folder prompt (or pre-set `REKOL_HOME`), then it seeds
   `template/`, builds the first index, and installs the hook + skill.
2. `rekol init` — indexes any existing Claude Code history and offers to import
   your notes.
3. Edit `always/identity.md`, then try `rekol search "..."` / `rekol capture`.

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

## Uninstalling
rekol is yours to remove cleanly. From the repo:
```bash
./uninstall.sh            # interactive — asks before deleting the rebuildable index
./uninstall.sh --dry-run  # preview every change without touching anything
./uninstall.sh --yes      # non-interactive (keeps the index)
```
The uninstaller reverses what the installer set up — the `~/bin/rekol` shim, the
`rekol`/`memory` skills, the `~/.local/share/rekol` venv, every rekol hook in
`~/.claude/settings.json` (plus `env.REKOL_HOME`), and the rekol PATH + env
export lines in `~/.zshrc`. It backs up `settings.json` and `.zshrc` to
timestamped `.bak` files before editing them, and is idempotent (safe to re-run).

If you installed to **custom paths** (`--tools-home` / `--bin-dir`), you don't
need to repeat them: the installer records the resolved paths in a manifest at
`$REKOL_HOME/.install-logs/manifest.env`, and `./uninstall.sh` reads it to find
the right venv and shim. Precedence is explicit flags, then the manifest, then
the built-in defaults. If a path can't be confirmed (no manifest and nothing at
the default), the uninstaller reports it as a possible leftover at the end rather
than silently skipping it — re-run with `--tools-home PATH` / `--bin-dir PATH` (or
delete it by hand).

**Your markdown memory is never deleted.** Everything under `$REKOL_HOME`
(`always/`, `when/`, `topics/`, `knowledge/`, your `*.md`, the config, the local
git repo) is preserved. Only the derived `.index/` is removable — and only with
`--purge-index` or by confirming the prompt. After uninstalling, run
`source ~/.zshrc` (or open a new terminal). Re-running `./install.sh` later works
cleanly from that state.

## Contributing
See [CONTRIBUTING.md](./CONTRIBUTING.md). Licensed under [Apache-2.0](./LICENSE).
