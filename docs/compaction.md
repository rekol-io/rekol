# Surviving context compaction (#122)

Claude Code compacts a session's context when it fills: a summarizer replaces
the conversation with a summary. rekol's memory *survives* this (the
SessionStart hook re-injects REKOL.md and open tasks afterward) — but what
lived only in conversation is at the summarizer's mercy, and it preferentially
drops exactly what matters most: decisions **and their rationale**, session
conventions, stated constraints. The loss is silent: the agent doesn't know
that it doesn't know, so it never searches for what was dropped.

rekol's posture: **steer what we can, flush before erosion, re-present after.**

## 1. Steer: Compact Instructions (paste into your CLAUDE.md)

Claude Code honors a `# Compact Instructions` section in `CLAUDE.md` when
summarizing. rekol recommends this block — paste it into your project or
global `CLAUDE.md` (rekol never edits `CLAUDE.md` for you):

```markdown
# Compact Instructions

When compacting, preserve VERBATIM (never summarize away):
- decisions made this session and the rationale behind them
- stated constraints and conventions ("always X", "never Y" rules)
- the active working set: current task, files being edited, next action
- anything the user asked to remember or corrected

Tool outputs, file dumps, and exploratory reads may be dropped freely.
```

## 2. Flush: capture before the summarizer runs

Durable state written to the store **before** compaction survives perfectly —
that's the whole point of `rekol capture` and the task layer. When a session
runs long, persist decisions/conventions as they happen, and keep the current
task's "next action" note updated (`rekol task`): a compaction then costs you
tool outputs, not judgment.

### Automated: the threshold capture nudge (opt-in wiring)

rekol can nudge the agent to run that capture pass automatically, **once per
session, at 60% context usage** — early on purpose: selection quality degrades
as context fills, so a flush at the brink is judged by a model at its worst
moment.

Two pieces:

- `rekol _hook capture-nudge` runs on every prompt (wired by `install.sh`) and
  injects the one-time nudge. It is **silent** unless it knows the usage — and
  the only place Claude Code exposes context usage is the **statusline** input
  JSON, so:
- Pipe your statusline JSON through `rekol _hook context-watch` (records
  `context_window.used_percentage`; prints nothing). Wrap your existing
  statusline command:

  ```bash
  #!/bin/bash
  input=$(cat)
  printf '%s' "$input" | rekol _hook context-watch
  printf '%s' "$input" | /path/to/your-original-statusline
  ```

  No statusline yet? Ask Claude Code to set one up (`/statusline`) and include
  the `context-watch` line.

This wiring is opt-in because it touches *your* statusline command — rekol
won't hijack it. Without it, the nudge simply never fires.

### Why there is no PreCompact backstop

Claude Code has a `PreCompact` hook, but nothing it outputs reaches the model
(docs document context injection for other events only — PreCompact output is
logging-only). A "flush now!" reminder the agent never sees is theater, so
rekol deliberately doesn't ship one. The early threshold nudge above is the
mechanism — and it fires while judgment is still good, which the brink-of-
compaction moment is not.

## 3. Re-present: what fires after compaction

The SessionStart hook runs again post-compaction (source `compact`) with all
rekol handlers, so a freshly-compacted session is re-primed automatically:

- `REKOL.md` — the always-on memory index
- `rekol _hook session-tasks` — open/in_progress tasks (#113), including the
  one this session claimed: the working set comes back even if the summary
  dropped it
- `rekol _hook session-coverage` / `session-confidence` — health + confidence
  footers

No opt-in needed — this is the default wiring from `install.sh`.
