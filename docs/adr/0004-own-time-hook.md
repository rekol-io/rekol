# ADR 0004: REKOL ships its own time-context hook

- **Status:** Accepted — implemented in temporal grounding (PR #3).
- **Date:** 2026-05-31

## Context

Per-turn wall-clock / elapsed-time awareness (the injected `<env-time>` block)
previously came from an external `mac_setup` component (bash + `jq` hooks).
REKOL is becoming a standalone, cross-platform OSS tool; depending on a separate
personal repo for a core "context layer" feature is wrong, and `jq` is a runtime
dependency / portability friction point for users.

## Decision

Port the hook in-house as a hidden, **stdlib-only**, soft-fail Python subcommand:
`rekol _hook time-context` (UserPromptSubmit) + `rekol _hook record-stop` (Stop),
wired by `install.sh`. No `jq`; pytest-testable; one language across the product.
A double-injection guard refuses to add a second injector if the legacy
`mac_setup` hook is still present.

## Consequences

- REKOL is self-contained; `install.sh` owns all hook wiring (Steps 7E/7F).
- Soft-fail by design: any error degrades and exits 0 — a hook never blocks a
  prompt. `session_id` is validated against `^[A-Za-z0-9_-]+$` before path use.
- Cost: lightweight Python startup per turn (stdlib only, no model load) — accepted
  over the marginally faster bash+jq for portability and testability.
