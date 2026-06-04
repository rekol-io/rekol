# Security Policy

## Reporting a vulnerability

Please report security vulnerabilities **privately** to **security@rekol.io**.
Do **not** open a public GitHub issue for a vulnerability.

We'll acknowledge your report, work on a fix, and credit you (if you'd like) once
it's resolved.

## What to look at

rekol is local-first — no server, no account, no telemetry — so the main security
surfaces are:

- **`install.sh` / `uninstall.sh`** and the hooks they wire. They modify
  `~/.zshrc`, `~/.claude/settings.json`, and install a local venv + shim, so
  bugs here could affect your shell or assistant configuration.
- **The local index / cache.** `sessions.db` records your Claude Code transcripts
  **verbatim** (including anything you paste). By design it lives in a
  machine-local cache **outside** your memory folder
  (`${XDG_CACHE_HOME:-~/.cache}/rekol/`), so it is never carried off-machine by
  syncing `$REKOL_HOME`. Reports of paths where transcript/index data could leak
  into a synced or remote location are especially welcome.

## Out of scope

rekol has no hosted component; there is no server, API, or account to attack.

For general (non-security) questions, use **hello@rekol.io**.
