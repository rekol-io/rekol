# Onboarding v2 — implementation plan & architecture decisions

Wave 2 of the onboarding epic (#46), per product's FINAL consolidated spec
(`rekol-coordination/from-business/20260608-0008`). Wave 1 (T1–T7, merged) shipped the CLI
`rekol init` fork + the no-LLM corpus propose (#40) + the resumable bootstrap (#41) + safeguards
(#45). This wave makes onboarding **assistant-led**, reworks the baseline into **directive
scaffolds**, and adds **include-scope** (durable external-folder coverage). This doc records the
delta triage, the architectural calls, and the build plan so the work is resumable.

## Delta triage (code vs. workflow-support)

| Area | Issue | Kind | Notes |
|---|---|---|---|
| T8 discovery + include-scope | #63 | **CODE** (large) | detect+junk-filter, deny-list, extend indexing to included dirs, coverage %, deprecate `import` |
| T3 additions | #62 | **CODE** | scalable review UX (bulk-approve/stop-early), promote→REKOL.md, refine pass |
| invalidate-as-forget | #64 | **CODE** | wire forget semantics + per-session granularity + caveat |
| T4 scaffolds + gap-fill | #60 | CODE + content | directive inert scaffolds, gap-fill seeding, anatomy→docs |
| skill behavioral rules | #60/#61 | content (skill md) | search-don't-guess, grounding, hedging, capture — in `skill/rekol/skill.md` |
| T1 assistant-led `/rekol init` | #59 | content (skill md) + small CLI | new orchestrating skill; CLI stays as mechanical parity |
| T5 copy + scope | #61 | content (copy/docs) | positive-only, no-negatives, Claude-Code scope (incl Desktop) |
| IDE hook verification | #65 | verification | confirm SessionStart fires in VS Code/JetBrains |
| T2 / T3 core / T6 / T7 | — | **DONE** (merged) | satisfied by wave-1 + review fixes |

## Architectural decisions (the awkward bits, resolved)

1. **"Extend indexing to included dirs forever"** → included dirs are stored in config + re-indexed
   on **each `session-index` run**, governed by the **deny-list**. New files under an included dir
   are picked up on the next run. *No separate filewatcher daemon in this cut* (deferred; flagged).
2. **"Retire `import`"** → **deprecate, don't remove**. `import` keeps working with a "prefer
   include-scope" notice, so existing `backstage-ai-archive` synthetic transcripts don't break.
3. **`invalidate` granularity** → **per-session** (matches the archive/index unit); documented.
   Honest caveat in copy: the original `.jsonl` is owned by Claude Code — we forget from rekol.
4. **T1 onboarding** → **skill-led primary** (a new skill orchestrating steps 0–5, shelling to CLI
   primitives + doing bootstrap reasoning in one context). CLI `rekol init` stays as scriptable
   parity. UX: silent-skip empty steps, magnitude-aware index, adaptive close, "change anytime."
5. **Baseline scaffolds** → **directives, not placeholder data** ("record the user's name here as
   you learn it", never `[name]`); **gap-fill** (keep the user's existing layers, seed only missing).
   Behavioral rules live in the skill (hybrid home, auto-improves for all).

## Build plan — parallel agents (worktree-isolated, non-overlapping files)

Wave 2A (parallel, off `main`):
- **A1 skill behavioral rules** — `skill/rekol/skill.md` (search-don't-guess, grounding, hedging, capture). #60/#61
- **A2 T4 scaffolds + gap-fill** — `template/**`, `onboarding/starter_pack.py`, `docs/anatomy-of-good-memory.md`. #60
- **A3 T5 copy + getting-started** — `README.md`, `install.sh`, `docs/getting-started.md`. #61
- **A4 T3 additions** — `bootstrap.py`, `cli_bootstrap.py`, tests. #62
- **A5 T8 include-scope** — new include module, `config.py`, `cli_session_index.py`, coverage, deprecate `cli_import`. #63 *(big)*
- **A6 invalidate-as-forget** — `cli_invalidate.py`, copy. #64

Then: integration-verify → adversarial review → fix → merge stack.
Wave 2B (after 2A merges): **A7 T1 assistant-led onboarding skill** (#59) — orchestrates the merged
primitives; + cli_init UX refinements. Then verify → merge → **install on this mac**.

File-ownership is partitioned so the only shared-file risk (`skill/rekol/skill.md`) has a single
owner (A1); A3 owns `install.sh`/`README`/`docs/getting-started`; A2 owns `template/`/anatomy doc.

## Flags / open questions
- **IDE SessionStart hook** (#65) — can't reliably drive an IDE headless; will hand QA a documented test.
- **Real-time watching of included dirs** — deferred (periodic re-index only); revisit post-launch.
- **`invalidate` granularity** — confirm per-session against the current impl during A6.
- **Coverage %** denominator (discoverable set) — align with QA's ambient harness during A5.
