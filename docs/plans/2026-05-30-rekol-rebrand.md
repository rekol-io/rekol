# Implementation Plan: REKOL Full Rebrand

**Date:** 2026-05-30
**Branch for execution:** `feat/rekol-rebrand` (create from `main`)
**Status:** Plan — not yet executed

---

## Goal

Rebrand the project currently named `memory-tools` to **REKOL** (rekol.io) across
every layer:

1. Rename the Python package `src/memory_tools/` → `src/rekol/` and fix every import.
2. Collapse the 8 separate console scripts into a single `rekol` Click command group
   with subcommands.
3. Support `$REKOL_HOME` as the primary environment variable, with `$MEMORY_HOME`
   retained as a fallback so Leon's live setup keeps working.
4. Rebrand all user-facing text (README, docs, hooks, skill, templates, CHANGELOG).
5. Rename the GitHub repo `memory-tools` → `rekol` (documented step; auto-redirects).
6. Note (no execution) that PyPI publishing will use the name `rekol` at v0.1.

The existing **167-test suite is the regression safety net**. After the package
rename and after the CLI consolidation, the full suite must pass. New tests are
added for the env-var fallback precedence and the unified CLI dispatch.

### Non-goals

- No behavior changes to search/index/capture/migrate logic.
- No PyPI publish (out of scope; only the package name is set so a future v0.1
  ships as `rekol`).
- No change to `MEMORY_TOOLS_HOME` (the *install location* env var used by the
  `bin/` shims and `install.sh`) — that is a different variable from `MEMORY_HOME`
  (the *data directory*). This plan only touches `MEMORY_HOME`. See the
  "Naming clarifications" section.

---

## Architecture

### Current layout (verified on `main`)

```
src/memory_tools/
  __init__.py
  chunker.py  config.py  embeddings.py  indexer.py  model.py
  search_combined.py  store.py
  cli_capture.py  cli_docs_convert.py  cli_index.py  cli_invalidate.py
  cli_migrate.py  cli_propose.py  cli_search.py  cli_session_index.py
  docs_convert/  (__init__, convert, extract, transcript, walk, writer)
  migrate/       (__init__, archive, classify, discover, llm, migrator)
  sessions/      (__init__, ingest, store)
bin/             (8 bash shims, one per console script)
hooks/           (auto-reindex.sh, *-snippet.json)
skill/memory/skill.md
template/        (memory.config.yaml.example, MEMORY.md, always/, when/, topics/)
tests/           (29 test_*.py + acceptance.md + test_install.bats)
pyproject.toml   (name="memory-tools", 8 [project.scripts], mypy files=src/memory_tools)
install.sh       (loops over the 8 command names; runs memory-index / memory-migrate)
.pre-commit-config.yaml  (mypy hook hardcodes `mypy src/memory_tools`)
```

### Key facts that shape the design

- **`cli_index.py` and `cli_migrate.py` are Click *groups*** (`@click.group()`),
  each with their own subcommands (e.g. `rebuild`, `update`; `auto`, `file`).
  The other six (`cli_search`, `cli_capture`, `cli_invalidate`, `cli_propose`,
  `cli_session_index`, `cli_docs_convert`) are plain `@click.command()`s.
  → The unified `rekol` group will register the six commands directly and register
  the two existing groups as **nested subgroups**. So `rekol index rebuild` and
  `rekol migrate auto ...` keep working with the same nested verbs.
- **Tests import each command as `main`** (e.g.
  `from memory_tools.cli_search import main as search_main`) and invoke them with
  `CliRunner().invoke(search_main, [...])`. Each `cli_*.py` keeps its `main`
  object exactly as-is; the new `rekol/cli.py` only *registers* them. This keeps
  the existing CLI tests valid (after the package-name import fix) and lets us add
  a small new dispatch test against the top-level `rekol` group.
- **130 occurrences of `memory_tools`** exist across `src/` + `tests/`. The rename
  is mechanical but must be verified with grep.
- **`config.py` raises `RuntimeError`** when `MEMORY_HOME` is unset. `cli_migrate.py`
  also reads `MEMORY_HOME` directly (line ~30). Both must consult the new resolver.

### Target layout (after this plan)

