# Design: memory confidence metadata

**Status:** proposal — scope agreed, awaiting product call on pre- vs post-launch.
**Tracking issue:** #87
**Related:** #86 (memory confidence & anti-staleness epic), #57 (local-only cache posture).

## Problem

rekol's highest-value lever isn't *more capture* — it's making recall **trustworthy**, so
the assistant doesn't state stale facts confidently. Today a memory carries only `updated`
(last write). That can't distinguish:

- a 6-month-old note that's been **confirmed true** three times since → trustworthy, vs
- a note edited yesterday that **nobody has checked** against the live system → not.

The dangerous failure mode is *confidently wrong recall*: the assistant asserts a fact that
silently went stale (a file moved, a cluster was decommissioned). We have no signal for it.

## Goals

- Add **confidence metadata** to memories: when a fact was last *used*, last *confirmed
  true*, and whether/when it was *invalidated*.
- **Additive and forward-only**: start accumulating the day the feature ships; **never
  rewrite or backfill existing files**. Absence of metadata = "unknown / predates the
  system → treat as unverified."
- **Surface** the signal to the assistant so it can ground its confidence.
- **Guide, don't dictate**: let the assistant work *with the user* to decide what to do
  about a low-confidence fact, rather than hard-coding a staleness rule.

## Non-goals (deferred)

- An automated staleness *judgment* ("month-old + untouched ⇒ stale"). Staleness is
  **contextual** — a month-old IP is stale; a month-old architecture decision isn't. That
  judgment is delegated to the assistant-in-context (see Layer 3), and any future
  *automation* of it is out of scope here.
