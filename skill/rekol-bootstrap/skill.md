---
name: rekol-bootstrap
description: Cold-start memory bootstrap. Distil recurring "we always X / repos live in Y / we decided Z" from indexed Claude Code history into curated, review-gated REKOL memory. Trigger on "bootstrap my memory", "seed rekol from my history", "what should I remember from past sessions", or right after a first-run `rekol session-index`. Resumable: a reaped run picks up where it left off.
---

# rekol-bootstrap

The cold-start memory bootstrap. Indexing makes your Claude Code history
*searchable*; this routine *distils* it — turning recurring instructions,
preferences, and decisions buried in past transcripts into ambient, curated
REKOL memory. **You** (the user's own Claude) are the precision pass: the engine
recalls and batches candidates with no LLM; you cluster, classify, fix
frontmatter, and present an approve/edit/skip gate before anything is captured.

This is the LLM-precision half of the onboarding flow. The recall half (T2)
already ran inside `rekol bootstrap` — it reused transcript search to surface
candidate instruction-bearing messages, deduped them against existing memory, and
narrowed to a bounded, reviewable shortlist.

## Non-negotiables

- **Review-gated.** You NEVER auto-write memory. Every captured item passes a
  human approve/edit/skip gate. `rekol bootstrap` itself never captures — it only
  recalls, scopes, batches, and checkpoints. The *only* command that writes memory
  is `rekol capture`, and you run it only on an approved item.
- **Conservative.** Capture only high-confidence, recurring, durable facts —
  "would a future session genuinely benefit from knowing this?" Skip trivia,
  one-offs, and anything you can't verify against its cited transcript.
- **Provenance.** Each candidate carries its source (session id, transcript file,
  line). Open the source if a candidate is borderline; don't trust recall blindly.
- **Resumable.** The run is long over a big corpus and WILL get interrupted (a
  SessionEnd reap, a `^C`, a crash). The engine checkpoints per batch; you must
  drive it batch-by-batch so an interrupted run resumes without reprocessing.

## The loop (harness-agnostic — no `/goal` required)

Run these `rekol` commands directly. The whole routine is just: plan once, then
process one batch at a time, marking each done so a resume skips it.

### 1. Plan (or resume)

```
rekol bootstrap --status
```

- If it reports `run_id: null` (no run in progress), **plan a fresh run**:
  ```
  rekol bootstrap
  ```
  This recalls + scopes + batches and writes a per-batch review file under the
  local-only `pending-review/` cache dir (the command prints the exact path) for
  each batch, plus a checkpoint. The default
  scope is a **bounded** recent window (last ~90 days, capped sessions) so the
  first run stays fast and reviewable. Widen explicitly when the user asks:
  - `rekol bootstrap --all-time` — the entire corpus
  - `rekol bootstrap --scope-days 30` — a tighter window
  - `rekol bootstrap --scope-project rekol --scope-project infra` — chosen projects
  - `rekol bootstrap --max-sessions 50` — cap the corpus size
- If a run **is** in progress, continue it: `rekol bootstrap --resume`. If you
  need a different scope, the engine refuses a silent scope change — start over
  explicitly with `rekol bootstrap --reset [scope flags]`.
- If it reports "no candidates in scope", tell the user there's nothing to
  bootstrap yet (a fresh install needs `rekol session-index --full` first, or the
  scope is too narrow — offer `--all-time`).

### 2. Process each pending batch

Loop while there are pending batches:

```
rekol bootstrap --next
```

This prints the path of the next not-yet-done batch's review file (empty output
means the run is complete — stop). For that file:

1. **Read** the batch review file. Each candidate has its content, a **suggested
   layer**, provenance, and a ready-to-edit `rekol capture` line.
2. **Cluster + classify.** Group near-duplicate candidates; pick the right layer
   for each (see "Layers" below). The suggested layer is a heuristic seed —
   correct it. Conservatively default anything you're unsure about to
   `knowledge` (non-ambient), never `always`.
3. **Search-before-write.** For each kept candidate, `rekol search "<gist>" --top
   5 --json` first. If a near-duplicate memory exists, **update** it (edit the
   file + `rekol index update`) instead of creating a parallel one.
4. **Present the approve/edit/skip gate** to the user — show the candidate, your
   proposed layer + filename + frontmatter, and its source. Wait for the user's
   decision. Do not capture on your own initiative.
5. **Capture approved items** with the corrected layer/frontmatter:
   ```
   rekol capture --layer <always|when|topic|knowledge> --file <name>.md \
     --name "..." --description "..." [--tags a,b] [--aliases x,y] \
     [--project <slug>]
   ```
   Capture is itself review-safe: it refuses near-duplicates (cosine conflict
   check) unless you pass `--force`, and it reindexes automatically.
6. **Update `REKOL.md`** only for items that genuinely deserve always-on status
   (a pointer/trigger line), respecting the `always/` 8 KB cap.
7. **Mark the batch done** so a resume skips it:
   ```
   rekol bootstrap --mark-done <batch-id>
   ```
   The `<batch-id>` is the one named in the review file heading (e.g.
   `project-rekol`). Do this only after you've finished capturing/skipping the
   whole batch — it is the checkpoint that makes the run resumable.
8. **Delete the batch review file** once done (it's a scratch artifact).

Then go back to `rekol bootstrap --next` for the next batch. When `--next` prints
nothing, the run is complete — summarise to the user what was captured (one line
per item) so they can audit.

## Layers (classify each candidate)

- **`always/`** — standing, always-on instructions that should be ambient in
  every session ("always run ruff before committing", "never force-push main").
  Ambient memory is expensive context — reserve it for genuinely cross-cutting
  rules. 8 KB hard cap.
- **`when/`** — activity-scoped rules ("when deploying, pull images first"). File
  as `when-<activity>.md`.
- **`topics/`** — noun-scoped facts ("the rekol repo lives in ~/github/rekol",
  "Prometheus is the canonical metrics source"). File as `topics/<noun>.md`.
- **`knowledge/`** — durable reference facts that don't fit the above and
  shouldn't be ambient. The safe default for anything unclassifiable.

Project-specific facts: pass `--project <slug>` to scope a memory under
`projects/<slug>/<layer>/`. The batch id's `project-<slug>` suffix is your hint.

## Resumability — how it works (and why you drive it this way)

`rekol bootstrap` writes a small JSON checkpoint
(`.bootstrap-state.json` inside the local-only `pending-review/` cache dir) recording the ordered batch
plan, which batches are done, and the scope the plan was built from. Writes are
atomic, so a crash never corrupts it. Because you `--mark-done` each batch only
after finishing it, an interrupted run resumes at the first unfinished batch:

- A reaped/killed session: next time, `rekol bootstrap --status` shows the
  remaining batches; `rekol bootstrap --next` hands you the first one. Nothing
  already captured is re-offered.
- The per-batch review files are written up front, so even if the engine dies
  immediately after planning, the candidates are on disk as reviewable artifacts.

**Do not** try to process the whole corpus in one giant pass — that defeats the
checkpointing. One batch, mark done, next.

## Optional: `/goal` durability accelerant

If the user has the `/goal` workflow available and the corpus is large, wrapping
this loop in a `/goal` makes the long run more durable (auto-resume across
reaps). This is **optional** — the loop above is fully harness-agnostic and works
without `/goal`. If you don't have `/goal`, just run the loop directly; the
engine's checkpoint already gives you resumability.
