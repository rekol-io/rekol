# ADR 0003: Durable layers get the full (un-decayed) recency boost

- **Status:** Accepted — implemented in temporal grounding (PR #3).
- **Date:** 2026-05-31

## Context

REKOL's memory is layered. `always/` and `knowledge/` are durable and
time-insensitive; `when/` and `topics/` are situational. A uniform recency boost
(ADR 0001) would let a recent ephemeral note out-rank a years-old-but-valid
`knowledge/` decision on a near-tie.

The intuitive fix — **exempt durable layers from the boost (zero term)** — is
wrong, and we caught this during implementation: zero-boost doesn't protect a
durable hit, because a fresher non-exempt hit still gets *its* boost and edges
the durable hit out at equal cosine. Zero-boost changes nothing for the exact
case it was meant to fix.

## Decision

Treat exempt layers (`temporal_recency_exempt_layers`, default
`["always", "knowledge"]`) as **always-current**: they receive the full,
un-decayed boost `w` (not zero). A durable hit then competes on equal footing and
wins the near-tie by the fresher hit's small decay gap.

## Consequences

- Durable memory is not out-aged by recency. Invalidation and `valid_from`
  filters still apply to exempt layers — only the recency tiebreak changes.
- Because exempt memory never decays, it needs a freshness check, or a
  wrong-but-durable fact sits atop recall forever. Hence `rekol review` + a
  SessionEnd nudge + an inline `[review?]` tag re-confirm overdue durable memory.
