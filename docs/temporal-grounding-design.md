# Temporal grounding for REKOL — design

**Status:** Design — approved 2026-05-31, hardened the same day per an
architectural review (see "Revision note"). Pending implementation plan. Living
doc; update in place as the work evolves.

## Goal

Make REKOL's memory **time-aware**. Today recall is "not clean" because the
temporal signal that capture already records is dropped before it reaches
retrieval, so an invalidated or stale memory ranks identically to a live one.
This effort:

1. Connects the existing timestamp pipeline end-to-end so retrieval prioritizes
   current/live memory and excludes no-longer-true memory.
2. Makes REKOL ship its **own** temporal-awareness hook instead of depending on
   the external `mac_setup` time-tracking component.

### Scope guardrail (important)

This is the **cheap, local** version — timestamps + recency + invalidation
filtering. Do **NOT** build a temporal knowledge graph or a bi-temporal
reasoning engine. Do not break existing semantic search. Keep it simple and
local.

## Background — verified audit (2026-05-31)

Every claim below was re-checked against the current `src/rekol/` tree.

| Finding | Status | Evidence |
| --- | --- | --- |
| No time hook in this repo's `hooks/` | Confirmed | `sessionstart`/`posttooluse`/`sessionend` snippets only cat REKOL.md/MEMORY.md or reindex; the env-time block users see comes from the external `mac_setup` hook (`~/.local/share/mac_setup/hooks/inject-time-context.sh` + `record-stop-time.sh`, state at `~/.claude/session-env/time-context-<id>.json`). |
| Curated `chunks` table has no timestamp columns | Confirmed | `store.py` `SCHEMA_CHUNKS` = id, file_path, heading, line_start, line_end, text, tags_json, aliases_json, embedding. `files` = path, mtime, content_hash, indexed_at (machine times only). |
| Indexer drops the parsed timestamps | Confirmed | `indexer._index_one` never references created/updated/valid_from/invalidated_at; `replace_chunks_for_file` writes no timestamps. |
| `model.py` already parses all four + exposes `is_invalidated` | Confirmed | `MemoryFile.created/updated/valid_from/invalidated_at` (parsed in `parse_file`, lines 135-138), `is_invalidated` property. Capture side works; the data is thrown away at index time. |
| Ranking is cosine-only, no time term | Confirmed | `store.search()` → `np.argsort(-scores)`; `search_combined.py` → `sorted(key=-score)`. The de-prioritization the `model.py`/`cli_invalidate.py` docstrings promise was never implemented. |
| Session/transcript index carries + surfaces timestamps | Confirmed | `sessions/store.py` stores `timestamp_iso`/`timestamp_unix`; `cli_search` renders an absolute date per session hit (no relative phrasing; ranking has no time term). |

**Stale coordination note (corrected):** the build brief warned of an in-flight
effort making session embeddings semantic + wiring the SessionEnd auto-index.
That **merged in PR #1 (2026-05-31)** — already in `sessions/store.py` and
`install.sh`. It is a completed dependency, not a live conflict.

## Design decisions (locked)

- **Integration depth:** first-class, on by default — temporal context is part
  of the product; the installer wires it; it is documented/positioned publicly.
- **Hook implementation:** a hidden Python subcommand (`rekol _hook
  time-context` / `rekol _hook record-stop`), stdlib-only, soft-fail. No `jq`.
- **Invalidated memories:** excluded from default recall; retrievable via
  `rekol search --include-invalidated`, tagged and forced below all live hits.
- **`valid_from`:** future-dated memory filtered out by default.
- **Recency:** a mild, tunable, **layer-aware** boost intended to separate
  near-ties (durable layers `always/`/`knowledge/` exempt); see A4 for the
  (honest) limits of that intent.
- **Legacy cutover:** `mac_setup` gains a real uninstall for the two legacy
  components it installed; the `time-tracking/` component is retired. Sequence:
  `mac_setup uninstall` → `rekol install`. No general rekol migration tool.

## Workstream A — Curated temporal retrieval (highest value; a fix)

The storage and capture already exist; we are connecting an existing pipe. The
key seam decision from review: **`store.search()` stays a pure retrieval layer**
(fetch rows + timestamps + cosine). All temporal **policy** (filter, boost,
penalty) lives in a new `ranking.py` seam so the store keeps no config
dependency and the policy is unit-testable in isolation.

### A1. Schema — add timestamp columns to the curated index

