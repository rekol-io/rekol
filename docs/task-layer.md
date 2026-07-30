# Cross-session task layer (#113)

Durable, cross-session tasks stored as markdown in `$REKOL_HOME/tasks/` and
surfaced into every new session at SessionStart — so in-progress work survives
session death the way memories already do. Claude Code's built-in task list
stays the **working view**; this layer is the **durable memory**.

Downstream consumer: **#143 (opt-in auto-resume)** reads this layer's
`in_progress` + `session_id` state as its intent semaphore ("does this session
need continuing?"). The schema below is designed for that.

## Storage: one file per task

`$REKOL_HOME/tasks/<id>.md` — one markdown file per task, NOT a single
`tasks.md`.

The store is **fully shared**: every session reads all tasks, any session can
claim, complete, or add one. Per-task files are about *write granularity*, not
visibility — concurrent sessions updating different tasks touch different
files, so a sync provider (Dropbox et al.) never has to merge concurrent
writes to one file. Same reason the coordination channel is one-note-per-file.

`tasks/` is deliberately **outside** the indexed layer dirs
(`always/when/topics/knowledge`): tasks are operational state, not knowledge —
they never enter the semantic index, and `rekol search` stays clean.

## Schema

```markdown
---
id: fix-lane-watch-seen-set        # slug; equals the filename stem
title: Give lane-watch a persistent seen-set
status: open                       # open | in_progress | blocked | done
owner_role: dev                    # optional; "" = unclaimed (shared)
session_id: ""                     # set on claim (rekol task start) — the #143 semaphore
created: 2026-07-30
updated: 2026-07-30
links: ["rekol-io/rekol#143"]      # issues / PRs / notes
---

Free-form body: next action, context for whoever picks it up.
```

Lifecycle: `open` (shared, unclaimed) → `in_progress` (claimed by a session:
`session_id` set) → `done`. `blocked` parks it with a reason in the body.
A task moves between shared and session-claimed by editing frontmatter — the
file never moves. Timestamps reuse `model.py`'s date idioms.

## Conflict handling: two classes, two mechanisms

1. **Same-machine races** (two sessions on one Mac write the same task
   concurrently) → **optimistic concurrency (CAS)** in every CLI write:
   read + sha256 → build new content → re-hash; if unchanged, write via
   temp-file + `os.replace` (the repo's atomic-write idiom); if changed,
   re-read and retry (bounded). Lock-free — nothing to leak if a process dies.
2. **Cross-machine races via sync** (two machines edit before sync converges) —
   CAS *cannot* help here: each machine's hash check runs against its local
   copy, both pass, sync forks a conflicted copy later. There is no shared
   authority at write time. Mitigation is the storage shape itself: per-task
   files shrink the collision surface to "same task, both machines, same sync
   window." That residue is accepted; a future `rekol doctor` check can flag
   `* (conflicted copy)*` files under `tasks/` so forks never rot silently.

## CLI

```
rekol task add "title" [--link REF]... [--role ROLE] [--note TEXT]
rekol task start ID [--session SESSION_ID] [--role ROLE]
rekol task done ID
rekol task block ID --reason TEXT
rekol task list [--status S]... [--all]
```

`start` is the claim operation (sets `in_progress` + `session_id`);
`done`/`block` update status and stamp `updated`. `list` shows open +
in_progress by default; `--all` includes done/blocked. v1 has **no** auto-sync
from Claude Code's built-in task list — the agent maintains this layer via the
CLI (a TaskUpdate→rekol bridge is a possible later addition).

## SessionStart injection

`rekol _hook session-tasks` — same contract as `session-confidence` /
`session-coverage`: rides the SessionStart injection, prints open/in_progress
tasks (capped; oldest-first; silent when none), soft-fails (any error → print
nothing, exit 0). Wired as its **own** SessionStart handler in
`hooks/sessionstart-snippet.json`, added idempotently to existing installs by
`install.sh` (the #123 Step-7B pattern) — the delicate memory-loader command is
never touched.
