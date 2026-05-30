# Standalone Local-First Memory Module — v0.1 Extraction Design

**Date:** 2026-05-30
**Status:** Design — pending user review, then implementation plan
**Author:** Leon Katz (with Claude)
**Working name:** `memory-tools` (placeholder — naming + domain pass deferred)

---

## Problem / Opportunity

`memory-tools` is currently a subcomponent of the private `mac_setup` repo,
installed by its phase-3 script. It is already a real Python package (src layout,
`pyproject.toml`, console-script entry points, 20+ test modules, migration
tooling), and it is **fully local and private** by construction
(`sentence-transformers` for embeddings + `sqlite-vec` for the vector store —
no cloud, no API key, nothing leaves the machine).

The opportunity: extract it into a standalone open-source project so others can
install and use it. The hard ~70% (engine, tests, CLIs, hooks, skill, install
script) already exists. What remains is **decoupling, genericization, and OSS
scaffolding** — not invention.

## Positioning (the differentiation thesis)

A **local-first, files-you-own memory layer for AI assistants.** Three claims
that, *together*, nobody else owns:

1. **100% local & private** — local embedding model + local SQLite vector store.
   No cloud, no API key, no data egress. (mem0 / Zep / Letta all lean on hosted
   infra or a server you run; none lead with this.)
2. **Your memory is human-readable markdown in a folder you own** — browse it in
   Obsidian / VS Code if you ever want to. *No bespoke UI is a feature, not a
   gap.* (Basic Memory shares the markdown idea but is a flat Obsidian-style
   graph.)
3. **Structured, not a blob** — the `always / when / topics / knowledge` layers
   carry retrieval *triggers* (`when-*` fire on activity, `topics/<noun>` on
   nouns), plus **dual-source search** over curated memory *and* raw session
   transcripts.

The market splits into "opaque cloud memory" (mem0 / Zep) and "human wiki / KB"
(Basic Memory / Notion-likes). This project occupies a third, unclaimed spot:
**structured, local-first, files-you-own memory whose primary interface is the
assistant, not a human UI** — with shared-team sync and source-provenance as
opt-in future layers.

## Architectural Thesis: agnostic core, adapters per assistant

The engine is already a library: `src/memory_tools/{store,indexer,embeddings,
search_combined}.py` is the core; the `cli_*.py` files are thin Click wrappers;
the only Claude-specific coupling lives in `hooks/` + `skill/` + session
transcript ingestion. We make that boundary explicit so future assistants plug
in as adapters rather than rewrites.

- **v0.1** — ship the **agnostic core** + the **Claude Code adapter** (the
  reference integration we dogfood daily).
- **v0.2** — add an **MCP adapter** (one server → Claude Desktop, Cursor,
  Continue, Cody, …). MCP is what *proves* agnosticism rather than just claiming
  it; because the core is already a library, it is a drop-in adapter, not a
  rewrite.

**The one discipline that matters:** the core never parses a Claude transcript
or a hook payload. All Claude-isms live in `adapters/claude_code/`, which hands
the core plain `{text, metadata, timestamp}` records.

---

## v0.1 Scope

### In
- **History-preserving extraction** into a new standalone repo (see Step 1).
- The **engine** as a single pip-installable package (one package, explicit
  internal core/adapter boundary).
- The **Claude Code adapter**: SessionStart/PostToolUse hooks, the `memory`
  skill, and **session-transcript reading** (`claude-session-index`,
  `sessions/`) — dual-source search ships in v0.1.
- **`memory-docs-convert`** as the **onboarding / cold-start importer**
  ("bring your own corpus": point it at an existing notes/Obsidian/docs tree and
  make it searchable), genericized (see below).
- **`git clone + ./install.sh`** install path (Homebrew deferred).
- **Personal-data strip** + genericization (paths, identity, install defaults).
- Reserve the **`scope:` frontmatter field** (`private` default).
- **Apache-2.0** license + OSS scaffolding (CI, README, CONTRIBUTING, etc.).

### Out (roadmap, noted not specced here)
- MCP adapter (v0.2).
- `memory digest` health/summary command (v0.3).
- Shared-team memory server (`scope: shared` activates).
- Research persona: full-document archiving + periodic source verification.
- Homebrew tap (fast-follow once tagged releases exist).
- Linux / Windows support.
- Final name + domain.

