---
name: rekol
description: Persistent memory at $REKOL_HOME (falls back to $MEMORY_HOME). Trigger on "remember"/"save"/"forget", on a user correction, on a noun matching topics/<noun>.md or activity matching when/when-<activity>.md, or when a question might have a canonical source.
---

# rekol

Memory root: `$REKOL_HOME` (falls back to `$MEMORY_HOME`). Layers: `always/`, `when/`, `topics/`, `knowledge/`. Index: `REKOL.md` (always-on), `.index/INDEX.md` (auto-generated).

## Retrieve — prefer the vector index over reading whole files

Reading full topic files for a single fact wastes context. The index already stores heading-scoped chunks with line ranges; one `rekol search` call returns the most-relevant snippets across all files. Use it as the **first** lookup, not the fallback.

1. `REKOL.md` is already in context — scan for trigger pointers.
2. **Default lookup: `rekol search "phrase" --top 5 --json`.** The result is a JSON object with these keys: `query` (the input), `memory` (array of curated hits — authoritative, read first), `sessions` (array of transcript hits — supplementary, lower-priority), `is_promotion_candidate` (bool — true when memory was queried, returned nothing, and sessions hit; signals something worth capturing), and `sources_queried` (which tiers were actually searched). For each memory hit, use `file_path` + `line_start`/`line_end` to read just that range when you need surrounding context. For each session hit, use `jsonl_path` + `line_number` to locate the message in the original transcript. Only read the whole file when the chunk is genuinely insufficient.
3. **Direct file read** when you already know the exact filename (e.g. `topics/<noun>.md` matches the user's noun verbatim, or `when/when-<activity>.md` matches the activity).
4. **Last-resort grep**: `grep -rE "^(tags|aliases):" "${REKOL_HOME:-$MEMORY_HOME}"` for keyword lookup if search and direct match both miss.

### Search-before-write

Before capturing a new memory, run `rekol search` against its gist. If a near-duplicate exists, **update** it instead of creating a new file. The capture flow has cosine-similarity conflict detection, but pre-empting it avoids round-trips and keeps the layer count clean.

### Search-before-recommend

Before recommending a path, command, ID, env name, or canonical reference, do a quick `rekol search` against the topic. Prevents recommending stale info from in-context reasoning when a canonical source already exists.

## Bring in existing history

When a user wants to seed REKOL from work that already exists, map their phrasing to these commands. Run the command, then confirm with a `rekol search` so the user can verify ingestion worked.

| The user says… | Run | What it does |
| --- | --- | --- |
| "Index my past Claude Code sessions" / "index my history" | `rekol session-index --incremental` (or `--full` to force a re-walk of everything). On a brand-new install, `rekol init` wraps this with a confirm prompt. | Ingests `~/.claude/projects/**/*.jsonl` transcripts into the sessions index so `rekol search` surfaces past work. |
| "Import my notes from ~/Documents/ObsidianVault" | `rekol import ~/Documents/ObsidianVault` | Converts a tree of text files into synthetic transcripts under `claude_projects_dir`, then chains `session-index --incremental` to ingest them. |
| "What do you remember about <X>?" | `rekol search "<X>" --top 5 --json` | Searches curated memory + transcripts; use it to confirm an index/import actually landed. |

`rekol import` is a **mechanical conversion** — it turns your documents into searchable transcript text. It is **not** an LLM reading your notes and filing them into `always/`/`when/`/`topics/` layers. Curating notes into durable layered memory is still done with `rekol capture` (manually or proactively), one fact at a time.

## Capture — proactive, not silent

Capture surprises, corrections, and validated approaches as a side effect of getting work done — not just on explicit "remember this." But always tell the user **what** was captured/changed in one line so they can audit without going hunting.

The bar is still: *would a future session genuinely benefit from knowing this?* Don't capture trivia.

1. Propose layer + filename + frontmatter (`name`, `description`, `type`, `tags`, `aliases`, `see_also`).
2. `rekol capture --layer <L> --file <name>.md --name "..." --description "..." [--tags a,b] [--aliases x,y]`
3. Update `REKOL.md` only if the memory deserves always-on status.

After hand-edits to memory files (Edit/Write tool, not `rekol capture`), run `rekol index update` so the vector DB stays in sync with the markdown.

`created`/`updated` are auto-stamped as ISO-8601 datetimes with offset (e.g. `2026-05-14T15:30:00-04:00`); `valid_from` stays date-only.

`always/` has an 8 KB hard cap — overflow goes to `topics/` or `when/`.

## Stale-fact maintenance

When you observe that a memory is stale (file path moved, cluster decommissioned, person's role changed, command renamed), fix it as soon as you see it. Don't wait for the user to flag it. Update the file and run `rekol index update`. Mention the fix in your response so the user can audit.
