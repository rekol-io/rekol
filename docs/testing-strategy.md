# Testing & release strategy ("the big test")

**Status:** living doc, started 2026-06-05 · relates to [#28](https://github.com/rekol-io/rekol/issues/28) (release versioning + pipeline), [#27](https://github.com/rekol-io/rekol/issues/27) (`rekol update`).

How rekol is tested, and what gates a release. There are two tiers: a fast,
deterministic **merge gate** that runs on every PR in cloud CI, and a heavier
**release gate** ("the big test") that gates promotion to a stable release and
includes a **memory-quality evaluation** which, by its nature, runs locally.

## Versioning & channels (`0.x.y`)

- **Edge (`y`):** every merged PR cuts a point release at `y`, gated by the Tier-1
  merge gate. The edge channel is for someone who wants a specific, non-critical
  fix before it's folded into stable.
- **Stable (`x`):** the pipeline-vetted release, gated by the Tier-2 "big test".
  This is what `rekol update` **recommends and defaults to**.
- SemVer `0.x.y` during alpha (pre-1.0); a GitHub Release per tag.

## Tier 1 — per-PR merge gate (cloud CI, fast & deterministic)

Runs on every pull request; a PR must pass before merge, and an edge `y` release
is cut from passing main.

- `ruff` · `ruff format --check` · `mypy` · `pytest` · `bats` install/uninstall.
- Hermetic: the suite clears `REKOL_HOME`/`MEMORY_HOME` and points all derived
  state (index cache, archive) at temp dirs, so it never touches a real machine's
  memory.
- Deterministic: same input → same result, so it's safe to gate merges on.

## Tier 2 — release gate ("the big test")

Runs when promoting to a stable `0.x.0`. It is the full picture, not just the
deterministic gate:

- Everything in Tier 1, plus:
- **Install rerun-safety:** run `install.sh` twice (fresh, then re-run) and assert
  a clean exit both times — because reconcile-as-update (#27) makes "re-run the
  installer" the update path, so a non-clean rerun is a release blocker. (This is
  the class of bug #26 was.)
- **Memory-quality evaluation** (see below) — the holistic "is retrieval actually
  good?" check.
- (Future) cross-platform install acceptance (clean macOS user account + macOS/Ubuntu CI).

## The memory-quality evaluation (a private companion repo)

The deterministic suite proves the machinery works ("search returns something");
it does **not** prove retrieval is *good* ("the right memory surfaces at the right
moment"). That relevance/quality question is evaluated separately.

**It lives in a separate, private companion repo.** Two things to be clear about:

1. **What's private is the data, not the methodology.** The repo is private only
   because it exercises *real personal memory data*. The **test types and what is
   being evaluated are not secret** and are documented openly here. Anyone should
   be able to understand *what* we evaluate; they just can't see the maintainer's
   actual memory contents.
2. **It is local by nature.** Because it depends on real, private memory data, it
   **cannot run in public cloud CI**. For now it runs **locally on the maintainer's
   machine** as a release gate. It may move to a dedicated machine with a curated,
   manually-built memory store in the future, when that becomes feasible; until
   then, this machine is the host.

### What the memory-quality eval covers (open — not secret)

- **Retrieval relevance:** for a representative query, does the right memory rank
  at the top? (precision/recall over a labeled query→expected-memory set.)
- **Proactive surfacing:** does the *right* memory surface at the right moment
  *without the agent knowing to search* — e.g. the "search-before-ask" behavior?
  Measured as precision/recall of proactive triggering, not just on-demand search.
- **Ranking quality:** temporal recency + layer-aware boosts produce sensible
  ordering; durable memory isn't unfairly buried; stale facts decay.
- **No-regression:** a set of known query→answer pairs keeps working release over
  release (catches silent-empty / stale-index regressions like #18/#22).

These are evaluation *types*; the concrete fixtures that contain real memory live
in the private repo.

## Open: build ↔ QA coordination about the big test

The build session and a QA/eval agent need a way to (a) let the build side *see
the results* of the private memory-quality runs without exposing the private data,
and (b) coordinate about Tier-2 outcomes for this repo. This is to be designed —
likely an extension of the existing neutral coordination-channel pattern (a shared
surface that carries results/verdicts, not raw private memory). Tracked alongside
#28.
