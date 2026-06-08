# Memory Index (always-on)

This file is re-injected into every Claude session. Pointers below are phrased as triggers — read the referenced file when the trigger matches the user's request.

New here? Read [knowledge/anatomy-of-good-memory.md](knowledge/anatomy-of-good-memory.md) once — it explains what each layer (`always/`/`when/`/`topics/`/`knowledge/`) is for and what makes a memory worth keeping.

## Before any activity

- **Before touching a repo** — [when/when-touching-repos.md](when/when-touching-repos.md)
- **Before scoping an ops task** — [when/when-touching-environments.md](when/when-touching-environments.md) (apply to all environments unless told otherwise)
- **When asked where a URL, config, or canonical value lives** — consult `topics/<noun>.md` first; run `rekol search` if unsure.

## Who I am

- [always/identity.md](always/identity.md)

## Understanding (read on demand)

- Reasoning behind durable decisions lives in `knowledge/` — e.g. [knowledge/why-we-chose-x.md](knowledge/why-we-chose-x.md). Surface it with `rekol search` when a "why did we…" question comes up.

## Protocol

- For any noun in the user's message matching a topic, read `topics/<noun>.md`.
- Fallback: `rekol search "user's phrasing" --top 5 --json`.
- When the user says "remember this" or corrects you, follow the capture protocol in the `rekol` skill (invoke `/rekol`, or `/memory` for the back-compat alias).

## Scope

Memory files may carry `scope: private` (the default) in their frontmatter.
This field is reserved for a future shared-team store; all values are accepted
but have no effect in v0.1.