In `store.py`, extend `SCHEMA_CHUNKS` with four nullable TEXT columns,
denormalized from the file's frontmatter onto each chunk (the sole curated
ranker does `SELECT ... FROM chunks` with no join; denormalizing keeps that a
single scan, and the index is disposable so the redundancy is harmless):

```
created       TEXT,   -- canonical ISO date (YYYY-MM-DD), nullable
updated       TEXT,   -- canonical ISO date (YYYY-MM-DD), nullable
valid_from    TEXT,   -- canonical ISO date (YYYY-MM-DD), nullable
invalidated_at TEXT   -- canonical ISO-8601 datetime (T-form), nullable
```

NULL means "no signal"; the `valid_from`→`created` fallback is **ranking-code
behavior, not a DB default** (SQLite has no cross-column default) — the schema
comment must not imply otherwise.

**Schema version:** stamp the DB with `PRAGMA user_version` (1 = original
schema, 2 = with timestamp columns). This is the migration trigger (A5) — cheap,
connection-durable, no table scan.

**Timestamp normalization (fixes a real format-mix bug).** PyYAML parses bare
frontmatter dates into `datetime.date`/`datetime.datetime`; `str()` on a
datetime yields a *space*-separated string, while `cli_invalidate.py`/
`cli_capture.py` write `T`-separated ISO. Left as-is the column would hold three
incompatible formats and `fromisoformat` would throw. Fix at **write/parse
time**, not read time: add a `_normalize_ts(value, *, date_only)` helper used by
`model.parse_file` so `created/updated/valid_from` are stored date-only
(`date.isoformat()`) and `invalidated_at` is stored full ISO-`T`
(`datetime.isoformat(timespec="seconds")`). The ranking parser then uses one
tolerant parse (try date, then datetime); any unparseable/missing value is
treated as "no temporal signal" and never hides a memory.

### A2. Carry the timestamps through indexing

`indexer._index_one` already holds a `MemoryFile` (with normalized timestamps).
Pass them into `store.replace_chunks_for_file(...)`, which writes them onto every
chunk row for that file. No new parsing — stop discarding what is already parsed.

### A3. Surface them in search output

- `store.search()` — add the four columns to the `SELECT` and to each result
  dict. Return the raw cosine as **`cosine_score`** (not the post-boost value).
- `ranking.py` — produces `final_score` (= `cosine_score` + recency boost −
  invalidation penalty) used for ordering; keeps both fields on each hit.
- `cli_search.py` — JSON includes `created`/`updated`/`valid_from`/
  `invalidated_at`, `cosine_score`, and `final_score`. Text output shows
  provenance (e.g. `updated 2026-05-14`; `[INVALIDATED 2026-03-01]` only under
  `--include-invalidated`). Exposing both scores keeps tests deterministic
  (assert on `cosine_score`, which is wall-clock-independent) and lets the model
  see why ordering shifted.

### A4. Temporal ranking (in `ranking.py`, not in the store)

`apply_temporal_ranking(hits, cfg, today, include_invalidated) ->
(ranked_hits, filtered_count)`, applied after `store.search()` returns cosine
scores, before `top_k` truncation:

1. **Invalidation filter (default on).** Drop hits whose `invalidated_at` is
   set, unless `include_invalidated=True`. When included, they are kept, tagged,
   and given a fixed penalty applied after the recency boost so they sort below
   every live hit (matching "never top under `--include-invalidated`").
2. **`valid_from` filter (default on).** Drop hits whose `valid_from` (or
   `created` when `valid_from` is absent) parses to a date after `today`. Both
   `today` and the comparison are date-granularity; missing/unparseable → keep.
3. **Recency boost (layer-aware).** `final = cosine + w·exp(-age_days /
   halflife)`, age from `updated` (fallback `created`); no usable date → zero
   term (no penalty). **Chunks whose layer (the first path component under
   `$MEMORY_HOME`, e.g. `knowledge/`) is in `temporal_recency_exempt_layers` get
   a zero recency term** — `always/` and `knowledge/` are durable/time-insensitive,
   so among still-valid hits they rank on pure cosine (invalidation and
   `valid_from` filters still apply to them; only the recency tiebreak is
   suppressed). `filtered_count` (hits removed by 1+2) is returned alongside.

**Honest limit (review correction):** a `w=0.03` boost is *not* a structural
guarantee of "never overrides semantic relevance" — a 0.03 cosine gap is
meaningful for BGE-small, so on some corpora a fresh hit can edge out a slightly
more-relevant old one. The defaults are sized to make this *unlikely*, not
impossible; tune via config. Acceptance criteria are worded accordingly.

