---
name: memory
description: Persistent memory at $MEMORY_HOME. Trigger on "remember"/"save"/"forget", on a user correction, on a noun matching topics/<noun>.md or activity matching when/when-<activity>.md, or when a question might have a canonical source.
---

# memory

Memory root: `$MEMORY_HOME`. Layers: `always/`, `when/`, `topics/`, `knowledge/`. Index: `MEMORY.md` (always-on), `.index/INDEX.md` (auto-generated).

## Retrieve

1. `MEMORY.md` already in context — scan for trigger pointers.
2. Match user nouns → `topics/<noun>.md`. Match activities → `when/when-<activity>.md`.
3. `grep -rE "^(tags|aliases):" "$MEMORY_HOME"` for keyword lookup.
4. Fallback: `memory-search "phrase" --top 5 --json`.

## Capture

Only on explicit "remember this" / correction / new canonical source. Never silent.

1. Propose layer + filename + frontmatter (`name`, `description`, `type`, `tags`, `aliases`, `see_also`).
2. `memory-capture --layer <L> --file <name>.md --name "..." --description "..." [--tags a,b] [--aliases x,y]`
3. Update `MEMORY.md` only if the memory deserves always-on status.

`created`/`updated` are auto-stamped as ISO-8601 datetimes with offset (e.g. `2026-05-14T15:30:00-04:00`); `valid_from` stays date-only.

`always/` has an 8 KB hard cap — overflow goes to `topics/` or `when/`.