---

## Implementation Outline

### Step 1 — History-preserving extraction into a new repo
Create the standalone repo from `memory-tools/`'s real history (not a copy):

```
git subtree split -P memory-tools -b memory-export      # zero-dependency option
# or: git filter-repo --path memory-tools/               # cleaner if installed
```

The resulting branch becomes the new repo's `main`. The new repo is the source
of truth going forward. **Base the extraction on `origin/main`** (which now
includes `docs_convert/`), not an older local checkout.

Post-extraction, `mac_setup` consumes the result via one of: pinned vendored
copy, submodule, or the published package. (Decision deferred; not a v0.1
blocker. Until resolved, avoid divergent edits in both trees.)

### Step 2 — Repo structure: one package, explicit boundary
```
<repo>/
  src/memory_tools/
    core/            # engine — ZERO assistant assumptions
      store.py  indexer.py  embeddings.py  search_combined.py
      chunker.py  model.py  config.py
    adapters/
      claude_code/   # the ONLY Claude-aware code
        ingest.py    # transcript .jsonl -> plain {text, metadata, timestamp}
        (hooks/ + skill/ packaged alongside)
    docs_convert/    # bring-your-own-corpus importer (see Step 4)
    cli_*.py         # thin Click wrappers (engine-level)
  hooks/  skill/  template/  tests/  docs/
  install.sh  pyproject.toml  LICENSE(Apache-2.0)  README.md
  CONTRIBUTING.md  CODE_OF_CONDUCT.md  CHANGELOG.md
  .github/workflows/ci.yml  .github/ISSUE_TEMPLATE/ ...
```
Physically split into separate packages (`memory-core` + adapters) only when
MCP lands in v0.2 and proves the seam — not before.

### Step 3 — Genericization / personal-data strip
- Audit `skill/memory/skill.md` and `template/*` for "Leon", Dropbox paths, or
  machine-specific assumptions → replace with `$MEMORY_HOME` / generic examples.
- Make install-time **legacy migration opt-in**: `install.sh` currently runs
  `memory-migrate auto` by default (a Bedrock/legacy path specific to this
  machine). Gate it behind a `--migrate` flag; new users should not get it.
- Reframe sync as **optional, local-only by default** (see Sync Model). Reword
  "Dropbox-backed recommended" → "point `$MEMORY_HOME` at any folder; sync it
  however you like."
- Keep `template/always/identity.md.example` generic.

### Step 4 — `memory-docs-convert` genericization
- Turn the hardcoded text-extension allowlist (`TEXT_EXTENSIONS`) into
  `--include` / `--exclude` flags so HTML and other formats are **opt-in**
  rather than silently dropped (the HTML exclusion was a personal call).
- Document it as the onboarding path in the README.
- **v0.2 cleanup (noted):** it currently emits synthetic Claude `.jsonl` to
  reuse the ingester — a Claude-ism in a general feature. Once the core/adapter
  seam is firm, retarget it at a generic record format.

### Step 5 — Sync Model (three tiers)
- **Markdown source** (`always/when/topics/knowledge`) → *optionally* synced;
  user's choice of Dropbox, iCloud Drive, a git remote, Syncthing, or nothing.
- **Vector index** (`.index/`) → always local, disposable, rebuildable.
- **Session transcripts / `sessions.db`** → always local.

`install.sh` defaults to local-only and documents sync options rather than
assuming Dropbox. The `.dropboxignore` write stays (harmless when Dropbox is
absent); `git_track` remains an opt-in audit-trail option.

### Step 6 — `scope:` frontmatter reservation
Add `scope: private` (default) to the frontmatter schema + template now. Nothing
reads it in v0.1; it exists so the future shared-team server does not require
migrating everyone's corpus.

### Step 7 — OSS scaffolding
`LICENSE` (Apache-2.0) · `README.md` (positioning + 60-second install) ·
`CONTRIBUTING.md` · `CODE_OF_CONDUCT.md` · `CHANGELOG.md` · version `0.1.0` ·
issue/PR templates · `CODEOWNERS` (auto-requests maintainer review).