```
src/rekol/
  __init__.py  ... (all modules, same names)
  cli.py                     # NEW: top-level `rekol` Click group
  config.py                  # resolves REKOL_HOME, falls back to MEMORY_HOME
  cli_*.py                   # unchanged command/group objects, imports fixed
pyproject.toml   name="rekol", single [project.scripts] rekol = "rekol.cli:main"
bin/rekol                    # NEW single shim; old 8 shims removed
install.sh                   # installs single `rekol`; uses REKOL_HOME (fallback MEMORY_HOME)
```

---

## Tech Stack

- **Python 3.11+**, `setuptools` build backend, src-layout.
- **Click 8.x** for the CLI (groups + commands).
- **Dev tooling (already gated):** Ruff (lint + format), mypy, pre-commit, GitHub
  Actions CI.
- **Dev venv:** `.venv-dev/` — run all tooling via its binaries:
  - tests: `.venv-dev/bin/pytest -q`
  - lint: `.venv-dev/bin/ruff check .` and `.venv-dev/bin/ruff format --check .`
  - types: `.venv-dev/bin/mypy src/rekol` (path changes after rename)
- **Test count baseline:** 167 passing tests. This is the regression gate.

---

## Naming clarifications (read before starting)

Three distinct env vars exist; do not conflate them:

| Variable | Meaning | Touched by this plan? |
|---|---|---|
| `MEMORY_HOME` | The user's **data directory** (memory root) | **Yes** — add `REKOL_HOME` as primary, keep this as fallback |
| `REKOL_HOME` | New primary name for the data directory | **Yes** — introduced |
| `MEMORY_TOOLS_HOME` | The **install location** of the venv/shims (used by `bin/*` and `install.sh`) | Rename to `REKOL_TOOLS_HOME` as part of the shim/installer task, but keep a fallback too (Task 6) |

`MEMORY.md` (the always-on memory index file at the data root) is a **filename**, not
the brand. It is intentionally **left unchanged** — renaming it would break every
user's existing memory root and the SessionStart hook that `cat`s it. Flagged for
Leon in the self-review.

---

## Execution Tasks