- Semantic contradiction detection at capture (#86 item 4).
- Changing how capture/search/index fundamentally work.

## Design

### The three fields and where each lives

Placement is driven by **how often a field changes** — the high-frequency mechanical signal
must not live in a synced file, or every recall would rewrite (and re-sync, and re-commit) a
file the user never edited.

| Field | Changes | Home | Rationale |
|---|---|---|---|
| `last_used` | every search that returns the hit | **local index DB** (cache) | Mechanical, high-frequency, and inherently *per-machine* (laptop vs work Mac differ). Writing it to frontmatter would thrash sync + git on every search. The cache already holds derived, local, disposable state (`index.db`/`sessions.db`). |
| `last_confirmed` | rarely, deliberately | **frontmatter** | Human-meaningful, low-frequency, *should* sync across devices and show in `git blame`. |
| `invalidated_at` | once | **frontmatter** | A real, one-time fact about the memory's life; should travel + appear in history. |

This hybrid is the core decision. Everything-in-the-file hits the `last_used` write-thrash
problem; everything-in-the-index loses cross-device sync and git history for the two fields
that deserve them.

### Forward-only / additive falls out for free

- `last_used` lives in a new index table keyed by memory path/chunk. Facts never recalled
  since the feature shipped simply have **no row** → absence = unknown. No backfill.
- `last_confirmed` / `invalidated_at` are optional frontmatter keys. Existing files don't
  have them; the parser already tolerates unknown keys (see Constraints), so nothing breaks
  and nothing is rewritten until the user/assistant deliberately confirms or invalidates a note.

### A small lifecycle: live → suspect → invalid

Rather than *deleting* a wrong memory, stamp it. This gives a tiny state machine:

- **live** — normal.
- **suspect** — flagged mid-session when a contradiction is observed ("`git log` shows this
  file moved"), with a reason, without forcing a full rewrite. (Subsumes #86 item 1,
  `flag-suspect`.)
- **invalid** — `invalidated_at` set; the fact is known-dead but retained, so search can
  *exclude-but-explain* ("invalidated 2026-05 because X") instead of silently dropping it.

`last_confirmed` is the positive counterpart: it moves a note back toward "trusted."

### The three layers (and why the judgment layer is the cheapest)

1. **Write** (the real work): stamp `last_used` in the index on recall; read/write
   `last_confirmed` + `invalidated_at` in frontmatter via capture/edit and small new
   commands (`confirm`, `flag-suspect`, `invalidate`-a-memory).
2. **Surface** (cheap once written): show the signal on search hits, e.g.
   `updated 3mo ago · confirmed never · used 12×`. This is the #86-item-5 "confidence tag"
   — a display line, not a feature, once the data exists.
3. **Guide, don't dictate** (skill text, ~zero code): a *soft* behavioral note in the rekol
   skill — "when a hit's metadata suggests it may be unverified or old, don't state it
   flatly; surface the uncertainty and work with the user to confirm, update, or invalidate
   it." No `if stale then X`. This delegates the contextual judgment to the assistant +
   user, which is the thing that's actually good at it.

This sidesteps the unsolvable "define stale in code" problem entirely. The only deferred
piece is the *optional* future automation of that judgment.

## New CLI surface (sketch)

- `rekol confirm <file>` — stamp `last_confirmed: <now>` in frontmatter (preserving all
  other keys).
- `rekol flag-suspect <file> --reason "..."` — mark suspect + reason; surfaced first by review.
- `rekol invalidate-memory <file> --reason "..."` — stamp `invalidated_at`; retained, not deleted.
  (Distinct from the existing session-scoped `rekol invalidate`; naming TBD to avoid confusion.)
- `last_used` needs no command — it's stamped automatically on recall.
- Search hit rendering gains the compact confidence tag.

## Schema / storage changes

- **Index (local cache):** a new table, e.g. `memory_usage(path TEXT, chunk_id, last_used_iso, use_count)`,
  updated on each search that surfaces the hit. Lives in the existing `index.db` (cache) — never synced.
- **Frontmatter:** two new optional keys, `last_confirmed` and `invalidated_at` (ISO-8601),
  plus an optional `suspect` marker (`{since, reason}`).

## Constraints (grounded in current code)

- **Parsing already tolerates extra keys.** `model.load_memory_file` (uses
  `python-frontmatter`) validates only `name`/`description`/`type` and reads `tags`/`aliases`;
  unknown keys are ignored. So adding the new keys does **not** make a file "invalid." ✅
- **Writes must preserve unknown keys.** `MemoryFile` is a fixed dataclass that *drops*
  unknown frontmatter keys. The capture/edit/confirm write path must round-trip via the raw
  `frontmatter.Post` (read → merge the changed keys → dump), **not** reconstruct frontmatter
  from a `MemoryFile`, or it would silently strip `last_confirmed`/`invalidated_at`. This is
  the one real implementation gotcha. (Verify the current capture write path before building.)
- **No migration.** Forward-only by construction; nothing reads or rewrites old files.

## Risks

- **Calibration / nagging (main risk).** An over-eager Layer-3 nudge that hedges on
  everything trains the user to ignore it. Mitigation: tone lives in skill text ("raise it
  once, lightly; only when the fact is *verifiable* and the signal is *genuinely* weak —
  don't interrogate"), tunable post-launch with **zero code change**.
- **`last_used` write on the read path.** Recall now does a small index write. Must be cheap
  and best-effort (a failed usage-stamp must never fail a search). Batch/throttle if needed.
- **Command naming collision** with the existing session `invalidate`. Resolve before shipping.

## Pre- vs post-launch — for product to decide

The two real launch *blockers* (#84 pending-review→cache, #85 embeddings offline-first) are
done, so this is **optional against the Jun 16 date**. The decision is strategic, not
technical:

- **For pre-launch:** this is the feature that most directly backs rekol's core promise —
  *"memory that doesn't make your assistant confidently wrong."* Launch is when the project
  gets the most eyes; a differentiator shipped at launch earns visibility that a post-launch
  feature only gets *if the project is already popular*. Missing that window isn't fatal but
  could be hard (and financially limiting) to recover.
- **Against pre-launch:** it's the largest of the candidate features — a new index table + a
  write on the recall path + new commands + frontmatter round-trip care. It adds scope risk
  to a fixed date when the actual blockers are already closed.

**Scope sizing for the decision:** Layer 1 (write) is the bulk. Layers 2–3 (surface + guide)
are small once Layer 1 exists. A defensible **minimum** pre-launch cut: ship Layers 1+2+3 for
`last_confirmed`/`invalidated_at` + the `last_used` stamp + the soft skill nudge; defer *only*
the automated staleness judgment (already a non-goal here).

## Open questions for product

1. Pre-launch (Jun 16) or first post-1.0 feature?
2. If pre-launch: full layers 1–3, or a thinner cut (e.g. frontmatter fields + surface, defer
   `last_used` index write)?
3. Command naming for memory-invalidate vs the existing session `invalidate`.