### Step 8 — Lint / format / type gate (enforces readability bar)
The project's "verbose, well-commented, human-readable" standard must hold for
*contributions*, not just first-party code, so it is enforced by tooling in two
places (local + CI):

- **Ruff** — lint **and** format in one tool (replaces flake8/isort/Black).
  Enable the docstring (`D`), naming (`N`), and complexity rules so the
  readability/naming conventions are machine-checked.
- **mypy** — static type checking, **pragmatic** to start: type the public API
  and all new code; tighten toward strict over time. (Avoids blocking the
  extraction on retro-annotating the entire existing codebase.)
- **pre-commit** — runs Ruff + mypy + a fast test subset on `git commit`, so
  contributors get the same gate locally before they push.

### Step 9 — CI + branch protection
- **GitHub Actions CI** on every PR: Ruff (lint+format check), mypy, full
  pytest + bats suites, on macOS (a Linux job marked allow-fail as a
  portability signal).
- **Branch protection on `main`**: require the CI status checks to pass **and**
  one maintainer review before merge; no direct pushes to `main`.

### Step 10 — Publishing model & contribution flow
- **Private-first → public at launch.** Develop the extraction in a *private*
  repo; flip to public only when v0.1 is release-ready. The repo name does
  **not** block starting (GitHub renames auto-redirect old URLs); the name only
  needs to be locked before (a) the public flip / announcement or (b) any PyPI
  publish (PyPI names are permanent — and PyPI is deferred out of v0.1 anyway).
- **Contribution flow (fork + PR — the small-project standard):** contributor
  forks → branches → `pre-commit install` → change → tests pass → opens a PR
  against `main` → CI must be green → maintainer review → merge. Documented in
  `CONTRIBUTING.md` (mostly boilerplate).
- **Contribution licensing: inbound = outbound.** A one-line note in
  `CONTRIBUTING.md` states contributions are licensed under the repo's
  Apache-2.0. No DCO or CLA for v0.1 (revisit only if a company/foundation forms
  around the project).

---

## Distribution

- **v0.1:** `git clone <repo> && ./install.sh` (works today; full control over
  venv, hooks, skill, index build, and the sqlite-vec/Python-extension gotcha).
- **Fast-follow / v0.2:** Homebrew tap (`brew install …`) once tagged releases
  and a tap repo exist — deferred because it needs release machinery and a
  versioned tarball+checksum per release.

## License

**Apache-2.0** — permissive plus an explicit patent grant and retaliation
clause; the modern default for infra tooling and friendliest if a hosted /
shared-server component grows later. (MIT considered; AGPL ruled out as
adoption-hostile here.)

## Testing

- Keep the existing pytest + bats suites.
- **Add a boundary test:** assert `core/` imports nothing from `adapters/`
  (enforces the seam that makes MCP a drop-in).
- **Add an install smoke test:** `install.sh --test-mode` against an empty
  `MEMORY_HOME` seeds the template cleanly.

---

## Roadmap (post-v0.1)

| Phase | Contents |
|---|---|
| v0.2 | MCP adapter (proves agnosticism; unlocks Cursor/Continue/etc.); Homebrew tap; `docs-convert` generic-record retarget |
| v0.3 | `memory digest` — health/summary command (what was learned, stale/duplicate/contradiction report). *Not* a browse-your-memory UI. |
| Later | Shared-team memory server (`scope: shared`); research persona (full-doc archive + source verification); Linux/Windows support; final name + domain |

## Open Items

- **Name + domain.** Before locking a name, check three namespaces: GitHub
  (org/repo), PyPI (package name — common words likely taken), and a domain
  (`.dev` / `.sh` read more "dev tool" than `.io`; a domain is *not* required to
  ship v0.1). Candidates so far: *Strata* (layered model), *Engram* (memory
  trace). Run a dedicated availability sweep + positioning pass before launch.
- **`mac_setup` consumption mechanism** post-extraction (vendor / submodule /
  package dependency).

## Non-Goals (v0.1)

- No human browse/search UI (the markdown files *are* the browsable surface).
- No MCP server yet.
- No full external-document storage (links-with-provenance via the existing
  `reference` type / `topics/` is enough; full archiving is a later research
  persona feature).
- No shared/remote store implementation (only the `scope:` field is reserved).
- No cross-platform support yet (macOS first).
