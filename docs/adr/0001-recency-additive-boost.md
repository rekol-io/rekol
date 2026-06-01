# ADR 0001: Additive recency boost over re-ranking

- **Status:** Accepted — implemented in temporal grounding (PR #3, merge `a5843a4`).
- **Date:** 2026-05-31

## Context

Curated-memory retrieval was cosine-only. We want newer/live memory to win
near-ties without building a temporal knowledge graph or a bi-temporal reasoning
engine — an explicit non-goal (that is Zep/Letta territory; keep it cheap and
local). The mechanism must be simple, predictable, and tunable.

## Decision

Apply a small **additive** recency term on top of the cosine score:
`final = cosine + w·exp(-age_days / halflife)` (defaults `w=0.03`,
`halflife=180d`, both config-tunable in `rekol.config.yaml`). Ranking stays a
filter + a mild boost — not a re-ranker, not a learned model.

## Consequences

- Recency mainly separates near-ties; semantic relevance dominates. The guarantee
  is "unlikely to override," **not** "never" — a 0.03 cosine gap is meaningful for
  BGE-small, so on some corpora a fresh hit can edge out a slightly-more-relevant
  older one. Tunable to taste (`w=0` disables it).
- `store.search` returns `cosine_score` and `final_score` separately, so a ranking
  shift is explainable and tests assert on the wall-clock-independent cosine.
- No graph, no model, no training; fully reversible via config.
