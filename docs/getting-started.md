# Getting started

Two paths, depending on what you already have. **Path A** is for people with a
pile of Claude Code history they want to bring in. **Path B** is for a clean
machine starting fresh. Both end in the same place: a local memory folder your
assistant uses automatically.

First, install (see the [README](../README.md#install-macos) for details):

```bash
git clone https://github.com/rekol-io/rekol && cd rekol && ./install.sh
```

The installer asks where to keep your memory (default `~/rekol-memory`), sets up
the `rekol` CLI, wires the Claude Code hooks + skill, seeds a starter memory from
`template/`, and builds the first vector index.

REKOL works anywhere Claude Code runs — the terminal, your IDE (VS Code or
JetBrains), or the Claude Desktop app.

What to expect, honestly: on **Day 1** your indexed history is *searchable* —
recall. The assistant can find what you said before. *Understanding* — your
recurring habits ("repos live in `~/src`", "always run the linter before
committing") as ambient, always-loaded memory — grows over time, and the optional
[memory bootstrap](#run-the-memory-bootstrap-later) is how you accelerate it.
Searchable first, understood as it learns.

---

## Path A — existing user (you have Claude Code transcripts)

You've been using Claude Code for a while. Those past sessions are the richest
seed for your memory.

### 1. Run `rekol init`

```bash
rekol init
```

`init` is interactive and every prompt defaults to a safe no-op (pressing Enter
through it changes nothing). It will:

- Detect your past Claude Code sessions under `~/.claude/projects/` and offer to
  **index them** so they become searchable. This wraps
  `rekol session-index --incremental`.
- Offer to **import a notes/docs folder** (e.g. an Obsidian vault) if you have
  one — see [Bring in your history](../README.md#bring-in-your-history).
- Point out any cloud-sync folders (Dropbox, iCloud) where `REKOL_HOME` could
  live so your markdown syncs across devices.

Indexing is opt-in: `init` (and the skill) act after you confirm.

### 2. Verify it landed

```bash
rekol search "something you remember discussing"
```

You should see matching transcript snippets. Your history is now searchable.

### 3. Edit your identity

Open `always/identity.md` (under your memory folder) and tell the assistant who
you are. This file is re-injected at the start of every session.

### 4. Run the memory bootstrap later

Indexing makes history *searchable*; *distilling* it into curated, always-on
memory is a separate step. See
[Run the memory bootstrap later](#run-the-memory-bootstrap-later) — it's optional
and best run once your history is indexed.

---

## Path B — new user (clean machine, no transcripts)

No Claude Code history yet? That's fine — you start from the seeded `template/`
memory and grow from there. There's nothing to back-index, so the path is
shorter.

### 1. Run `rekol init` (optional but recommended)

```bash
rekol init
```

With no transcripts to index, `init` simply reports "No Claude Code transcripts
found" and skips history indexing. It still offers to import a notes/docs folder
if you have one, which is the fastest way to seed a fresh store:

```bash
rekol import ~/Documents/ObsidianVault
```

`import` is a *mechanical* conversion — it makes your docs findable via search.
Filing those notes into the `always` / `when` / `topics` layers is curation: the
assistant's job (or yours, with `rekol capture`).

### 2. Edit your identity

Open `always/identity.md` and tell the assistant who you are. Even a few lines
("I'm a backend engineer; my repos live in `~/src`; I prefer X over Y") give it
ambient context from the very first session.

### 3. Just work

From here, your memory grows as you use the assistant:

- **Capture as you go.** Tell the assistant "remember this" or correct it, and
  it writes a memory file (you approve — no silent saves). Or do it by hand with
  `rekol capture`.
- **Search anytime.** `rekol search "query"` covers both your curated notes and,
  once you have them, your session transcripts.

### 4. Run the memory bootstrap later

Once you've accumulated some Claude Code history, the optional
[memory bootstrap](#run-the-memory-bootstrap-later) distills it into curated
memory — the same step Path A users run.

---

## Run the memory bootstrap later

Indexing (Path A) and importing (Path B) make content **searchable** — that's
recall. *Distilling* your recurring patterns ("always do X", "repos live in Y",
"we chose Z") into the ambient, always-loaded layers that make the assistant
*just know* things is the **memory bootstrap** — a separate, optional step you
run when you're ready.

Two things to be honest about up front:

- **It runs your own Claude over your transcripts.** There's no bundled LLM and
  no API key for the memory layer itself — the bootstrap is a skill routine that
  uses the assistant you already pay for. That means **real token spend** on your
  account, proportional to how much history you bootstrap.
- **Nothing is auto-written.** The bootstrap is conservative and review-gated:
  it proposes high-confidence, recurring, durable memories with provenance, and
  *you* approve what lands in `always` / `when` / `topics` / `knowledge`. It
  never silently rewrites your memory.

To run it, open Claude Code and say: **"set up my rekol memory"** (or "bootstrap
my rekol memory"). The assistant drives the flow; you review and accept the
candidates. Because it operates over your indexed corpus, run it once you've
indexed your history (Path A step 1), or once enough history has accumulated
(Path B).

> Indexing and manual `rekol capture` build curated memory you can rely on today.
> The framing above holds throughout: searchable on Day 1, understood as it
> learns.

---

## Where to go next

- **[README](../README.md)** — install options, the full CLI, layout, sync, and
  uninstall.
- **[READ-ME-CLAUDE.md](../READ-ME-CLAUDE.md)** — point a fresh Claude instance
  at this to teach it the capture/retrieval protocol.
- **`rekol --help`** — every subcommand. `rekol doctor` reports index health if
  search ever looks off.
