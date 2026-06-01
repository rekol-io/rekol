# ADR 0002: Invalidated memories excluded from default recall

- **Status:** Accepted — implemented in temporal grounding (PR #3).
- **Date:** 2026-05-31

## Context

`rekol invalidate` marks a memory's facts as no-longer-true (`invalidated_at`)
without deleting it — the historical claim stays useful ("what did I believe in
March?"). Earlier docstrings and a design doc said retrieval "should/can
de-prioritize" invalidated memories, but ranking never did: they ranked
identically to live ones. (This claim-vs-code gap is the motivating example for
the doc-status convention in [README](README.md).)

## Decision

Exclude invalidated memories from default recall. They remain retrievable via
`rekol search --include-invalidated`, where they are **tagged** `[INVALIDATED]`
and **hard-ranked below every live hit** (a fixed penalty applied after the
recency boost).

## Consequences

- Default recall is always-live — a no-longer-true fact never surfaces as if
  current. History is opt-in, never silent.
- A query matched only by invalidated/future memory returns zero live hits;
  retrieval reports a `filtered_count` so the "no memory — consider capturing"
  hint is not falsely raised.
- Alternative considered: keep them in results but down-weight. Rejected — a
  stale item could still land in `top_k` and mislead.
