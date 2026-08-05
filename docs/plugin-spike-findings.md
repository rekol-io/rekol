# Claude Code plugin spike — findings (#153)

Spike answering whether rekol can ship as a Claude Code plugin instead of
`git clone && ./install.sh`. Prototype lives in `plugin/`.

**Verdict: viable.** Four of five acceptance criteria pass with evidence; one
needs a live install to finish. One genuine blocker surfaced that is not about
plugins at all (see *Package source*).

## Why plugins, not Homebrew (#116)

A formula **cannot** own the Claude Code integration — brew always leaves a
second `rekol setup` step. The plugin removes exactly that step: the idempotent
`settings.json` merges (installer Steps 7 / 7B / 7C / 7E2 / 7F) that are the
most fragile part of the installer and have produced real bugs. Plugin hooks are
declared in `hooks/hooks.json` and **merge natively** with the user's own hooks,
so that whole bug class disappears along with the code that creates it.

## Criteria

### 1. Failed bootstrap must be visible — ✅ PASS
Plugins have no install-time hook, so the Python env is built on first
SessionStart. That moves failure from install-time (loud, user watching) to
session-time (quiet, user not) — the direction that hides failures.

Verified empirically:
- **Plugin hook output does reach the model.** Not inferred: the installed
  `superpowers` plugin injects at SessionStart and its content appeared in a live
  session.
- A failed bootstrap prints reason + fix + log path and states plainly
  *"memory is NOT active this session"*.
- It reports **once per session**, not per prompt; other hook modes stay silent
  when not ready, so a broken install is loud once rather than noisy forever.
- A healthy bootstrap prints progress, because a silent 1–2 minute first launch
  reads as a hang.

### 2. Idempotent + concurrency-safe — ✅ PASS
12 simultaneous cold starts against one state dir:

```
venv builds that ran:      1    (must be 1)
sessions reporting broken: 0
sessions that waited:      11/12
partial marker left:       no
lockdir cleaned up:        yes
venv usable afterwards:    yes
```

Note for reviewers: **macOS ships no `flock`**, so the mkdir-lock fallback *is*
the primary path on Mac — it is the one that had to be correct, and it is the one
tested above. Interrupted builds leave a `.partial` marker so a half-built venv
is never mistaken for a finished one.

### 3. Scriptable / non-interactive install — ✅ PASS
`claude plugin` exposes a full CLI: `install`, `uninstall`, `enable`, `disable`,
`list`, `details`, plus `marketplace add|update|list|remove`. QA's headless
container harness keeps working.

Two incidental finds worth tracking:
- `claude plugin details` reports **projected token cost** — useful for the
  context-budget work.
- `claude plugin tag` creates `{name}--v{version}` git tags — overlaps #28.

### 4. Coexistence with an existing `install.sh` install — ✅ PASS
A user who ran `install.sh` and then installs the plugin would be double-wired:
two SessionStart injections, duplicate memory blocks, duplicate capture-nudge.
The plugin **cannot** edit `settings.json` (that is the point of it), so it
**defers**: detects the installer's hooks, stands down, and says once how to pick
one. Verified in both directions — stands down when installer hooks are present,
proceeds normally on a clean machine.

### 5. Differentiators preserved — ⏳ needs a live install
Ambient/auto-triggered retrieval is carried by the same `_hook` commands the
installer wires (`session-tasks`, `session-coverage`, `session-confidence`,
`time-context`, `capture-nudge`), invoked from the plugin's hooks rather than
from `settings.json` — so it should be behaviour-identical. Confirming that
end-to-end needs the plugin actually installed, which was deliberately not done
on a live machine during the spike (it would add hooks to every session).

## Blocker: package source

The bootstrap does `pip install rekol==<version>` — but **rekol is not published
to PyPI**, so there is nothing to install. This is not a plugin problem; it is a
distribution prerequisite shared with #116. Options: publish to PyPI, or pip
install from a tagged GitHub release artifact.

**This makes #28 (release pipeline) a dependency of the plugin path, not just of
Homebrew.** Worth stating plainly: plugin-first is *not* dependency-free, it just
has a smaller and more useful dependency than brew.

## Prototype layout

```
plugin/
  .claude-plugin/plugin.json   name, version, description, repo
  hooks/hooks.json             SessionStart / SessionEnd / UserPromptSubmit
  hooks/bootstrap              entrypoint: bootstrap + coexistence + hook dispatch
```

## Next

1. Resolve the package source (PyPI or release artifact) — gates everything else.
2. Live-install in a container to finish criterion 5 and let QA run their matrix.
3. Marketplace repo + publishing — follow-up, not part of the spike.
