---
name: memory
description: Use this skill whenever you might benefit from the persistent memory system — to consult stored context, write new memories, or look up canonical sources. Triggers: user says "remember this"/"save this"; user corrects or re-explains something; user asks about a topic that might have a canonical source; before any activity that has a `when/when-<activity>.md` rule.
---

# memory skill

You have access to a persistent memory system rooted at `$MEMORY_HOME`.

## Four retrieval paths — use all of them

1. **`MEMORY.md`** — already in context. Scan for trigger-phrased pointers that match the user's intent.
2. **Tags and aliases** — `grep -rE "^(tags|aliases):" "$MEMORY_HOME"` to find files by keyword.
3. **Inter-file links and `see_also`** — follow them when reading any memory file.
4. **`memory-search "query" --top 5 --json`** — semantic fallback when paths 1–3 miss.

Always try paths 1–3 before falling back to path 4.

## Retrieval protocol

- If the user's request matches a `when/when-<activity>.md` trigger, read that file first.
- If a noun in the request matches `topics/<noun>.md`, read it.
- Otherwise, run `memory-search`.
- If nothing relevant, answer from general knowledge and offer to save the new context.

## Capture protocol (writing to memory)

Triggered by:
- Explicit "remember this" / "save this to memory."
- Detected correction ("you forgot," "I told you before").
- Learning a new canonical source.

Steps:
1. Propose the layer (`always` / `when` / `topic` / `knowledge`) and target file.
2. Propose frontmatter: `name`, `description`, `type`, `tags`, `aliases`, `see_also`.
3. Write the file via `memory-capture --layer ... --file ...` OR by writing the file directly and then running `memory-index update`.
4. Update `MEMORY.md` **only if** the memory deserves always-on status.
5. Confirm with a one-line summary.

No silent saves.

## Budget

`always/*.md` has an 8 KB hard cap. If adding to `always/` would exceed it, route the memory to `topics/` or `when/` instead.

## CLIs

- `memory-index rebuild | update`
- `memory-search "query" [--top N] [--json]`
- `memory-capture --layer <L> --file <name.md> --name "..." --description "..." [--tags a,b,c] [--aliases x,y]`
