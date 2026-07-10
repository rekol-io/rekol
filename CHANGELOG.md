# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this project aims to
follow [Semantic Versioning](https://semver.org/).

## [Unreleased]
### Added
- CI now **enforces the per-PR version bump** (#102 part 2): a `version-bump` job fails a
  PR unless its version is ahead of `main` (`scripts/bump_version.py --assert-ahead-of`). A
  status check rather than an auto-push, so it works cleanly with branch protection — you still
  run `bump_version.py` (one command), but forgetting it is now impossible.
### Fixed
- README accuracy pass for the public launch: 'runs on macOS' -> 'macOS and Linux';
  generalized the `~/.zshrc`-only references (`--no-shellrc`, uninstall, post-uninstall) to the
  shell rc for zsh AND bash; softened 'no export in v1' -> 'no export yet'.
### Fixed
- `install.sh` now requires a **venv-capable** Python, not just one with the sqlite
  extension (#launch smoke test). On Debian/Ubuntu the system `python3` can have
  `enable_load_extension` yet lack `ensurepip` (venv is a separate `python3-venv`
  package), so install died mid-`venv`; the probe now skips such interpreters and
  falls through to a venv-capable one, or hard-fails early with the exact `apt install
  python3-venv` fix. Fixes native-Linux install + the hosted CI Linux leg.

## [0.2.0] - 2026-07-09
First public release (quiet go-live; announcement Jul 14). Everything below shipped
during the pre-launch and hold windows.

### Added
- README contact line (`leon@rekol.io`) for questions/feedback.
- rekol skill: a fifth behavioral rule, **"Ask only after searching"** (#35 phase 1) —
  run `rekol search` before asking the user for information you might already have;
  split asks into *knowledge* (look it up) vs *judgment* (ask); ground questions as
  disambiguation over open "how?"; stay silent on a strong hit (precision over nagging).
- `scripts/bump_version.py` (#102): bumps the patch `y` and keeps `pyproject.toml`
  `version` and `src/rekol/__init__.py __version__` in lockstep (refuses on drift).
  `--baseline-ref` skips the bump when the minor/major already changed (a deliberate
  release), `--set X.Y.Z` for an explicit version, `--check` for a dry run. Replaces
  hand-editing the two literals each PR; the post-launch CI bump-on-merge step (still
  part of #102) will call it once CI is on hosted runners.

### Changed
- Install tests (`tests/test_install.bats`) no longer rebuild a venv per test (#78):
  a single shared venv is built once in `setup_file()` and reused via a new opt-in
  `install.sh --skip-deps` (also `REKOL_INSTALL_SKIP_DEPS=1`). Each test used to run a
  full installer → `pip install` pulling torch (731 MB), ~18 min/test and CI
  cancellations; the suite now builds deps once. `--skip-deps` is off by default, so
  production installs are unchanged. Added a per-test timeout safety net.

### Fixed
- `install.sh` now **upgrades an existing pre-#119 hardcoded `REKOL_HOME` rc line
  in place** instead of only adding the guard when absent (QA 20260620-2145). #119
  guarded new installs, but a machine that installed earlier kept its old
  `export REKOL_HOME="<path>"` line forever — the clobber survived for exactly the
  early adopters #119 meant to protect. A re-run now rewrites it to the
  `${REKOL_HOME:-<path>}` form (idempotent); new bats case covers the upgrade.
- Installed `REKOL_HOME` rc export is now a default-if-unset guard
  (`export REKOL_HOME="${REKOL_HOME:-<path>}"`) instead of a hardcoded value (#83).
  A fresh shell still resolves to the installed path (no change for the common
  single-store user), but an inherited `REKOL_HOME` — automation/CI/tests redirecting
  to a throwaway store, or `settings.json` relocating it — now survives re-sourcing the
  rc instead of being silently clobbered back to the baked-in path.
- Search no longer goes silently empty after a curated-index **schema bump** (#97).
  Previously an upgrade that bumped the index schema made `rekol search` exit with
  a stderr-only "run `rekol index rebuild`" message and **empty stdout** — which the
  assistant couldn't tell apart from a legitimate "no results". `rekol search` now
  **self-heals**: on a genuine schema-version mismatch it rebuilds the index in place
  (crash-safe temp-DB swap, offline-first) and returns real results, emitting a
  one-time notice to stderr only so `--json` stdout stays valid. Self-heal is scoped
  to schema bumps — a model-identity mismatch still fails loudly (it must not silently
  re-embed under a different model), and if a rebuild can't run (another index op holds
  the lock, or the cache is read-only) it falls back to the actionable message. The
  background `rekol index update` path intentionally keeps instructing a manual rebuild.

### Changed
- Launch runbook: **launch postponed indefinitely** (external clearance pending,
  new date TBD) — added a prominent "ON HOLD — do not execute" banner and replaced
  the hard "June 16" target with "TBD" so the runbook can't be misread as a live go.
- `.gitignore`: ignore dev-internal session/handoff notes (`SESSION-TODOS.md`,
  `HANDOFF.md`) so they can't be swept into the public repo.

### Changed
- Launch runbook: the public launch is **v0.2.0** (the minor bump per the
  versioning convention) — added the stamp-version-and-tag step before the flip.
- Launch runbook: added a dev-owned step to attach the `rekol.io` custom domain
  to the Cloudflare Pages site *after* the repo is public (kept dark until then
  so "View on GitHub" doesn't 404), plus a note that the site is Direct-Upload —
  a new build needs a manual `wrangler pages deploy` until git auto-deploy is wired.

### Changed
- README accuracy: "Day 1 searchable history" now reflects that install indexes
  your existing Claude Code sessions (searchable right after install), not gated
  on the post-install interview. Install section retitled "macOS & Linux"
  (Ubuntu 24.04 x86_64/arm64 verified + bash shell support).
- **bash shell support**: `install.sh`/`uninstall.sh` now write/remove the PATH +
  `REKOL_HOME` exports in the rc for the user's login shell (`$SHELL`) — `~/.zshrc`
  for zsh, `~/.bashrc` (Linux) or `~/.bash_profile` (macOS) for bash — so the
  `rekol` CLI works in a bash terminal, not only zsh. (Claude Code already got
  `REKOL_HOME` via `settings.json` regardless of shell.)

### Changed
- Pre-public hygiene: removed internal planning docs (`docs/plans/`); Code of
  Conduct contact uses a project address (`conduct@rekol.io`); launch runbook
  gains a privacy/hygiene pre-flip checklist (commit authorship, generic test
  environments, identifier scrub).

### Changed
- Post-install terminal output slimmed to a single call-to-action ("set up my
  rekol memory" in a new Claude Code session), with the two genuine fresh-start
  prerequisites and the local/never-uploaded line. The manual checklist
  (edit identity, `rekol search` verify) and feature explainer move to the
  assistant-led flow / README — the terminal just gets you there.
- README: the "Day 1 searchable" claim is gated on running setup (indexing is
  pull-based, not at install) so the first search isn't empty-by-surprise.
- `--help` opt-out hint now shows valid YAML (`archive_enabled: false`, with the
  space) so a copy-paste actually disables the archive.
- Launch runbook: require the final acceptance run against the *actual* launch
  commit/tag (not an earlier fix commit) before the public flip.
- First-run polish (QA macOS pass): the memory-folder prompt now says you can
  press Enter for the default; `install.sh` picks a suitable Python more reliably
  — it probes `python3.12`/`3.11` and the keg-only `python@3.12`/`@3.11` Homebrew
  prefixes, and **stops early with a clear fix** if no interpreter has Python
  ≥3.11 + `sqlite3.enable_load_extension` (instead of silently degrading search).
- README gains **Prerequisites** and **Troubleshooting** sections (Python/sqlite
  extension, keg-only Homebrew, Intel-mac NumPy, `rekol doctor --deep`).

### Added
- **Memory confidence metadata (#87):** `rekol confirm <file>` (stamp
  `last_confirmed`, distinct from an edit) and `rekol flag-suspect <file>
  --reason` (mark a contradiction without rewriting), completing a
  live → suspect → invalid lifecycle. Search hits now show a confidence tag
  (`· confirmed Nago` / `· unconfirmed` / `⚠ suspected (since X — reason)`), and
  the always-on SessionStart injection gains a footer flagging unverified
  always-on facts so the agent hedges before asserting them. Surface-only —
  rekol shows the signal, the agent decides.
- **`rekol doctor --deep`:** post-install acceptance probe — verifies the
  embedding model loads + embeds meaningfully (catches the silent mean-pooling
  degradation class) and that end-to-end curated recall works.
- `.github/FUNDING.yml` (GitHub Sponsors button).

### Changed
- The bootstrap/propose **pending-review queue moved to the local-only cache**
  (out of the synced `REKOL_HOME`), so raw transcript candidates never reach a
  sync provider (#57).
- The embedding model now **loads offline-first** (`local_files_only`) — no
  online HuggingFace check on every search; works offline and under corporate
  TLS interception, and won't silently fall back to a degraded model.
- Hardened secret-shape detection in the bootstrap review gate (Slack, JWT,
  OAuth Bearer tokens).
- Removed dead sqlite-vec setup from the curated `IndexStore`; documented its
  search as a deliberate full numpy scan (vec0 KNN tracked in #90).

### Fixed
- `install.sh` crashed on its final index/session/archive steps when the memory
  home was answered via the prompt (`REKOL_HOME` not exported) — the resolved
  home is now exported once for all subprocesses (#99).
- Intel (x86_64) macOS install crashed on the first embedding (NumPy 2.x vs the
  older torch ABI) — NumPy is capped to `<2` on Intel macs, and `rekol doctor
  --deep` surfaces the exact remedy (#101).

## [0.1.0] - 2026-06-09
### Added
- Open-source scaffolding: Apache-2.0 license, README, CONTRIBUTING, Code of
  Conduct, issue/PR templates.
- Quality gate: Ruff (lint+format), mypy, pre-commit, GitHub Actions CI.

### Genericization & onboarding (Plan 2)
- Data-level names branded REKOL (`rekol.config.yaml`, `REKOL.md`, `rekol`
  skill) with back-compat reads of the legacy names (`memory.config.yaml`,
  `MEMORY.md`, `/memory` shim).
- `scope: private` frontmatter field reserved (parsed but not validated in v0.1; any value is accepted so existing files are never dropped from the index).
- Legacy migration is now opt-in (`install.sh --migrate`).
- `rekol import` gained `--include`/`--exclude` for file-type selection.
- Sync reframed as local-first; `REKOL_HOME` is any folder you own.
- New `rekol init` interactive onboarding (transcript indexing, corpus import,
  cloud-sync detection, opt-in migration).

### Changed
- Rebranded the project from `memory-tools` to **REKOL**: the Python package is
  now `rekol`, and the formerly separate `memory-*` console scripts are unified
  under a single `rekol` command (`rekol search`, `rekol index`, `rekol capture`,
  `rekol import`, etc.). Docs, hooks, skill, and templates are rebranded to match.
- The data-directory env var is now `REKOL_HOME`. `MEMORY_HOME` is still accepted
  as a fallback, so existing installs keep working without changes.

### Fixed
- `rekol search` crash on queries containing FTS5 operator characters
  (e.g. hyphens) — queries are now sanitized into safe FTS5 phrases.
