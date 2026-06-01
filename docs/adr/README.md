# Architecture Decision Records

Short, dated, immutable records of durable design decisions — the *why* the code
can't express. One decision per file: `NNNN-kebab-title.md`.

Format: **Status** · **Context** · **Decision** · **Consequences**. Once a
decision ships it is `Accepted`; a later reversal gets a *new* ADR that
supersedes it — don't rewrite history.

## Doc-status convention

Design docs and ADRs mark planned-vs-implemented explicitly, so fluent prose is
never mistaken for shipped behavior:

- **Proposed** — described, not built.
- **Accepted / Implemented in `<commit|vX>`** — shipped.
- **Partial** — some of it ships; the gap is named.

Specs that describe code are verified against the code, not trusted because they
read well. (This convention exists because an early design doc + docstrings
asserted "retrieval de-prioritizes invalidated memories" before that behavior was
ever built — see ADR 0002.)

## Index

- [0001](0001-recency-additive-boost.md) — additive recency boost over re-ranking
- [0002](0002-invalidated-excluded-by-default.md) — invalidated excluded by default, opt-in to include
- [0003](0003-layer-aware-full-boost.md) — durable layers get the full (un-decayed) boost
- [0004](0004-own-time-hook.md) — REKOL ships its own time hook
