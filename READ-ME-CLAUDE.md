# READ-ME-CLAUDE — memory-tools

If you are a fresh Claude Code instance and a user points you at this file, read it top to bottom. Your job: understand the system, install it if asked, and use it correctly thereafter.

## What this is

A persistent memory system for Claude Code. Data lives in `$MEMORY_HOME` (a Dropbox-backed directory). Four layers of markdown:

- `always/*.md` — re-injected every session via the SessionStart hook. Hard budget 8 KB total.
- `when/when-<activity>.md` — task-triggered defaults. You read these when about to do the matching activity.
- `topics/<topic>.md` — canonical-source registry. Read when the topic comes up.
- `knowledge/*.md` — long-form. Found via vector search.

Every file has YAML frontmatter (`name`, `description`, `type`, `tags`, `aliases`, `see_also`, `created`, `updated`).

## Four retrieval paths (use all four)

1. `MEMORY.md` at the root — already in context; scan trigger-phrased pointers ("Before X, read Y").
2. Frontmatter tags/aliases — `grep -r "alias:" "$MEMORY_HOME"` to find by keyword.
3. Inter-file links and `see_also` — follow them when reading any memory file.
4. `memory-search "query" --top 5 --json` — semantic vector fallback when the above miss.

## Install (when user asks)

```
cd ~/mac_setup
./setup.sh --profile personal --phase 3    # or --profile work
```

It is idempotent. Existing memory files are never modified. `~/.claude/settings.json` is backed up before any edit. Install journal written to `$MEMORY_HOME/.install-journal-<timestamp>.log`.

## Capture protocol (how you write to memory)

When the user says "remember this," corrects you, or you learn a canonical source:

1. Propose layer (`always` / `when` / `topic` / `knowledge`) and target file (new or existing).
2. Propose frontmatter.
3. Write the file.
4. Update `MEMORY.md` **only** if the memory deserves always-on (mostly `always/*`).
5. Run `memory-index update`.
6. Confirm to user with a one-line summary.

No silent saves.

## Retrieval protocol (how you read memory)

1. Check `MEMORY.md` (already in context) for trigger-phrased pointers matching the user's intent.
2. If an activity is starting, read the matching `when-*.md`.
3. If a known topic is mentioned, read `topics/<topic>.md`.
4. Else, run `memory-search "user's phrasing" --top 5` and read relevant hits.
5. If nothing relevant, answer from general knowledge and offer to save new context.

## Examples

See `template/` for example files with frontmatter.
