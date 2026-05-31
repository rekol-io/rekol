# Memory Index (always-on)

This file is re-injected into every Claude session. Pointers below are phrased as triggers — read the referenced file when the trigger matches the user's request.

## Before any activity

- **Before touching a repo** — [when/when-touching-repos.md](when/when-touching-repos.md)
- **Before scoping an ops task** — [when/when-touching-environments.md](when/when-touching-environments.md) (apply to all N environments unless told otherwise)
- **When asked about infra URLs or config** — consult `topics/<topic>.md` first; run `rekol search` if unsure.

## Who I am

- [always/identity.md](always/identity.md)

## Protocol

- For any noun in the user's message matching a topic, read `topics/<noun>.md`.
- Fallback: `rekol search "user's phrasing" --top 5 --json`.
- When the user says "remember this" or corrects you, follow the capture protocol in the `memory` skill.
