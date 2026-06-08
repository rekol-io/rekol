---
name: rekol-init
description: Assistant-led REKOL memory onboarding — the PRIMARY first-run setup interview. Trigger on "/rekol init", "set up my rekol memory", "rekol initialize my memory", "onboard rekol", "get rekol started", or right after install when a user asks how to begin. You (the user's own Claude) run a short five-step interview in one context — recall (index history), include (knowledge dirs), understanding (bootstrap), baseline (scaffolds), close — shelling out to mechanical `rekol` CLI commands and doing the bootstrap reasoning yourself. Opt-in, re-runnable, adapts to what's detected. Distinct from `rekol-bootstrap` (the distil-only routine this orchestrates at step 3) and the mechanical `rekol init` CLI (the scriptable fallback).
---

# rekol-init

The assistant-led onboarding interview — the **primary** way a fresh REKOL
install gets set up. **You** (the user's own Claude) run it: a short, adaptive
five-step conversation that turns an empty install into searchable recall and,
optionally, curated understanding. You shell out to mechanical `rekol` commands
for the I/O and do the bootstrap reasoning yourself, all in this one context.

The mechanical `rekol init` CLI is the scriptable fallback; this skill is the
real onboarding. Run it when the user says `/rekol init`, "set up my rekol
memory", "rekol initialize my memory", or otherwise asks to get started.

## The two honest layers (the spine of the whole interview)

Frame everything around these two — they are what makes onboarding feel honest
rather than magical:

- **Recall** — your Claude Code history and knowledge files, made *searchable
  now*. Indexing is mechanical and immediate. This is step 1 (sessions) and step
  2 (knowledge dirs).
- **Understanding** — recurring instructions distilled into *curated, ambient
  memory* that grows over time. This is the bootstrap (step 3), and it keeps
  growing as you work (the capture protocol in the `rekol` skill).

## Non-negotiables (read before you start)

- **Nothing is indexed or captured without an explicit yes.** Every step is
  opt-in. You may run the *detection* commands freely (they only read), but you
  run an *ingesting* command (`session-index`, `include add`, `bootstrap`,
  `capture`) only after the user agrees to that step.
- **Silent-skip an empty source.** If a step's source turns up nothing, say one
  calm line and move on — NEVER "Found 0 sessions, index them?". A `0` is not a
  question.
- **"(you can change this anytime)" — everywhere.** Every choice is reversible
  and the flow is re-runnable. Say so at each step so no choice feels permanent.
- **Smart Enter-through defaults.** Offer a sensible default per step and let the
  user accept it with a word. Don't make them spell out everything.
- **Adapt to what's detected.** This is an interview, not a script. Skip steps
  that don't apply; spend words where there's a real decision.
- **Magnitude-aware.** Before a long job (indexing or bootstrapping a big
  corpus), say it may take a while so a multi-minute run isn't a surprise.
- **Review-gated understanding.** The bootstrap NEVER auto-writes memory. It goes
  through the same approve/edit/skip gate the `rekol-bootstrap` skill defines.

## Before you begin — detect, don't ask blindly

Run these read-only probes first so every offer is grounded in real counts (the
count IS the coverage signal you show the user):

- Past Claude Code sessions: `rekol doctor` (or just note `rekol session-index`
  will report the count) — the headline recall signal.
- Knowledge dirs: `rekol include discover <root> --json` for a candidate root
  (e.g. the user's home or a notes/docs folder they name). Junk-filtered, ranked
  by markdown count.
- Current scope: `rekol include show` — surfaces any already-included dirs +
  deny globs (so a re-run shows current state, fully editable).

If a probe returns nothing for a source, that source's step is silently skipped.

---

## Step 0 — Intro (always)

Open with the honest framing in two or three sentences, no jargon:

> REKOL gives this assistant memory in two layers. **Recall** makes your past
> Claude Code work and your knowledge files *searchable*. **Understanding**
> distils the recurring instructions you've given — "we always X", "repos live
> in Y" — into curated memory that grows over time. Nothing gets indexed or
> saved without your say-so, and you can change any of this anytime.

Then move into the steps that actually apply. Don't enumerate steps that will be
skipped.

## Step 1 — Recall: index your sessions *(only if sessions exist)*

If there are past Claude Code transcripts, offer to index them so REKOL can
search your history. If there are none, **silently skip** — say nothing about
sessions.

Present three choices with a default:

- **Index all** *(default)* — `rekol session-index --incremental` (idempotent and
  cheaper: it indexes everything not already indexed, so it's right for both the
  first run and re-runs; `--full` re-embeds from scratch — reserve it for a forced
  rebuild).
- **Recent only** — for a user who wants a quick start: index incrementally now;
  the rest catches up automatically on future SessionEnd runs. (`rekol
  session-index --incremental`.)
- **Skip** — leave history unindexed; mention they can run `rekol session-index`
  anytime.

**Magnitude-aware:** if the history is large (hundreds+ of sessions), warn up
front that indexing embeds every message and may take a few minutes, so they can
choose Recent-only or defer with eyes open. Keep "Index all" the recommended
default — recall is the point.

## Step 2 — Include: your knowledge files *(only if any are found)*

Run `rekol include discover <root> --json` against a root the user names (or
their home / an obvious notes folder). The result is **junk-filtered** (no
`node_modules`/`.git`/build noise) and **ranked by markdown count**. If it's
empty, **silently skip**.

Show a **ranked, grouped** summary — the top dirs with their counts, NOT a dump
of 50 paths. Then offer:

- **All** *(default, clean)* — include the discovered dirs as-is. Junk is already
  filtered, so "All" is genuinely clean, not 5,000 files of garbage. For each dir
  the user wants: `rekol include add <dir>`.
- **Custom** — an **exclude-list**: the user removes what they don't want.
  Excludes can be **conversational** — "skip the vendored stuff and old-projects/"
  becomes deny globs: `rekol include deny vendored old-projects`. Excluding a
  folder takes everything under it out. Add the dirs they keep with `rekol
  include add <dir>`; persist the excludes with `rekol include deny <glob>...`.

This is **scope, not a one-time import**: a dir included once auto-indexes its
new files on every future `rekol session-index` run, governed by the deny-list.
Say so, and say it's editable anytime (`rekol include show` / `add` / `remove` /
`deny` / `allow`). After persisting, you may kick off the first index of the
included content with `rekol session-index --incremental`.

## Step 3 — Understanding: bootstrap *(offer when there's indexed history)*

This is the **understanding** layer — and it's where you do the real reasoning,
not just shell out. Be honest about value AND cost:

> Bootstrap mines your indexed history for recurring, durable instructions and
> proposes curated memory for them — review-gated, nothing saved without your
> approval. It runs your own Claude (no API key), but it **uses tokens** and can
> take a while on a big corpus.

Offer three choices:

- **Bootstrap now** — then pick a **scope**:
  - *Most-active / recent window* **(default)** — the bounded default
    (`rekol bootstrap`, no scope flags): a recent window, capped sessions.
  - *Recent window, tighter/wider* — `rekol bootstrap --scope-days N`.
  - *Specific projects* — `rekol bootstrap --scope-project <slug> ...`.
  - *Everything* — `rekol bootstrap --all-time` (warn: slowest, most to review).
- **Later** — defer, and **print the trigger** so it's actionable: "run
  `rekol bootstrap` anytime, or just say 'bootstrap my memory'."
- **Skip** — don't mine; understanding will still grow via the capture protocol.

If the user chooses **now**, drive the bootstrap exactly as the
**`rekol-bootstrap`** skill describes — plan once, then process one batch at a
time (`rekol bootstrap --next`), present the approve/edit/skip gate, `rekol
capture` approved items, `--mark-done` each batch. Use the **scalable-review**
affordances for a big corpus so the user isn't drowned:

- **Grouped + confidence-ranked** — review per batch, strongest first.
- **Stop-early** — `rekol bootstrap --top N` reviews the strongest N per batch
  and defers the rest (nothing is dropped).
- **Bulk-approve** — `rekol bootstrap --bulk-approve <batch>` emits every capture
  command for a batch so the user can approve the whole batch in one action (you
  still run the captures only after they approve).

End the bootstrap with a **customize/refine pass**: `rekol bootstrap --refine`
summarizes what landed; offer to adjust layers/wording on anything the user wants
changed. Remind them it's all editable anytime.

If there's no indexed history yet (they skipped step 1, or it was empty), **skip
this step** — there's nothing to mine. Tell them bootstrap becomes available once
they index, in one line.

## Step 4 — Baseline (default-on, quiet)

Make sure the home has its baseline. Two parts:

1. **Behavioral rules** already ship in the `rekol` skill (search-don't-guess,
   grounding, hedging, capture). Nothing to install — just mention the assistant
   now follows them.
2. **Learning scaffolds** — the generic `always/when/topics/knowledge` directive
   files that seed general memory. **Gap-fill, don't overwrite**: seed only the
   layers the user is missing, never clobbering their own files. The simplest way
   is to run the dedicated seed primitive, which is gap-fill by construction and
   does nothing else: `rekol init --seed-only` (it copy-if-absent seeds the starter
   pack — use `--seed-only`, NOT `--yes`, which on a machine with history would also
   fire a full indexing pass). On a fresh/empty home this lands the scaffolds; on a
   populated one it adds only the missing layers.

These scaffolds are **inert directives** ("record the user's name here as you
learn it"), never placeholder data — so the assistant never recalls a template
as a fact. Keep this step quiet: one line that the baseline is in place.

## Step 5 — Adaptive close

Adapt the ending to what actually happened:

- **With content** (you indexed sessions and/or included knowledge): print the
  **coverage report** — `rekol coverage` for the include-scope line, and recap
  the session count you indexed: "indexed N sessions · Z of ~Y files (~C%)". If
  the bootstrap ran, add "K proposals reviewed / captured."
- **Empty or tiny** (nothing indexed, fresh install): **skip the report** — there
  is nothing to measure. Pivot to "you're set up; here's how it grows": capture
  as you work (`rekol capture` / "remember this"), re-run `rekol init` as your
  history builds, include a knowledge dir anytime (`rekol include add <dir>`).

Either way, end on the signature line, verbatim:

> It recalls what it has, faithfully — what it doesn't have, it'll tell you.

And remind them the whole flow is re-runnable and every choice is reversible.

---

## Re-runs

On a re-run, lead with current state instead of assuming a blank slate: `rekol
include show` for the scope, note what's already indexed, and offer to *edit*
rather than redo. Every step stays opt-in and idempotent — indexing is
incremental, scaffolds are gap-fill, scope edits are additive/removable. Nothing
you do here clobbers the user's existing files.

## What you orchestrate (quick reference)

| Step | Mechanical command(s) | Your reasoning |
| --- | --- | --- |
| 1 Recall | `rekol session-index --incremental` (`--full` only for a forced rebuild) | magnitude warning, default pick |
| 2 Include | `rekol include discover --json` / `add` / `deny` / `show` | rank + group, conversational excludes → deny globs |
| 3 Understanding | `rekol bootstrap` (+ `--next`/`--top`/`--bulk-approve`/`--refine`), `rekol capture` | cluster/classify, approve gate, layer correction (per `rekol-bootstrap`) |
| 4 Baseline | `rekol init --seed-only` (gap-fill seed) | confirm baseline, keep quiet |
| 5 Close | `rekol coverage` | adaptive: report with content, "how it grows" when empty |