### A5. Migration / rebuild (explicit, never silent)

The store can DROP tables but cannot re-embed (it has no `Embedder`/
`memory_root` — only `Indexer` does). So "auto rebuild on open" is impossible
and is removed from the design. Instead, mirror the sessions store's explicit
pattern:

- `store.needs_schema_migration()` checks `PRAGMA user_version` (or
  `PRAGMA table_info(chunks)`).
- `rekol index rebuild` / `rekol index update` call it and, if migration is
  needed, run the full `Indexer` rebuild (which can re-embed) and bump
  `user_version` to 2.
- Read-only commands (`rekol search`/`capture`/`invalidate`/`propose`) call it
  and, on mismatch, raise a typed `CuratedSchemaOutdatedError` whose handler
  prints an actionable message ("curated index schema is out of date — run
  `rekol index rebuild`") and exits non-zero — **never** silently wipe the index
  or return empty results from freshly-dropped tables.

`$MEMORY_HOME` markdown is never touched; the index is disposable.

### A6. Config knobs (flat keys — the loader requires it)

`config.py` whitelists only keys present in `DEFAULTS` (`config.py:112`) and
builds a **flat** `Config` dataclass, so a nested `temporal:` block would be
silently dropped. Add the knobs as **flat top-level** keys in `DEFAULTS`, the
`Config` dataclass, and the constructor (same `bool()/float()/int()` coercion as
the existing fields):

```
temporal_exclude_invalidated:    true   # bool
temporal_respect_valid_from:     true   # bool
temporal_recency_weight:         0.03   # float — small; only near-ties
temporal_recency_halflife_days:  180    # number — exponential half-life
temporal_recency_exempt_layers:  ["always", "knowledge"]  # list[str] — durable layers exempt from the recency boost
```

(The exempt-layers value is a flat top-level key with a list value — handled by
the same whitelist+coercion path, coerced via `list(...)`.)

(`rekol.config.yaml`, with the existing `memory.config.yaml` back-compat read.)
`rekol search` gains `--include-invalidated` (overrides the exclude default for
one query). Defaults leave non-temporal results visually unchanged.

### A7. Promotion-candidate correctness

`is_promotion_candidate` currently fires when memory returns zero hits — but if
all hits were filtered out as invalidated/future-dated, that would falsely tell
the user to capture a memory they already have. Thread the `filtered_count` from
A4 through `search_combined` to `CombinedSearchResult`; only flag a promotion
candidate when `memory_hits == 0` **and** `filtered_count == 0`.

## Workstream B — Port the time hook into REKOL (self-contained)

### B1. Hidden `_hook` subcommand group

New module `src/rekol/cli_hooks.py`, registered in `cli.py` as a hidden group:

- `rekol _hook time-context` — UserPromptSubmit. Reads the hook JSON payload
  from stdin (`session_id`), emits an `<env-time>` block (local + UTC; elapsed
  since last user message and last assistant response), maintains the state file.
- `rekol _hook record-stop` — Stop. Writes the completion epoch into the state
  file's `last_assistant_epoch`, preserving `last_user_epoch`; no stdout.

Stdlib only (`json`, `datetime`, `os`, `pathlib`, `sys`, `re`) — no heavy rekol
imports (no embeddings/model load) so per-turn startup stays light.

**State file:** `~/.claude/session-env/time-context-<session_id>.json`,
`{"last_user_epoch": int, "last_assistant_epoch": int | null}` — same location/
shape as the retired mac_setup hook (compatible existing state).

**`session_id` validation (security).** Before using `session_id` in a path,
assert it matches `^[A-Za-z0-9_-]+$`; on mismatch, soft-fail to a degraded
envelope. Prevents path traversal (`..`/`/`) from a malformed payload — a
pre-existing gap in the bash version, fixed here.

**Soft-fail by design:** any error (missing/invalid session_id, corrupt state,
bad payload) emits a degraded `<env-time>` (time-context) or nothing
(record-stop) on stderr and **always exits 0**.

### B2. Hook snippets + installer wiring

- New `hooks/userpromptsubmit-snippet.json` (→ `rekol _hook time-context`) and
  `hooks/stop-snippet.json` (→ `rekol _hook record-stop`), mirroring the existing
  snippet format.
- `install.sh` gains **Step 7E** (UserPromptSubmit) and **Step 7F** (Stop),
  cloning Step 7D: `DO_HOOK` gate, `jq` merge, idempotency keyed on the
  `rekol _hook ...` command, independent timestamped settings.json backup,
  journal logging, `--no-hook` opt-out.

### B3. Double-injection guard (and its dead-end, fixed)

REKOL's env-time block must be the only one. Step 7E's check also detects a
legacy `inject-time-context.sh` entry. Review caught a dead-end: if
`rekol install` runs **before** `mac_setup uninstall`, the guard skips the rekol
hook, then uninstall removes the legacy one → **no hook at all**. So when the
guard detects a legacy entry it must NOT print "already present — no-op"
(implies success); it prints an actionable warning: *"Legacy mac_setup time hook
detected — rekol's time hook was NOT installed. Run `mac_setup --uninstall`,
then re-run `rekol install`."* The cutover doc states the same re-run rule.
Happy-path order (uninstall → install) avoids this entirely.

## Workstream C — Optional polish

Relative phrasing ("3 weeks ago") beside absolute dates in session + curated
hits (`cli_search.py` rendering only — no ranking change). Lowest priority.

## Cutover — retire the mac_setup install (Option 2)

The legacy memory_tools + time-tracking were installed by `mac_setup`, which has
no uninstall (only `.bak` settings restore). Give `mac_setup` a real uninstall
for the two components it owns, then cut over to rekol.

### What must be removed (preserving data)

- **memory_tools:** 6 shims in `~/bin` (`memory-search`, `memory-capture`,
  `memory-index`, `memory-invalidate`, `memory-propose`, `claude-session-index`);
  the editable venv at `~/.local/share/memory-tools/.venv`; the memory hooks in
  `settings.json` (SessionStart index + PostToolUse `auto-reindex.sh`).
- **time-tracking:** the 2 hooks in `settings.json` (UserPromptSubmit + Stop),
  the 2 scripts in `~/.local/share/mac_setup/hooks/`, and `~/.claude/session-env/`
  state.
- **Preserve:** `$MEMORY_HOME` markdown (`~/Dropbox/memory`). The uninstall must
  enumerate exactly what it deletes (shims, venv, hook entries by command
  string, session-env state) and must never touch a path under `$MEMORY_HOME`.
  The disposable index may be deleted (rekol rebuilds it).

### Uninstall mechanism

- Add `--uninstall` to `mac_setup/memory-tools/install.sh` and
  `mac_setup/time-tracking/install.sh` (the inverse of each install): remove
  shims/venv, and `jq`-delete the component's own hook entries from
  `settings.json` keyed on their command strings, after a timestamped backup.
  (Keying on command substrings is fragile — pin the exact command strings the
  installers wrote, and back up before mutating so a mismatch is recoverable.)
- Add `mac_setup/scripts/uninstall.sh` (or `setup.sh --uninstall`) to orchestrate
  both. Idempotent and dry-run-able, matching install-script conventions.

### Sequence

1. Build the uninstall + rekol Workstreams A/B (and tests).
2. Per machine (personal, then work): `mac_setup … --uninstall` → verify
   shims/hooks/venv gone, `$MEMORY_HOME` intact.
3. Run the standalone rekol `install.sh` → memory + the new Python time hooks.
   (If install was run before uninstall, re-run it now — see B3.)
4. Retire from `mac_setup`: delete the `time-tracking/` component and its
   phase-5 wiring; neutralize phase-3 so `setup.sh` no longer (re)installs legacy
   memory_tools. Remove `memory-tools/` once uninstall has run everywhere.
5. Verify acceptance criteria.

## Data flow

```
capture/invalidate → frontmatter (created/updated/valid_from/invalidated_at)
   → model.parse_file (parses + _normalize_ts to canonical ISO)
   → indexer._index_one  ──[A2: carried]──▶ store.replace_chunks_for_file
   → chunks table (A1: new columns, user_version=2)
   → store.search  →  rows + timestamps + cosine_score   (pure retrieval)
   → ranking.apply_temporal_ranking  ──[A4: filter + boost + penalty]──▶
        ranked hits (final_score) + filtered_count
   → search_combined / cli_search (A3 fields; A7 promotion gate; C phrasing)

UserPromptSubmit ─▶ rekol _hook time-context ─▶ <env-time> + state file
Stop             ─▶ rekol _hook record-stop  ─▶ state file (assistant epoch)
```

## Error handling

- **Hooks:** soft-fail, always exit 0 (B1); degraded `<env-time>` on any error;
  `session_id` validated before path use.
- **Schema migration:** explicit, never silent — read-only commands raise/instruct,
  rebuild commands re-embed (A5); markdown never touched.
- **Date parsing:** malformed/absent dates = "no temporal signal" (no filter
  drop, zero recency term) — a bad date must never hide a live memory.
- **Uninstall:** back up `settings.json` before mutation; refuse any path under
  `$MEMORY_HOME`; idempotent (safe to re-run).

## Testing

- **A (retrieval):** after rebuild the columns exist (`user_version==2`);
  `_index_one` persists normalized timestamps; `_normalize_ts` collapses the
  date/datetime/T/space format mix to one canonical form; `apply_temporal_ranking`
  excludes invalidated by default and includes+penalizes them under the flag;
  not-yet-`valid_from` memory is never returned; recency reorders only near-ties
  (a strongly-relevant old memory still beats a weakly-relevant new one); recency
  is suppressed for `always/`+`knowledge/` (a durable `knowledge/` hit isn't
  out-ranked by a newer `topics/` note on a near-tie); **a
  query whose only matches are invalidated/future returns 0 hits with
  `filtered_count>0` and does NOT flag a promotion candidate**; read-only
  commands error (not wipe) on an out-of-date schema; **non-temporal results
  unchanged** (assert on `cosine_score`).
- **B (hook):** `time-context` emits a well-formed `<env-time>` with correct
  deltas across user→assistant→user; `record-stop` updates only
  `last_assistant_epoch`; soft-fail (exit 0) on missing/corrupt payload and on a
  path-traversal `session_id`; install merge idempotent; guard emits the warning
  (not "no-op") on a legacy entry.
- **Cutover:** uninstall removes the enumerated artifacts and leaves
  `$MEMORY_HOME` intact (integration/dry-run check).

## Acceptance criteria

- For the same query, an invalidated memory does **not** rank at/above a live
  one (excluded by default; tagged + never top under `--include-invalidated`);
  not-yet-`valid_from` memory isn't surfaced.
- Recency *usually* lets newer memory win ties without overriding a clearly
  more-relevant older hit (tunable; not a hard guarantee); it is layer-aware —
  `always/`/`knowledge/` hits are exempt from the recency term.
- Search output (text + JSON) includes `created`/`updated`/`valid_from` plus
  `cosine_score` and `final_score` for curated hits.
- A query matched only by invalidated/future memory returns 0 live hits and does
  **not** raise a promotion candidate.
- `rekol index rebuild` populates the new fields and bumps `user_version`;
  read-only commands instruct (never silently wipe) on an old schema.
- A fresh session gets an `<env-time>` block from REKOL's own hook with **no
  mac_setup dependency**, and there is **exactly one** such block.
- Non-temporal semantic search results are unchanged; new tests cover ranking,
  promotion gating, hook wiring, soft-fail, and `session_id` validation.
- `mac_setup --uninstall` removes the legacy artifacts and preserves
  `$MEMORY_HOME`; after `rekol install`, rekol owns memory + temporal context.

## Constraints / principles

- Preserve the data/code/index decoupling; the index is disposable → no lossy
  migration; a rebuild is fine.
- No temporal knowledge graph; no temporal-reasoning overclaim.
- Keep the curated indexer's content-hash incrementality working after the
  schema change.
- Build/test against the `rekol` package (`src/rekol/`); the live machine still
  runs legacy `memory-tools`, so install/deploy `rekol` to dogfood the change.

## Sequencing & coordination

A (retrieval fix) → B (hook port) → cutover → C (optional polish). A and B are
independent. The session-embeddings/SessionEnd dependency is already merged
(PR #1). Both B and that work touch `install.sh`, but only additively.

## Revision note — architectural review (2026-05-31)

Hardened after an architectural review of the first draft. Changes folded in:
flat config keys (the loader silently drops unknown/nested keys); explicit
schema migration via `PRAGMA user_version` with read-only commands instructing
rather than the impossible/destructive "rebuild on open"; write-time timestamp
normalization to fix a date/datetime format mix; `store.search()` kept as pure
retrieval with temporal policy moved to a `ranking.py` seam; separate
`cosine_score`/`final_score` for deterministic tests and honest "unlikely, not
never" recency wording; `filtered_count` to fix a promotion-candidate false
positive; `session_id` path-traversal validation; and a warning (not a silent
no-op) when the double-injection guard hits the wrong-order dead-end.

## Open questions

None blocking. `temporal_recency_weight` (0.03) and `temporal_recency_halflife_days`
(180) are starting values to validate empirically; config-tunable, no design
impact.
