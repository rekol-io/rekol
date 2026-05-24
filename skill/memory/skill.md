---
name: memory
description: Persistent memory at $MEMORY_HOME. Trigger on "remember"/"save"/"forget", on a user correction, on a noun matching topics/<noun>.md or activity matching when/when-<activity>.md, or when a question might have a canonical source.
---

# memory

Memory root: `$MEMORY_HOME`. Layers: `always/`, `when/`, `topics/`, `knowledge/`. Index: `MEMORY.md` (always-on), `.index/INDEX.md` (auto-generated).

## Retrieve — prefer the vector index over reading whole files

Reading full topic files for a single fact wastes context. The index already stores heading-scoped chunks with line ranges; one `memory-search` call returns the most-relevant snippets across all files. Use it as the **first** lookup, not the fallback.

1. `MEMORY.md` is already in context — scan for trigger pointers.
2. **Default lookup: `memory-search "phrase" --top 5 --json`.** Use the returned `file_path` + `line_start`/`line_end` to read just that range when you need surrounding context. Only read the whole file when the chunk is genuinely insufficient.
3. **Direct file read** when you already know the exact filename (e.g. `topics/<noun>.md` matches the user's noun verbatim, or `when/when-<activity>.md` matches the activity).
4. **Last-resort grep**: `grep -rE "^(tags|aliases):" "$MEMORY_HOME"` for keyword lookup if search and direct match both miss.

### Search-before-write

Before capturing a new memory, run `memory-search` against its gist. If a near-duplicate exists, **update** it instead of creating a new file. The capture flow has cosine-similarity conflict detection, but pre-empting it avoids round-trips and keeps the layer count clean.

### Search-before-recommend

Before recommending a path, command, ID, env name, or canonical reference, do a quick `memory-search` against the topic. Prevents recommending stale info from in-context reasoning when a canonical source already exists.

## Capture — proactive, not silent

Capture surprises, corrections, and validated approaches as a side effect of getting work done — not just on explicit "remember this." But always tell the user **what** was captured/changed in one line so they can audit without going hunting.

The bar is still: *would a future session genuinely benefit from knowing this?* Don't capture trivia.

1. Propose layer + filename + frontmatter (`name`, `description`, `type`, `tags`, `aliases`, `see_also`).
2. `memory-capture --layer <L> --file <name>.md --name "..." --description "..." [--tags a,b] [--aliases x,y]`
3. Update `MEMORY.md` only if the memory deserves always-on status.

After hand-edits to memory files (Edit/Write tool, not `memory-capture`), run `memory-index update` so the vector DB stays in sync with the markdown.

`created`/`updated` are auto-stamped as ISO-8601 datetimes with offset (e.g. `2026-05-14T15:30:00-04:00`); `valid_from` stays date-only.

`always/` has an 8 KB hard cap — overflow goes to `topics/` or `when/`.

## Stale-fact maintenance

When you observe that a memory is stale (file path moved, cluster decommissioned, person's role changed, command renamed), fix it as soon as you see it. Don't wait for the user to flag it. Update the file and run `memory-index update`. Mention the fix in your response so the user can audit.