> Convention: one commit per task (commit message given). Run the regression gate
> (`.venv-dev/bin/pytest -q`) at the points marked **GATE**. Use conventional-commit
> style. Do not push until the whole branch is ready (or push the branch and open a
> draft PR — Leon's call).

### Task 0 — Branch and baseline

1. From `main`: `git checkout -b feat/rekol-rebrand`.
2. Establish the green baseline so any later failure is attributable:
   ```
   .venv-dev/bin/pytest -q
   ```
   Confirm **167 passed**. If not 167, stop and reconcile with Leon before
   proceeding (the plan assumes 167 as the gate number).
3. No commit (baseline only).

---

### Task 1 — Rename the Python package directory

**Goal:** `src/memory_tools/` → `src/rekol/` with history preserved.

1. Move the directory with git so renames are tracked:
   ```
   git mv src/memory_tools src/rekol
   ```
2. Do **not** edit imports yet — this commit is a pure move. The suite will be red
   between Task 1 and Task 2; that is expected and why they are adjacent.
3. Commit:
   ```
   refactor: move package src/memory_tools -> src/rekol
   ```

---

### Task 2 — Fix all internal imports (`from memory_tools...` → `from rekol...`)

**Goal:** every `memory_tools` import in `src/` and `tests/` becomes `rekol`. This is
the package-rename half of the safety-net gate.

1. Rewrite imports in source and tests. Two equivalent options — pick one:

   **Option A (sed, fast):**
   ```
   grep -rl "memory_tools" src/ tests/ \
     | xargs sed -i '' 's/memory_tools/rekol/g'
   ```
   (`-i ''` is the BSD/macOS in-place form.)

   **Option B (ruff-safe manual):** if you prefer not to sed, edit each file the
   grep below reports. Either way, verify with step 3.

   Note: this also rewrites docstring/comment mentions of the *module* path like
   `from memory_tools.config import ...` inside docstrings, which is correct.
   Brand-text rebrand of prose lives in Task 5 — here we only care that the
   **literal token `memory_tools`** is gone.

2. Update the **pre-commit mypy hook** which hardcodes the package path
   (`.pre-commit-config.yaml`):
   - `entry: mypy src/memory_tools` → `entry: mypy src/rekol`

3. **Verify no `memory_tools` token remains in code:**
   ```
   grep -rn "memory_tools" src/ tests/
   ```
   Must print **nothing**. (This is the self-review check from the brief.)

4. **GATE — run the full suite against the renamed package.** Note: `pyproject.toml`
   still says `name = "memory-tools"` and lists the old scripts, but the package is
   importable as `rekol` because tests import the package directly and the dev venv
   was installed `-e`. If imports fail because the installed egg-link still points at
   `memory_tools`, reinstall editable:
   ```
   .venv-dev/bin/pip install -e ".[dev]"   # only if import errors appear
   .venv-dev/bin/pytest -q
   ```
   Confirm **167 passed**.

5. Commit:
   ```
   refactor: update all imports memory_tools -> rekol
   ```

---

### Task 3 — Update `pyproject.toml` package metadata + mypy paths

**Goal:** build metadata, tool config, and (temporarily) the old scripts all point at
`rekol`. The single-script consolidation is Task 4; here we just fix names/paths so
the package builds and mypy/ruff resolve.

1. Edit `pyproject.toml`:
   - `[project] name = "memory-tools"` → `name = "rekol"`.
   - `[tool.mypy] files = ["src/memory_tools"]` → `files = ["src/rekol"]`.
   - `[[tool.mypy.overrides]]` — module list is third-party only
     (`sentence_transformers.*`, `sqlite_vec.*`, `frontmatter.*`); **no change**
     needed, but confirm none reference `memory_tools`.
   - `[tool.setuptools.packages.find]` uses `where = ["src"]` (auto-discovers the
     package) — **no path edit needed**, it will now find `rekol`. Confirm.
   - Leave the 8 `[project.scripts]` entries momentarily, but update their module
     paths so an intermediate `pip install -e` still works:
     `memory_tools.cli_index:main` → `rekol.cli_index:main`, etc. (Task 4 replaces
     this whole block — this keeps the tree installable between tasks.)

2. Reinstall editable so the new dist name/metadata registers:
   ```
   .venv-dev/bin/pip install -e ".[dev]"
   ```

3. **GATE:**
   ```
   .venv-dev/bin/pytest -q          # 167 passed
   .venv-dev/bin/mypy src/rekol     # clean
   .venv-dev/bin/ruff check .       # clean
   ```

4. Commit:
   ```
   refactor: rename distribution to rekol; point pyproject + mypy at src/rekol
   ```

---

### Task 4 — Unified `rekol` CLI (Click group with subcommands) — TDD

**Goal:** one `rekol` command. Subcommand mapping (from the brief):

| Old console script | New invocation | Source object |
|---|---|---|
| `memory-search` | `rekol search` | `cli_search.main` (command) |
| `memory-index` | `rekol index` | `cli_index.main` (**group**) |
| `memory-capture` | `rekol capture` | `cli_capture.main` (command) |
| `memory-invalidate` | `rekol invalidate` | `cli_invalidate.main` (command) |
| `memory-propose` | `rekol propose` | `cli_propose.main` (command) |
| `memory-migrate` | `rekol migrate` | `cli_migrate.main` (**group**) |
| `claude-session-index` | `rekol session-index` | `cli_session_index.main` (command) |
| `memory-docs-convert` | `rekol import` | `cli_docs_convert.main` (command) |

Note the two groups (`index`, `migrate`) nest: `rekol index rebuild`,
`rekol migrate auto --commit`, etc. keep their existing subverbs unchanged.

**TDD order — write the test first:**

1. **New test file `tests/test_cli_group.py`.** Assert the unified dispatch:
   - `from rekol.cli import main as rekol_main`.
   - `rekol_main` is a `click.Group`.
   - Its registered command names are exactly:
     `{"search", "index", "capture", "invalidate", "propose", "migrate",
       "session-index", "import"}`
     (use `set(rekol_main.commands.keys())`). This is the automated guard that
     **every console script is accounted for**.
   - `CliRunner().invoke(rekol_main, ["--help"])` exits 0 and lists each subcommand.
   - Smoke one leaf: `CliRunner().invoke(rekol_main, ["search", "--help"])` exits 0.
   - Smoke a nested group: `CliRunner().invoke(rekol_main, ["index", "--help"])`
     exits 0 and mentions `rebuild`.
   - Run it — it must **fail** (no `rekol/cli.py` yet).

2. **Create `src/rekol/cli.py`:**
   ```python
   """Unified `rekol` command-line entrypoint.

   Collapses the formerly separate console scripts into one Click group so the
   tool presents a single `rekol <subcommand>` surface (REKOL — rekol.io).
   """
   from __future__ import annotations

   import click

   from rekol.cli_capture import main as capture_cmd
   from rekol.cli_docs_convert import main as import_cmd
   from rekol.cli_index import main as index_grp
   from rekol.cli_invalidate import main as invalidate_cmd
   from rekol.cli_migrate import main as migrate_grp
   from rekol.cli_propose import main as propose_cmd
   from rekol.cli_search import main as search_cmd
   from rekol.cli_session_index import main as session_index_cmd


   @click.group()
   @click.version_option(package_name="rekol")
   def main() -> None:
       """REKOL — layered, cross-indexed memory with local vector search."""


   # Leaf commands keep their own option/argument definitions; register under
   # rebranded names. The two Click *groups* (index, migrate) nest, preserving
   # their existing subverbs (e.g. `rekol index rebuild`).
   main.add_command(search_cmd, name="search")
   main.add_command(index_grp, name="index")
   main.add_command(capture_cmd, name="capture")
   main.add_command(invalidate_cmd, name="invalidate")
   main.add_command(propose_cmd, name="propose")
   main.add_command(migrate_grp, name="migrate")
   main.add_command(session_index_cmd, name="session-index")
   main.add_command(import_cmd, name="import")
   ```
   - `version_option(package_name="rekol")` requires the dist name set in Task 3.
   - Keep the per-module `main` objects untouched so the existing `test_cli.py`
     and `test_migrate_cli.py` imports stay valid.

3. **Replace the `[project.scripts]` block** in `pyproject.toml` with a single entry:
   ```toml
   [project.scripts]
   rekol = "rekol.cli:main"
   ```
   Reinstall editable so the `rekol` entrypoint is created:
   ```
   .venv-dev/bin/pip install -e ".[dev]"
   ```

4. **GATE:**
   ```
   .venv-dev/bin/pytest -q          # 167 + new tests pass
   .venv-dev/bin/ruff check .
   .venv-dev/bin/mypy src/rekol
   ```
   Also smoke the real entrypoint:
   ```
   .venv-dev/bin/rekol --help
   .venv-dev/bin/rekol index --help
   ```

5. Commit:
   ```
   feat: unify console scripts into single `rekol` command group
   ```

---

### Task 5 — `REKOL_HOME` primary, `MEMORY_HOME` fallback — TDD

**Goal:** the data-dir resolver prefers `REKOL_HOME`, falls back to `MEMORY_HOME`,
and errors clearly when neither is set. Do **not** remove `MEMORY_HOME` support —
Leon's live shell exports `MEMORY_HOME`.

**TDD order — write the tests first** (append to `tests/test_config.py`):

1. New tests:
   - `test_rekol_home_takes_precedence`: set both `REKOL_HOME=/a` and
     `MEMORY_HOME=/b`; assert `load_config().memory_home == Path("/a")`.
   - `test_memory_home_used_when_rekol_home_unset`: unset `REKOL_HOME`, set
     `MEMORY_HOME`; assert it resolves to the `MEMORY_HOME` path. (Guards Leon's
     live setup.)
   - `test_raises_when_neither_home_set`: `monkeypatch.delenv` both; assert
     `RuntimeError` is raised and the message names **both** `REKOL_HOME` and
     `MEMORY_HOME`.
   - Keep the existing `test_load_config_*` tests (they set `MEMORY_HOME`) — they
     must still pass via the fallback path.
   - Run — the new precedence/error-message tests fail.

2. **Edit `src/rekol/config.py` `load_config()`** — replace the env lookup:
   ```python
   # REKOL_HOME is the primary data-directory variable; MEMORY_HOME is kept as a
   # fallback so existing installs (which export MEMORY_HOME) keep working.
   env = os.environ.get("REKOL_HOME") or os.environ.get("MEMORY_HOME")
   if not env:
       raise RuntimeError(
           "Neither REKOL_HOME nor MEMORY_HOME is set. Export REKOL_HOME to point "
           "at your memory home directory before running rekol "
           "(MEMORY_HOME is accepted as a fallback)."
       )
   ```
   - Update the `load_config` docstring `Raises:` line to mention both vars.
   - Update the module docstring and `Config` docstring references from
     `$MEMORY_HOME` to `$REKOL_HOME` (note fallback).

3. **Edit `src/rekol/cli_migrate.py`** — it reads `MEMORY_HOME` directly (~line 30).
   Apply the same precedence:
   ```python
   env = os.environ.get("REKOL_HOME") or os.environ.get("MEMORY_HOME")
   if not env:
       click.echo("error: neither REKOL_HOME nor MEMORY_HOME is set.", err=True)
   ```
   Better: refactor this to call a shared helper in `config.py` (e.g.
   `resolve_memory_home() -> str | None`) and have both `load_config()` and
   `cli_migrate` use it, to avoid two copies of the precedence rule. If you add the
   helper, add a unit test for it.

4. **GATE:**
   ```
   .venv-dev/bin/pytest -q          # all pass incl. new env-var tests
   .venv-dev/bin/mypy src/rekol
   .venv-dev/bin/ruff check .
   ```

5. Commit:
   ```
   feat: prefer REKOL_HOME, fall back to MEMORY_HOME for data dir
   ```

---

### Task 6 — `bin/` shims + `install.sh`

**Goal:** ship one `rekol` shim; install a single console script; export `REKOL_HOME`.

1. **Replace the 8 shims with one** `bin/rekol`:
   ```
   git rm bin/memory-index bin/memory-search bin/memory-capture \
          bin/memory-invalidate bin/memory-propose bin/memory-migrate \
          bin/claude-session-index bin/memory-docs-convert
   ```
   Create `bin/rekol` (model on the old shim, but call the unified module and keep
   the install-location var with a fallback):
   ```bash
   #!/usr/bin/env bash
   set -euo pipefail

   # Shim for `rekol` — delegates to the venv Python entrypoint.
   # REKOL_TOOLS_HOME is the install location; MEMORY_TOOLS_HOME accepted as fallback.
   REKOL_TOOLS_HOME="${REKOL_TOOLS_HOME:-${MEMORY_TOOLS_HOME:-$HOME/.local/share/rekol}}"
   VENV_PY="$REKOL_TOOLS_HOME/.venv/bin/python"

   if [[ ! -x "$VENV_PY" ]]; then
     printf 'rekol venv not found at %s/.venv; run installer.\n' \
       "$REKOL_TOOLS_HOME" >&2
     exit 2
   fi

   exec "$VENV_PY" -m rekol.cli "$@"
   ```
   `chmod +x bin/rekol`. (`python -m rekol.cli` works because `cli.py`'s `main` is a
   Click group; if a `__main__` guard is wanted, add `if __name__ == "__main__":
   main()` to `cli.py` — optional, the entrypoint console script does not need it,
   but the `-m` shim does, so **add the guard**.)

2. **Edit `install.sh`:**
   - The shim-install loop (~line 154) iterates the 8 command names. Replace with a
     single `rekol` install (copy `bin/rekol`, symlink/install one console script).
   - `TOOLS_HOME` default path `~/.local/share/memory-tools` → `~/.local/share/rekol`
     (keep reading `REKOL_TOOLS_HOME` then `MEMORY_TOOLS_HOME` as fallback so an
     existing install dir is still found).
   - Pre-flight (~line 84): require `REKOL_HOME` **or** `MEMORY_HOME`; update the
     error text. The `MEMORY_HOME` mkdir/journal/seed logic (~lines 96–222) should
     operate on the resolved home (introduce `RESOLVED_HOME="${REKOL_HOME:-$MEMORY_HOME}"`
     near the top and use it).
   - `.zshrc` export step (~line 186): export `REKOL_HOME` (not `MEMORY_HOME`); guard
     so it is not re-added if already present.
   - Post-install index/migrate calls (~lines 479–501) invoke `memory-index` /
     `memory-migrate` binaries → change to `rekol index ...` / `rekol migrate ...`
     via the single venv entrypoint (`"${TOOLS_HOME}/.venv/bin/rekol" index update`,
     etc.).
   - Final "next steps" echo (~line 519) `memory-search "identity"` → `rekol search "identity"`.

3. **`tests/test_install.bats`** likely asserts on shim names / install behavior.
   Read it and update expectations to the single `rekol` shim. Run:
   ```
   bats tests/test_install.bats      # if bats is available; otherwise note as manual
   ```
   If `bats` is not installed in this environment, document that the bats suite must
   be run by Leon and update the assertions to match the new single-shim reality.

4. **GATE:** `.venv-dev/bin/pytest -q` (Python suite unaffected; bats is separate).

5. Commit:
   ```
   feat: install single `rekol` shim; export REKOL_HOME in installer
   ```

---

### Task 7 — Rebrand user-facing text (hooks, skill, templates, docs, README, CHANGELOG)

**Goal:** prose and config strings say REKOL / `rekol`, and hooks/skill use
`REKOL_HOME` (with `MEMORY_HOME` fallback where a shell snippet reads the var).

1. **`hooks/`:**
   - `auto-reindex.sh` (~lines 6/13/14/29/32): the script gates on `$MEMORY_HOME`.
     Make it resolve `REKOL_HOME` first: `HOME_DIR="${REKOL_HOME:-${MEMORY_HOME:-}}"`,
     then `[ -z "$HOME_DIR" ] && exit 0` and the `"$HOME_DIR"/*)` case match. Replace
     any `memory-index` call with `rekol index ...`.
   - `sessionstart-snippet.json`: the embedded command references `$MEMORY_HOME`,
     `MEMORY.md`, and the literal text `run memory-search`. Update to resolve
     `REKOL_HOME` (fallback `MEMORY_HOME`), keep `MEMORY.md` filename, and change
     the hint text `run memory-search` → `run rekol search`. Update the
     `[memory] ...` banner brand text to `[rekol] ...` (Leon to confirm wording).
   - `posttooluse-snippet.json` / `sessionend-snippet.json`: grep for `memory-tools`
     / `memory-` command names and rebrand to `rekol ...`.

2. **`skill/memory/skill.md`:** rebrand prose and the `memory-search` mention
   (~line 8/17) to `rekol search`; `$MEMORY_HOME` → `$REKOL_HOME` (note fallback).
   **Directory rename decision:** the skill currently lives at `skill/memory/`.
   Renaming to `skill/rekol/` is cleaner but the skill *name* `memory` is what users
   trigger and may be referenced elsewhere. **Flag for Leon** (see self-review) —
   default plan keeps the dir as `skill/memory/` and only rebrands the content,
   to avoid breaking skill discovery.

3. **`template/`:** `memory.config.yaml.example`, `MEMORY.md`, and the `*.example`
   files — grep for `memory-tools` / `memory-` commands and rebrand. Keep the
   `MEMORY.md` filename and the `memory.config.yaml` filename unless Leon decides to
   rename them (flagged — renaming the config filename would require a matching
   change in `config.py` which currently reads `memory.config.yaml`).

4. **Docs + top-level prose:** `README.md`, `CHANGELOG.md`, `CONTRIBUTING.md`,
   `READ-ME-CLAUDE.md`, `docs/*.md`, `.github/PULL_REQUEST_TEMPLATE.md`,
   `.github/ISSUE_TEMPLATE/*`, `CODEOWNERS`. Replace `memory-tools` → `rekol`, brand
   as **REKOL**, update example command lines to the unified `rekol <sub>` form, and
   update any `git clone .../memory-tools` URLs to `.../rekol`.
   - Add a short CHANGELOG entry under an Unreleased/`0.1.0` heading noting the
     rebrand and the env-var fallback.

5. **Sweep for stragglers** (everything except code, which Task 2 already cleaned):
   ```
   grep -rni "memory-tools" . --exclude-dir=.git --exclude-dir=.venv-dev \
     --exclude-dir=.mypy_cache --exclude-dir=.ruff_cache --exclude-dir=.pytest_cache
   ```
   Review each remaining hit; legitimate fallback references to `MEMORY_HOME` /
   `MEMORY_TOOLS_HOME` are expected and should stay.

6. **GATE:** `.venv-dev/bin/pytest -q` (text changes should not affect tests; if a
   test asserts on banner/help text, update that test).

7. Commit:
   ```
   docs: rebrand memory-tools -> REKOL across docs, hooks, skill, templates
   ```

---

### Task 8 — GitHub repo rename (documented operational step)

**Goal:** rename the remote repo and repoint the local clone. Cheap — GitHub
auto-redirects the old URL, so existing clones and links keep resolving.

1. Rename via the CLI:
   ```
   gh repo rename rekol
   ```
   (Run from inside the repo; renames `leonkatz/memory-tools` → `leonkatz/rekol`.)
2. Repoint the local remote:
   ```
   git remote set-url origin git@github.com:leonkatz/rekol.git
   git remote -v   # verify
   ```
3. No source commit. Note: this is typically done **after** the rebrand PR merges,
   or independently — it does not need to be inside the feature branch. Leon decides
   timing.

---

### Task 9 — PyPI note (no execution)

The distribution name is now `rekol` (set in Task 3). The name `rekol` has been
verified available on PyPI. **Publishing is out of scope** for this plan; it happens
when v0.1 ships. No action here — recorded for traceability.

---

## Self-Review Checklist (run before opening the rebrand PR)

- [ ] **No `memory_tools` import remains:** `grep -rn "memory_tools" src/ tests/`
      prints nothing.
- [ ] **Every console script is accounted for in the unified group.** The 8 old
      scripts map 1:1 to the 8 subcommands and `tests/test_cli_group.py` asserts the
      exact command-name set
      `{search, index, capture, invalidate, propose, migrate, session-index, import}`.
      Cross-check against the original `[project.scripts]` (8 entries) — none dropped,
      none added.
- [ ] **Only one `[project.scripts]` entry** remains: `rekol = "rekol.cli:main"`.
- [ ] **`bin/` contains exactly one shim** (`bin/rekol`); the 8 old shims are removed.
- [ ] **Env-var precedence tested:** REKOL_HOME wins; MEMORY_HOME used when REKOL_HOME
      unset; clear error naming both when neither is set. `cli_migrate` uses the same
      rule (ideally via a shared `config.py` helper — no duplicated precedence logic).
- [ ] **Regression gate green:** `.venv-dev/bin/pytest -q` shows **167 + new tests**
      passing (expect 167 + ~3 env-var + ~5 CLI-group tests).
- [ ] **Static gates green:** `.venv-dev/bin/ruff check .`,
      `.venv-dev/bin/ruff format --check .`, `.venv-dev/bin/mypy src/rekol` all clean.
- [ ] **pre-commit hook** updated (`mypy src/rekol`), and `pre-commit run -a` passes.
- [ ] **CI config** (`.github/workflows/ci.yml`) — grep for `memory_tools` /
      `memory-tools` / `src/memory_tools`; update any hardcoded path so CI lints/types
      the renamed package.
- [ ] **Prose sweep:** `grep -rni "memory-tools" .` (excluding caches/.git/.venv-dev)
      returns only intentional `MEMORY_HOME` / `MEMORY_TOOLS_HOME` fallback references.
- [ ] **Live-setup safety:** with only `MEMORY_HOME` exported (no `REKOL_HOME`), a
      manual `rekol search "..."` against Leon's real memory root still works.

---

## Open Questions for Leon (decide before/while executing)

1. **`skill/memory/` directory + skill name `memory`** — rebrand content only (keep
   dir/name `memory`) or rename to `skill/rekol/` and rename the skill? Renaming may
   break existing skill triggers. *Default: keep `memory`, rebrand content.*
2. **`MEMORY.md` filename and `memory.config.yaml` filename** at the data root — keep
   (avoids breaking every existing memory root) or rename to a `REKOL.md` /
   `rekol.config.yaml` with back-compat reads? *Default: keep filenames.*
3. **`MEMORY_TOOLS_HOME` (install location var)** — rename to `REKOL_TOOLS_HOME` with
   fallback (Task 6 default) or leave entirely? *Default: rename with fallback.*
4. **SessionStart banner wording** — `[rekol] ...` vs keeping `[memory] ...`? Cosmetic.
5. **GitHub rename timing** — before or after the rebrand PR merges? *Default: after.*

---

## Commit / PR Summary

Commits (one per logical task):
1. `refactor: move package src/memory_tools -> src/rekol`
2. `refactor: update all imports memory_tools -> rekol`
3. `refactor: rename distribution to rekol; point pyproject + mypy at src/rekol`
4. `feat: unify console scripts into single rekol command group`
5. `feat: prefer REKOL_HOME, fall back to MEMORY_HOME for data dir`
6. `feat: install single rekol shim; export REKOL_HOME in installer`
7. `docs: rebrand memory-tools -> REKOL across docs, hooks, skill, templates`

Open a PR `feat/rekol-rebrand` → `main` titled **"REKOL full rebrand"**, listing the
self-review checklist results in the body. Do **not** self-merge — Leon reviews.
