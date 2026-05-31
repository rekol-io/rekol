# REKOL Genericization & Onboarding Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Date:** 2026-05-31
**Branch for execution:** `feat/rekol-genericization-onboarding` (create from `main`)
**Status:** Plan — not yet executed

**Goal:** Make REKOL safe and pleasant for a brand-new user: brand the data-level names to REKOL (with back-compat reads of the old names), strip the remaining first-party personal data, make legacy migration opt-in, make the docs-convert importer's file-type filter configurable, reframe sync as local-first, reserve the `scope:` frontmatter field, and add an interactive `rekol init` onboarding flow — then validate the whole from-zero path.

**Architecture:** Six sequenced phases, each one commit with its own GATE (Ruff + mypy + pytest, plus bats where the install path changes). The hard ordering rule: **a rename never lands without its back-compat fallback in the same commit.** Phase 1 (data-name rebrand + fallbacks) is the foundation every later phase references. Genericization (Phase 2) precedes onboarding (Phase 5); fresh-start validation (Phase 6) runs last, once generic content exists for it to assert against. New onboarding *detection* logic lives in a pure, unit-tested `onboarding/` subpackage; the interactive prompts live in a thin `cli_init.py` shell so the testable logic is decoupled from `click.prompt`.

**Tech Stack:** Python 3.11+, Click 8, PyYAML, `python-frontmatter`, pytest, bats; Ruff (lint+format), mypy. No new runtime dependencies.

**Decisions locked (from brainstorming, 2026-05-31):**
1. **Scope** = full "genericization **& onboarding**" (the rebrand plan's expanded definition), not the narrow Plan-1 one-liner.
2. **Data-level names** = **brand everything REKOL** with **back-compat reads** of the old names. Confirmed mechanism for the skill (per Claude Code docs): the **directory name is the invocation name** and there is **no `aliases:` frontmatter field**, so backward compatibility for `/memory` is provided by shipping **two skill directories** — a canonical `rekol/` and a thin `memory/` shim. Auto-triggering is driven by the `description` text, which both carry.

---

## File Structure

**New files**

- `src/rekol/onboarding/__init__.py` — subpackage marker + public re-exports.
- `src/rekol/onboarding/detect.py` — *pure* detection helpers (transcripts, cloud-sync dirs). No I/O prompts, no `click`. Unit-tested.
- `src/rekol/cli_init.py` — `rekol init`: the interactive onboarding shell (thin; orchestrates `detect` + existing CLIs).
- `skill/rekol/skill.md` — canonical skill (moved from `skill/memory/skill.md`, branded REKOL).
- `skill/memory/skill.md` — thin back-compat shim delegating to `/rekol` (replaces the old real skill in place).
- `template/rekol.config.yaml.example` — renamed from `template/memory.config.yaml.example`.
- `template/REKOL.md` — renamed from `template/MEMORY.md`.
- `tests/test_config_backcompat.py` — config-filename fallback tests.
- `tests/test_sessionstart_hook.py` — hook-snippet REKOL.md/MEMORY.md fallback test.
- `tests/test_model_scope.py` — `scope:` frontmatter field tests.
- `tests/test_docs_convert_extensions.py` — `--include`/`--exclude` filter tests.
- `tests/test_onboarding_detect.py` — detection-helper tests.

**Modified files**

- `src/rekol/config.py` — read `rekol.config.yaml`, fall back to `memory.config.yaml`; docstrings.
- `src/rekol/model.py` — add `scope` field (default `"private"`, `DEFAULT_SCOPE`); parsed but **not** validated in v0.1.
- `src/rekol/indexer.py` — docstring `MEMORY.md` → `REKOL.md` (cosmetic; no logic).
- `src/rekol/cli_session_index.py:66` — message `memory.config.yaml` → `rekol.config.yaml`.
- `src/rekol/docs_convert/__init__.py` — keep `TEXT_EXTENSIONS` as the default; document override.
- `src/rekol/docs_convert/extract.py` — `is_text_native` accepts an explicit extension set.
- `src/rekol/docs_convert/walk.py` — `group_sessions` threads an extension set down.
- `src/rekol/docs_convert/convert.py` — `convert_tree` accepts `text_extensions`.
- `src/rekol/cli_docs_convert.py` — `--include`/`--exclude` flags; stale `claude-session-index`/`memory-docs-convert` docstrings → `rekol`.
- `src/rekol/cli.py` — register the new `init` subcommand.
- `src/rekol/migrate/llm.py:3` — strip personal comment.
- `hooks/sessionstart-snippet.json` — cat `REKOL.md`, fall back to `MEMORY.md`; banner text.
- `install.sh` — skill src/dst (both dirs), `rekol.config.yaml` read, `--migrate` gate, sync wording, `rekol init` hook.
- `skill/rekol/skill.md` — `MEMORY.md` → `REKOL.md` references; trigger keywords intact.
- `template/always/identity.md.example`, `template/REKOL.md` — `scope:` + REKOL naming.
- `tests/test_indexer.py:33`, `tests/test_config.py:59` — de-personalize fixtures/comments.
- `tests/test_install.bats` — re-enable the skipped full-install test against generic content.
- `README.md`, `READ-ME-CLAUDE.md`, `CHANGELOG.md` — sync wording, quickstart, changelog entry.

**Intentionally NOT changed**

- `src/rekol/migrate/archive.py`, `src/rekol/migrate/discover.py`, `src/rekol/migrate/migrator.py` — their `MEMORY.md` references point at **Claude Code's autoMemory file** (`~/.claude/projects/<slug>/memory/MEMORY.md`), a different file from REKOL's data-root index. Renaming them would break legacy-migration detection. Leave as-is.
- `CODEOWNERS`, `CODE_OF_CONDUCT.md` contact email, `README.md` repo URL — legitimate OSS maintainer metadata (Leon owns the project), not personal data to strip.

---

## Phase 0 — Branch

- [ ] **Step 1: Create the feature branch from `main`**

```bash
cd ~/Library/CloudStorage/Dropbox/github/memory-tools
git checkout main && git pull --ff-only
git checkout -b feat/rekol-genericization-onboarding
. .venv-dev/bin/activate    # dev venv from Plan 1
```

---

## Phase 1 — Data-name rebrand + back-compat reads

The foundation. Every rename ships with its fallback in the same commit.

### Task 1.1 — `config.py`: read `rekol.config.yaml`, fall back to `memory.config.yaml`

**Files:**
- Test: `tests/test_config_backcompat.py` (create)
- Modify: `src/rekol/config.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config_backcompat.py
"""Config filename back-compat: rekol.config.yaml is preferred; memory.config.yaml still works."""

from __future__ import annotations

from pathlib import Path

import pytest

from rekol.config import load_config


def _write(root: Path, name: str, body: str) -> None:
    (root / name).write_text(body, encoding="utf-8")


def test_reads_new_filename(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REKOL_HOME", str(tmp_path))
    monkeypatch.delenv("MEMORY_HOME", raising=False)
    _write(tmp_path, "rekol.config.yaml", "chunk_max_bytes: 999\n")
    cfg = load_config()
    assert cfg.chunk_max_bytes == 999


def test_falls_back_to_legacy_filename(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REKOL_HOME", str(tmp_path))
    monkeypatch.delenv("MEMORY_HOME", raising=False)
    _write(tmp_path, "memory.config.yaml", "chunk_max_bytes: 777\n")
    cfg = load_config()
    assert cfg.chunk_max_bytes == 777


def test_new_filename_wins_when_both_present(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REKOL_HOME", str(tmp_path))
    monkeypatch.delenv("MEMORY_HOME", raising=False)
    _write(tmp_path, "rekol.config.yaml", "chunk_max_bytes: 111\n")
    _write(tmp_path, "memory.config.yaml", "chunk_max_bytes: 222\n")
    cfg = load_config()
    assert cfg.chunk_max_bytes == 111
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv-dev/bin/pytest tests/test_config_backcompat.py -v`
Expected: `test_reads_new_filename` and `test_new_filename_wins_when_both_present` FAIL (only `memory.config.yaml` is read today).

- [ ] **Step 3: Implement the fallback in `load_config`**

In `src/rekol/config.py`, replace the single-filename line (currently line 101):

```python
    root = Path(os.path.expanduser(env))
    config_file = root / "memory.config.yaml"
```

with the preferred-then-legacy lookup:

```python
    root = Path(os.path.expanduser(env))
    # rekol.config.yaml is the current name; memory.config.yaml is read as a
    # fallback so memory roots created by older versions keep working untouched.
    config_file = root / "rekol.config.yaml"
    if not config_file.exists():
        config_file = root / "memory.config.yaml"
```

Update the three docstrings that name the file (module docstring line 1, `Config` docstring line 42, `load_config` docstring line 81): change `$REKOL_HOME/memory.config.yaml` → `$REKOL_HOME/rekol.config.yaml (memory.config.yaml as fallback)`.

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv-dev/bin/pytest tests/test_config_backcompat.py -v`
Expected: all three PASS.

- [ ] **Step 5: Update the in-tree references to the config filename**

In `src/rekol/cli_session_index.py:66`, change:

```python
            "Set session_search_enabled: true in memory.config.yaml to enable."
```
to:
```python
            "Set session_search_enabled: true in rekol.config.yaml to enable."
```

- [ ] **Step 6: Rename the template config example**

```bash
git mv template/memory.config.yaml.example template/rekol.config.yaml.example
```

Edit the header comment (line 1) of `template/rekol.config.yaml.example`:

```yaml
# Copy to $REKOL_HOME/rekol.config.yaml and edit ($MEMORY_HOME accepted as fallback).
```

### Task 1.2 — `install.sh`: read `rekol.config.yaml` for git_track; seed the renamed example

**Files:**
- Modify: `src/install.sh` (repo root `install.sh`)

- [ ] **Step 1: Update the `git_track` config read (line ~436)**

In `install.sh`, replace:

```bash
CONFIG_YAML="${RESOLVED_HOME}/memory.config.yaml"
```
with a preferred-then-legacy resolution:
```bash
# rekol.config.yaml is the current name; fall back to memory.config.yaml so an
# existing root created by an older install is still read.
CONFIG_YAML="${RESOLVED_HOME}/rekol.config.yaml"
[[ -f "${CONFIG_YAML}" ]] || CONFIG_YAML="${RESOLVED_HOME}/memory.config.yaml"
```

- [ ] **Step 2: Confirm no other hardcoded `memory.config.yaml` / `*.example` read remains**

Step 1 already fixes the one direct config read (line 436). Sweep for any *other* hardcoded reference that bypasses `load_config()`'s fallback or the `*.example` glob:

Run: `grep -nE "memory\.config|\.example|seed|template" install.sh`
Expected: the only `memory.config` hit is the fallback you just added on line 436; seeding globs `template/*.example` (line ~231). If a literal `memory.config.yaml.example` is hardcoded in the seeding step, change it to `rekol.config.yaml.example`.

### Task 1.3 — `REKOL.md` data-root index, with `MEMORY.md` fallback in the hook

`MEMORY.md` is read at runtime in exactly **one** place: the SessionStart hook cats it. No Python reads the data-root `MEMORY.md`.

**Files:**
- Test: `tests/test_sessionstart_hook.py` (create)
- Modify: `hooks/sessionstart-snippet.json`
- Rename: `template/MEMORY.md` → `template/REKOL.md`
- Modify: `src/rekol/indexer.py:208` (docstring only)

- [ ] **Step 1: Write the failing test (the hook prefers REKOL.md, falls back to MEMORY.md)**

```python
# tests/test_sessionstart_hook.py
"""The SessionStart hook command cats REKOL.md when present, else MEMORY.md."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SNIPPET = REPO_ROOT / "hooks" / "sessionstart-snippet.json"


def _hook_command() -> str:
    data = json.loads(SNIPPET.read_text(encoding="utf-8"))
    # Snippet shape: {"hooks":{"SessionStart":[{"hooks":[{"command": "..."}]}]}}
    # Walk to the single command string regardless of nesting depth.
    found: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            if "command" in node and isinstance(node["command"], str):
                found.append(node["command"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    assert len(found) == 1, f"expected exactly one command, got {len(found)}"
    return found[0]


def _run(command: str, home: Path) -> str:
    result = subprocess.run(
        ["bash", "-c", command],
        env={"REKOL_HOME": str(home), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout


def test_prefers_rekol_md(tmp_path: Path) -> None:
    (tmp_path / "REKOL.md").write_text("REKOL CONTENT\n", encoding="utf-8")
    (tmp_path / "MEMORY.md").write_text("LEGACY CONTENT\n", encoding="utf-8")
    out = _run(_hook_command(), tmp_path)
    assert "REKOL CONTENT" in out
    assert "LEGACY CONTENT" not in out


def test_falls_back_to_memory_md(tmp_path: Path) -> None:
    (tmp_path / "MEMORY.md").write_text("LEGACY CONTENT\n", encoding="utf-8")
    out = _run(_hook_command(), tmp_path)
    assert "LEGACY CONTENT" in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv-dev/bin/pytest tests/test_sessionstart_hook.py -v`
Expected: `test_prefers_rekol_md` FAILS (hook only knows `MEMORY.md`).

- [ ] **Step 3: Update the hook command**

In `hooks/sessionstart-snippet.json`, replace the `command` value so it resolves `REKOL.md` first and `MEMORY.md` second. New command string (single line in JSON, shown unescaped for readability):

```bash
HOME_DIR="${REKOL_HOME:-$MEMORY_HOME}"; IDX="$HOME_DIR/REKOL.md"; [ -f "$IDX" ] || IDX="$HOME_DIR/MEMORY.md"; if [ -n "$HOME_DIR" ] && [ -f "$IDX" ]; then echo "[rekol] $HOME_DIR loaded — consult $(basename "$IDX"), when/*.md, topics/*.md, or run rekol search"; cat "$IDX"; else echo '[rekol] memory home not configured (set REKOL_HOME)'; fi
```

JSON-escaped (what actually goes in the file, replacing the current line 9 `"command": "..."`):

```json
            "command": "HOME_DIR=\"${REKOL_HOME:-$MEMORY_HOME}\"; IDX=\"$HOME_DIR/REKOL.md\"; [ -f \"$IDX\" ] || IDX=\"$HOME_DIR/MEMORY.md\"; if [ -n \"$HOME_DIR\" ] && [ -f \"$IDX\" ]; then echo \"[rekol] $HOME_DIR loaded — consult $(basename \"$IDX\"), when/*.md, topics/*.md, or run rekol search\"; cat \"$IDX\"; else echo '[rekol] memory home not configured (set REKOL_HOME)'; fi"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv-dev/bin/pytest tests/test_sessionstart_hook.py -v`
Expected: both PASS.

- [ ] **Step 5: Rename the template index file**

```bash
git mv template/MEMORY.md template/REKOL.md
```

- [ ] **Step 6: Cosmetic docstring fix**

In `src/rekol/indexer.py:208`, change `` ``MEMORY.md`` (always-on, hand-curated) is the only `` → `` ``REKOL.md`` (always-on, hand-curated) is the only ``.

- [ ] **Step 7: Update the bats install tests for the renamed seeded files (do NOT defer to Phase 6)**

`tests/test_install.bats` runs the **real seeding** path in `--test-mode` and asserts on the old template names. These break the moment this phase lands, so they must be fixed in this commit — not at Phase 6. Update:
- Test 1 (line 45): `[ ! -f "${MEMORY_HOME}/MEMORY.md" ]` → `[ ! -f "${MEMORY_HOME}/REKOL.md" ]`.
- Test 2 (line 60): `[ -f "${MEMORY_HOME}/MEMORY.md" ]` → `[ -f "${MEMORY_HOME}/REKOL.md" ]`.
- Test 2 (lines 66–67): `[ -f "${MEMORY_HOME}/memory.config.yaml" ]` → `[ -f "${MEMORY_HOME}/rekol.config.yaml" ]`; `[ ! -f "${MEMORY_HOME}/memory.config.yaml.example" ]` → `[ ! -f "${MEMORY_HOME}/rekol.config.yaml.example" ]`.

(Test 3 keys off `always/identity.md`, which is unchanged in this phase.)

### Task 1.4 — Skill rebrand: canonical `rekol/` + `memory/` shim

Per Claude Code docs: directory name = invocation name; no `aliases:` field. So ship two dirs.

**Files:**
- Move: `skill/memory/skill.md` → `skill/rekol/skill.md` (then edit)
- Create (replace in place): `skill/memory/skill.md` (thin shim)
- Modify: `install.sh` skill-install step (lines ~32, 244–260)

- [ ] **Step 1: Move the real skill to the canonical directory**

```bash
mkdir -p skill/rekol
git mv skill/memory/skill.md skill/rekol/skill.md
```

- [ ] **Step 2: Edit `skill/rekol/skill.md` — set name + REKOL.md references**

Frontmatter `name:` becomes `rekol`; keep the trigger-rich `description` verbatim (it drives auto-triggering). In the body, change the two `MEMORY.md` references (lines 8 and 35 of the original) to `REKOL.md`:

```yaml
---
name: rekol
description: Persistent memory at $REKOL_HOME (falls back to $MEMORY_HOME). Trigger on "remember"/"save"/"forget", on a user correction, on a noun matching topics/<noun>.md or activity matching when/when-<activity>.md, or when a question might have a canonical source.
---
```

Body line 8: `Index: `REKOL.md` (always-on), `.index/INDEX.md` (auto-generated).`
Body step "Update `MEMORY.md` only if…" → "Update `REKOL.md` only if…".
Body line 14: "`REKOL.md` is already in context — scan for trigger pointers."

- [ ] **Step 3: Create the `memory/` shim (deliberately non-triggering)**

`skill/memory/skill.md` (new content — the directory name `memory` is what preserves the manual `/memory` invocation). **The shim's `description` must NOT repeat the trigger keywords.** Claude Code auto-triggers on `description` text; if the shim and the canonical `rekol` skill carried the same keywords, both would auto-fire on every memory event — double invocation, doubled context, and two (partly contradictory) instruction sets. Auto-triggering is owned solely by the canonical `rekol` skill; the shim exists only so a user who types `/memory` still lands somewhere sensible.

```markdown
---
name: memory
description: Compatibility alias for the rekol memory skill. Manually invoke as /memory; prefer /rekol. Does not auto-trigger — the rekol skill owns memory triggering.
---

# memory (compatibility alias)

This is a backward-compatibility alias for the `rekol` skill. `/memory` and
`/rekol` are interchangeable. Follow the `rekol` skill at
`~/.claude/skills/rekol/skill.md` for the full retrieval/capture protocol.
```

- [ ] **Step 4: Update `install.sh` to install both skill directories**

The current step installs one skill from `${COMPONENT_DIR}/skill/memory/skill.md` to `${SKILL_DIR}` where `SKILL_DIR="$HOME/.claude/skills/memory"` (line 32). Generalize it to install both. Replace the `readonly SKILL_DIR=...` line (32) with:

```bash
readonly SKILL_BASE="$HOME/.claude/skills"
```

Replace the Step 6 body (lines 244–260, the `if [[ "$DO_SKILL" == "1" ]]; then ... fi` block) with a loop over both skills:

```bash
if [[ "$DO_SKILL" == "1" ]]; then
  # Install both the canonical `rekol` skill and the `memory` back-compat shim.
  # Claude Code derives the /<name> trigger from the directory name and has no
  # aliases field, so the shim directory is what keeps `/memory` working.
  for skill_name in rekol memory; do
    skill_dst_dir="${SKILL_BASE}/${skill_name}"
    run "mkdir -p '${skill_dst_dir}'"

    local_skill_src="${COMPONENT_DIR}/skill/${skill_name}/skill.md"
    local_skill_dst="${skill_dst_dir}/skill.md"

    # Back up only when content differs — avoids churn on repeated installs
    if [[ -f "$local_skill_dst" ]] && ! cmp -s "${local_skill_src}" "${local_skill_dst}"; then
      local_skill_backup="${local_skill_dst}.bak-${TS}"
      say "backing up existing ${local_skill_dst} → ${local_skill_backup}"
      run "cp '${local_skill_dst}' '${local_skill_backup}'"
      log_journal "BACKED-UP ${local_skill_dst} -> ${local_skill_backup}"
    fi

    run "cp '${local_skill_src}' '${local_skill_dst}'"
    log_journal "INSTALLED skill ${local_skill_dst}"
  done
fi
```

### Task 1.5 — `REKOL_TOOLS_HOME` (verify, already present)

**Files:** `install.sh`

- [ ] **Step 1: Confirm the install-location var precedence**

Run: `grep -n "REKOL_TOOLS_HOME\|MEMORY_TOOLS_HOME" install.sh`
Expected: `TOOLS_HOME_DEFAULT="${REKOL_TOOLS_HOME:-${MEMORY_TOOLS_HOME:-$HOME/.local/share/rekol}}"` already present (line 29). No change needed — this task is a verification checkpoint only.

### Task 1.6 — Phase 1 GATE + commit

- [ ] **Step 1: Run the gates (including bats — Phase 1 renames seeded files)**

```bash
.venv-dev/bin/ruff check . && .venv-dev/bin/ruff format --check .
.venv-dev/bin/mypy src/rekol
.venv-dev/bin/pytest -q
bats tests/test_install.bats
```
Expected: all green. bats is in this phase's gate (not just Phase 6) because Task 1.3 Step 7 renames the seeded files the existing install tests assert on — they must pass here. If a pre-existing pytest test asserts on `MEMORY.md`/`memory.config.yaml` banner or message text, update that test to the new name (with a comment noting the fallback is still covered by the new back-compat tests).

- [ ] **Step 2: Commit**

```bash
git add -A
git commit -m "feat: brand data-level names REKOL with back-compat reads

Rename rekol.config.yaml / REKOL.md / skill 'rekol', each with a
fallback (memory.config.yaml / MEMORY.md / 'memory' shim) so existing
memory roots and /memory triggers keep working."
```

---

## Phase 2 — Personal-data strip + `scope:` reservation

### Task 2.1 — Reserve the `scope:` frontmatter field

**Files:**
- Test: `tests/test_model_scope.py` (create)
- Modify: `src/rekol/model.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_model_scope.py
"""scope: frontmatter field — defaults to 'private', any value is preserved (unread, unvalidated in v0.1)."""

from __future__ import annotations

from pathlib import Path

from rekol.model import parse_file

_BASE = """---
name: t
description: d
type: topic
{scope_line}---

body
"""


def _write(tmp_path: Path, scope_line: str) -> Path:
    p = tmp_path / "t.md"
    p.write_text(_BASE.format(scope_line=scope_line), encoding="utf-8")
    return p


def test_scope_defaults_to_private(tmp_path: Path) -> None:
    mf = parse_file(_write(tmp_path, ""))
    assert mf.scope == "private"


def test_scope_explicit_private(tmp_path: Path) -> None:
    mf = parse_file(_write(tmp_path, "scope: private\n"))
    assert mf.scope == "private"


def test_scope_shared_preserved(tmp_path: Path) -> None:
    mf = parse_file(_write(tmp_path, "scope: shared\n"))
    assert mf.scope == "shared"


def test_unknown_scope_is_preserved_not_rejected(tmp_path: Path) -> None:
    # v0.1 reserves but does NOT read or validate scope. A file that already
    # uses scope: informally (e.g. 'work') must still parse and index — never
    # be silently dropped from the index by a ValidationError.
    mf = parse_file(_write(tmp_path, "scope: work\n"))
    assert mf.scope == "work"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv-dev/bin/pytest tests/test_model_scope.py -v`
Expected: FAIL with `AttributeError: 'MemoryFile' object has no attribute 'scope'`.

- [ ] **Step 3: Add `scope` to the model (parse, do NOT validate)**

`parse_file` failures are swallowed by the indexer: `indexer.py` catches `ValidationError` and `continue`s (lines 143–147, 179–183, 219–221), silently dropping the file from the index. So a memory file already carrying `scope:` with some informal value (`work`, `team`, …) must NOT raise — it would silently vanish from search. v0.1 therefore **reserves the field but does not validate it**. A future shared-team store (v0.2) adds validation together with a migration.

In `src/rekol/model.py`, after the `ALLOWED_TYPES` constant (line 19) add:

```python
# scope is reserved for a future shared-team store but is NOT read or validated
# in v0.1. We accept any value (default "private") so that a memory file which
# already uses scope: informally still parses and indexes instead of being
# silently dropped. Validation + migration land when the shared store does.
DEFAULT_SCOPE = "private"
```

Add the field to the `MemoryFile` dataclass (after `type: str`, line 58), and document it in the docstring `Args:` block:

```python
    scope: str = DEFAULT_SCOPE
```

In `parse_file`, in the `MemoryFile(...)` constructor call, add `scope` alongside `type=...` (no validation block):

```python
        scope=str(meta.get("scope", DEFAULT_SCOPE)),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv-dev/bin/pytest tests/test_model_scope.py -v`
Expected: all PASS.

- [ ] **Step 5: Seed `scope: private` in the templates**

Add `scope: private` to the frontmatter of `template/always/identity.md.example` (after the `type: always` line) and add a one-line note to `template/REKOL.md` documenting that memory files may carry `scope: private` (default) — reserved for a future shared store.

### Task 2.2 — De-personalize first-party fixtures and comments

These are the only remaining personal-data hits after excluding legitimate OSS metadata (CODEOWNERS, CoC email, repo URL) and the design docs.

**Files:**
- Modify: `tests/test_indexer.py:33`
- Modify: `tests/test_config.py:59`
- Modify: `src/rekol/migrate/llm.py:3`

- [ ] **Step 1: Genericize the indexer test fixture**

In `tests/test_indexer.py:33`, change:
```python
        "# Identity\n\nLeon is a senior manager.\n",
```
to:
```python
        "# Identity\n\nAlex is a senior engineer.\n",
```
Check the surrounding test for any assertion on the string `"Leon"`/`"senior manager"` and update it to match (grep the test file for `Leon` and `senior manager`).

- [ ] **Step 2: De-personalize the config test comment**

In `tests/test_config.py:59`, change the comment `# Guards Leon's live setup: a shell that only exports MEMORY_HOME must` → `# Back-compat: a shell that only exports MEMORY_HOME (no REKOL_HOME) must`.

- [ ] **Step 3: De-personalize the migrate/llm comment**

In `src/rekol/migrate/llm.py:3`, change `We use Sonnet by default (Leon's preference).` → `We use Sonnet by default (good cost/quality tradeoff for classification).`

- [ ] **Step 4: Verify no first-party personal data remains**

```bash
grep -rniI "leon\b\|leonkatz\|dropbox" src/ tests/ template/ skill/ hooks/ \
  | grep -vi "leonkatz@\|CODEOWNERS\|github.com/leonkatz"
```
Expected: no hits except the deliberately-kept maintainer metadata. (`Dropbox` wording in `install.sh`/`README` is handled in Phase 4.)

### Task 2.3 — Phase 2 GATE + commit

- [ ] **Step 1: Gates**

```bash
.venv-dev/bin/ruff check . && .venv-dev/bin/ruff format --check .
.venv-dev/bin/mypy src/rekol
.venv-dev/bin/pytest -q
```
Expected: all green.

- [ ] **Step 2: Commit**

```bash
git add -A
git commit -m "feat: reserve scope: frontmatter field; strip first-party personal data

scope: private (default) is validated but unread in v0.1. De-personalize
test fixtures and comments; keep legitimate maintainer metadata."
```

---

## Phase 3 — Opt-in migration

`install.sh` currently runs `rekol migrate auto --commit --no-llm --quiet` unconditionally (lines 507–517). New users should not get the Bedrock/legacy path; gate it behind `--migrate`.

### Task 3.1 — Add a `--migrate` flag, default off

**Files:** `install.sh`

- [ ] **Step 1: Add the flag to the parser and usage block**

In the flag-parsing section (the `case` over `"$@"`, near the top), add a `--migrate` case that sets a `DO_MIGRATE=1` variable (default `0`). Add `DO_MIGRATE=0` to the "Mutable config" defaults. Add to the usage/comment block (lines 8–15):

```bash
#   --migrate       opt in to importing legacy ~/.claude/projects/*/memory/ content
```

- [ ] **Step 2: Gate the migrate step (lines 507–517)**

Replace the Step 10 body so migration only runs when opted in:

```bash
say "checking for legacy memory to migrate"
if [[ "${TEST_MODE}" == "1" ]]; then
  say "test-mode: skipping rekol migrate"
elif [[ "${DO_MIGRATE}" != "1" ]]; then
  say "skipping legacy migration (pass --migrate to import ~/.claude/projects/*/memory/ content)"
else
  # Use the just-installed unified rekol CLI; idempotent, silent on no-op.
  if "${TOOLS_HOME}/.venv/bin/rekol" migrate auto --commit --no-llm --quiet 2>&1 | sed 's/^/  /'; then
    log_journal "MIGRATED legacy memory (auto)"
  else
    say "rekol migrate auto failed (non-fatal)"
  fi
fi
```

Update the Step 10 comment block (lines 500–505) to say migration is **opt-in via `--migrate`**, and that `--no-llm` avoids needing Bedrock creds at install time.

### Task 3.2 — Phase 3 GATE + commit

- [ ] **Step 1: Gate (shell + bats subset that exercises test-mode install)**

```bash
.venv-dev/bin/pytest -q
bats tests/test_install.bats   # existing tests run in --test-mode (migrate already skipped there)
```
Expected: green. (`--test-mode` already skips migrate, so existing bats tests are unaffected; the real opt-in path is validated in Phase 6.)

- [ ] **Step 2: Commit**

```bash
git add -A
git commit -m "feat: make legacy migration opt-in behind install.sh --migrate

New installs no longer run rekol migrate auto by default; the Bedrock/legacy
import path is offered only when the user passes --migrate."
```

---

## Phase 4 — Configurable docs-convert + sync wording

### Task 4.1 — `--include` / `--exclude` for docs-convert

`is_text_native` is the single filter, used by `walk.group_sessions` (grouping) and `convert.convert_tree` (drop-counting). Thread an explicit extension set through both.

**Files:**
- Test: `tests/test_docs_convert_extensions.py` (create)
- Modify: `src/rekol/docs_convert/extract.py`, `walk.py`, `convert.py`, `cli_docs_convert.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_docs_convert_extensions.py
"""docs_convert honors an explicit text-extension set (the --include/--exclude basis)."""

from __future__ import annotations

from pathlib import Path

from rekol.docs_convert import TEXT_EXTENSIONS
from rekol.docs_convert.extract import is_text_native
from rekol.docs_convert.walk import group_sessions


def test_default_extensions_exclude_html(tmp_path: Path) -> None:
    assert is_text_native(tmp_path / "a.md") is True
    assert is_text_native(tmp_path / "a.html") is False


def test_explicit_set_includes_html(tmp_path: Path) -> None:
    exts = TEXT_EXTENSIONS | {"html"}
    assert is_text_native(tmp_path / "a.html", text_extensions=exts) is True


def test_group_sessions_threads_extension_set(tmp_path: Path) -> None:
    child = tmp_path / "session1"
    child.mkdir()
    (child / "note.html").write_text("<p>hi</p>", encoding="utf-8")
    # Default set drops .html → no groups
    assert group_sessions(tmp_path) == []
    # Explicit set including html → one group with the file
    groups = group_sessions(tmp_path, text_extensions=TEXT_EXTENSIONS | {"html"})
    assert len(groups) == 1
    assert groups[0].files[0].path.name == "note.html"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv-dev/bin/pytest tests/test_docs_convert_extensions.py -v`
Expected: FAIL (`is_text_native`/`group_sessions` take no `text_extensions` argument).

- [ ] **Step 3: Add the optional extension set to `extract.is_text_native`**

In `src/rekol/docs_convert/extract.py`:

```python
def is_text_native(path: Path, text_extensions: frozenset[str] | None = None) -> bool:
    """True when the file's extension is one we read as plain text.

    ``text_extensions`` overrides the default :data:`TEXT_EXTENSIONS` allowlist
    (the --include/--exclude basis). Extensions are lowercase, no leading dot.
    """
    exts = TEXT_EXTENSIONS if text_extensions is None else text_extensions
    return path.suffix.lstrip(".").lower() in exts
```

(`extract_text` itself does not filter by extension, so it is unchanged.)

- [ ] **Step 4: Thread it through `walk.group_sessions`**

In `src/rekol/docs_convert/walk.py`, give `group_sessions` the parameter and pass it to every `is_text_native` call:

```python
def group_sessions(
    source_dir: Path, text_extensions: frozenset[str] | None = None
) -> list[SessionGroup]:
```

Replace the two `is_text_native(p)` calls (the `_root` filter and the per-child `rglob` filter) with `is_text_native(p, text_extensions=text_extensions)`.

- [ ] **Step 5: Thread it through `convert.convert_tree`**

In `src/rekol/docs_convert/convert.py`, add the parameter and use it in both the drop-count rglob and the `group_sessions` call:

```python
def convert_tree(
    source_dir: Path,
    target_dir: Path,
    prefix: str,
    max_bytes: int,
    dry_run: bool = False,
    text_extensions: frozenset[str] | None = None,
) -> ConvertStats:
```

Change the unsupported-count line to `... and not is_text_native(p, text_extensions=text_extensions)` and the grouping line to `groups = group_sessions(source_dir, text_extensions=text_extensions)`.

- [ ] **Step 6: Run test to verify it passes**

Run: `.venv-dev/bin/pytest tests/test_docs_convert_extensions.py -v`
Expected: all PASS.

- [ ] **Step 7: Add `--include`/`--exclude` to the CLI and fix stale names**

In `src/rekol/cli_docs_convert.py`:
- Fix the stale docstrings (lines 1–11): `memory-docs-convert` → `rekol import`, `claude-session-index` → `rekol session-index`, `memory-search` → `rekol search`.
- **Replace the subprocess + PATH chaining (lines 84–99) with an in-process invocation of the sibling subcommand** — the current code shells out to `claude-session-index` and bails with `sys.exit(3)` if `shutil.which` can't find it on `PATH`. After install, the venv's `rekol` is only on `PATH` once `~/.zshrc` is sourced, so the subprocess can silently no-op. Invoke the command in-process instead (no `PATH` dependency, and it reuses session-index's own guards + stats):

```python
    if not index:
        return

    # Ingest the just-written transcripts by invoking the session-index
    # subcommand in-process. Lazy import to avoid the cli -> cli_docs_convert
    # -> cli import cycle. standalone_mode=False makes Click return/raise
    # instead of sys.exit, so a failure here doesn't kill the whole convert.
    from rekol.cli import main as rekol_cli

    click.echo("ingesting new transcripts (session-index --incremental) ...", err=True)
    try:
        rekol_cli(["session-index", "--incremental"], standalone_mode=False)
    except SystemExit as exc:  # some leaf commands still sys.exit on error paths
        if exc.code not in (0, None):
            click.echo(f"session-index exited {exc.code}; index may be partial.", err=True)
            sys.exit(exc.code)
```

  Remove the now-unused `import shutil` and `import subprocess` (keep `import sys` — it is still used by the `__main__` guard).
- Add the two options and compute the effective extension set:

```python
@click.option(
    "--include",
    default="",
    help="Comma-separated extra extensions to treat as text (no dots), e.g. "
    "'html,rst'. Added on top of the built-in allowlist.",
)
@click.option(
    "--exclude",
    default="",
    help="Comma-separated extensions to drop from the allowlist (no dots), "
    "e.g. 'json,csv'.",
)
```

Add `include: str, exclude: str` to the `main` signature, and before calling `convert_tree`:

```python
    from rekol.docs_convert import TEXT_EXTENSIONS

    def _split(raw: str) -> set[str]:
        return {e.strip().lstrip(".").lower() for e in raw.split(",") if e.strip()}

    text_extensions = (TEXT_EXTENSIONS | _split(include)) - _split(exclude)
```

Pass `text_extensions=text_extensions` into the `convert_tree(...)` call.

### Task 4.2 — Reframe sync as local-first

**Files:** `install.sh`, `README.md`, `READ-ME-CLAUDE.md`

- [ ] **Step 1: Reword `install.sh` sync language**

- Line 5: `#   REKOL_HOME — path to the memory root directory (Dropbox-backed recommended)` → `#   REKOL_HOME — path to the memory root directory (local; sync it however you like)`.
- Line 88 error message: replace `Point REKOL_HOME at a Dropbox-backed directory` with `Point REKOL_HOME at any directory (sync it via Dropbox/iCloud/git/Syncthing or keep it local)`.
- Step 8 (lines 415–425): keep writing `.dropboxignore` (harmless when Dropbox is absent) but generalize the comment to: "Keep the local vector index out of any file-sync (it is machine-specific, rebuildable, and would conflict across machines). We write `.dropboxignore`; for other sync tools exclude `.index/` yourself." Add `say` text reflecting that.

- [ ] **Step 2: Reword the docs**

- `READ-ME-CLAUDE.md:7`: `— typically a Dropbox-backed directory.` → `— a local folder you own; sync it across machines however you like (Dropbox, iCloud Drive, a git remote, Syncthing, or not at all). The vector index under `.index/` stays local and must be excluded from sync.`
- `README.md`: ensure the install/positioning copy says local-first; add a one-line "Sync (optional)" note matching the above. (Full quickstart copy is finalized in Phase 6.)

### Task 4.3 — Phase 4 GATE + commit

- [ ] **Step 1: Gates**

```bash
.venv-dev/bin/ruff check . && .venv-dev/bin/ruff format --check .
.venv-dev/bin/mypy src/rekol
.venv-dev/bin/pytest -q
```
Expected: green.

- [ ] **Step 2: Commit**

```bash
git add -A
git commit -m "feat: configurable docs-convert extensions; local-first sync wording

Add --include/--exclude to rekol import (HTML and others opt-in); reframe
REKOL_HOME as a local folder synced however the user likes, index excluded."
```

---

## Phase 5 — Interactive `rekol init` onboarding

Detection logic is pure and unit-tested in `onboarding/detect.py`; the interactive shell in `cli_init.py` is thin.

### Task 5.1 — Pure detection helpers

**Files:**
- Test: `tests/test_onboarding_detect.py` (create)
- Create: `src/rekol/onboarding/__init__.py`, `src/rekol/onboarding/detect.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_onboarding_detect.py
"""Pure onboarding detection: transcript discovery and cloud-sync candidates."""

from __future__ import annotations

from pathlib import Path

from rekol.onboarding.detect import CloudSyncDir, count_claude_transcripts, detect_cloud_sync_dirs


def test_count_transcripts_counts_jsonl_recursively(tmp_path: Path) -> None:
    (tmp_path / "projA").mkdir()
    (tmp_path / "projB" / "sub").mkdir(parents=True)
    (tmp_path / "projA" / "a.jsonl").write_text("{}", encoding="utf-8")
    (tmp_path / "projB" / "sub" / "b.jsonl").write_text("{}", encoding="utf-8")
    (tmp_path / "projB" / "notes.md").write_text("x", encoding="utf-8")
    assert count_claude_transcripts(tmp_path) == 2


def test_count_transcripts_missing_dir_is_zero(tmp_path: Path) -> None:
    assert count_claude_transcripts(tmp_path / "nope") == 0


def test_detect_cloud_sync_finds_existing_dirs(tmp_path: Path) -> None:
    dropbox = tmp_path / "Dropbox"
    dropbox.mkdir()
    candidates = {
        "Dropbox": dropbox,
        "iCloud Drive": tmp_path / "Library" / "Mobile Documents",  # absent
    }
    found = detect_cloud_sync_dirs(candidates)
    assert found == [CloudSyncDir(label="Dropbox", path=dropbox)]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv-dev/bin/pytest tests/test_onboarding_detect.py -v`
Expected: FAIL (`rekol.onboarding` does not exist).

- [ ] **Step 3: Create the subpackage**

`src/rekol/onboarding/__init__.py`:

```python
"""Onboarding helpers for `rekol init`.

Pure detection logic (no prompts) lives here so it is unit-testable; the
interactive shell lives in ``rekol.cli_init``.
"""

from .detect import CloudSyncDir, count_claude_transcripts, detect_cloud_sync_dirs

__all__ = ["CloudSyncDir", "count_claude_transcripts", "detect_cloud_sync_dirs"]
```

`src/rekol/onboarding/detect.py`:

```python
"""Pure detection helpers used by `rekol init` — no prompts, no side effects."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CloudSyncDir:
    """A detected cloud-sync folder offered as a REKOL_HOME location."""

    label: str
    path: Path


def count_claude_transcripts(projects_dir: Path) -> int:
    """Count Claude Code transcript files (``*.jsonl``) under ``projects_dir``.

    Returns 0 when the directory does not exist. This is the headline first-run
    value signal: a large count means install can turn an empty store into a
    searchable history of past work.
    """
    if not projects_dir.is_dir():
        return 0
    return sum(1 for _ in projects_dir.rglob("*.jsonl"))


def default_cloud_sync_candidates() -> dict[str, Path]:
    """Standard macOS cloud-sync folder candidates, keyed by display label."""
    home = Path(os.path.expanduser("~"))
    return {
        "Dropbox": home / "Dropbox",
        "iCloud Drive": home / "Library" / "Mobile Documents" / "com~apple~CloudDocs",
        "Google Drive": home / "Google Drive",
        "OneDrive": home / "OneDrive",
    }


def detect_cloud_sync_dirs(
    candidates: dict[str, Path] | None = None,
) -> list[CloudSyncDir]:
    """Return the subset of ``candidates`` that actually exist on disk.

    Order follows ``candidates`` insertion order so the output is deterministic.
    """
    if candidates is None:
        candidates = default_cloud_sync_candidates()
    return [CloudSyncDir(label=label, path=path) for label, path in candidates.items() if path.is_dir()]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv-dev/bin/pytest tests/test_onboarding_detect.py -v`
Expected: all PASS.

### Task 5.2 — `rekol init` interactive shell

**Files:**
- Create: `src/rekol/cli_init.py`
- Modify: `src/rekol/cli.py` (register the subcommand)

- [ ] **Step 1: Write `cli_init.py`**

The ordering follows the rebrand spec: transcript-index offer is the headline first step, then corpus import, then cloud-sync location, then opt-in migrate. Each prompt defaults to the safe/no-op answer so a bare `rekol init` with all-Enter does nothing destructive.

```python
"""rekol init: interactive first-run onboarding.

Detects what already exists on the machine and offers to ingest it, instead of
starting from an empty store. All prompts default to a safe no-op so pressing
Enter through the flow changes nothing. The detection logic is in
``rekol.onboarding.detect`` (pure, unit-tested); this module is the thin shell.

Sibling subcommands are invoked in-process (not via subprocess), so onboarding
does not depend on `rekol` being on PATH (it isn't until ~/.zshrc is sourced).
"""

from __future__ import annotations

import sys

import click

from rekol.config import load_config
from rekol.onboarding import count_claude_transcripts, detect_cloud_sync_dirs


@click.command(name="init")
@click.option(
    "--yes",
    is_flag=True,
    help="Accept the recommended default for every prompt (non-interactive).",
)
def main(yes: bool) -> None:
    """Interactively onboard a new REKOL install."""
    cfg = load_config()
    click.echo(f"REKOL home: {cfg.memory_home}")

    # 1) Headline: offer to index existing Claude Code transcripts.
    n_transcripts = count_claude_transcripts(cfg.claude_projects_dir)
    if n_transcripts > 0:
        if yes or click.confirm(
            f"Found {n_transcripts} past Claude Code sessions under "
            f"{cfg.claude_projects_dir}. Index them so REKOL can search your history?",
            default=True,
        ):
            _invoke(["session-index", "--full"])
    else:
        click.echo("No Claude Code transcripts found — skipping history indexing.")

    # 2) Offer to import an existing notes/docs corpus.
    if not yes and click.confirm(
        "Import an existing notes/docs folder (e.g. an Obsidian vault) now?",
        default=False,
    ):
        corpus = click.prompt("Path to the folder", type=click.Path(exists=True))
        _invoke(["import", corpus])

    # 3) Offer detected cloud-sync folders as a REKOL_HOME location reminder.
    cloud = detect_cloud_sync_dirs()
    if cloud:
        labels = ", ".join(c.label for c in cloud)
        click.echo(
            f"Detected cloud-sync folders ({labels}). REKOL_HOME can live in one so "
            "your markdown syncs across devices — but keep the .index/ directory out "
            "of sync (it is machine-specific and rebuildable)."
        )

    # 4) Opt-in legacy migration (off by default).
    if not yes and click.confirm(
        "Import legacy ~/.claude/projects/*/memory/ content into REKOL now?",
        default=False,
    ):
        _invoke(["migrate", "auto", "--commit", "--no-llm"])

    click.echo("rekol init complete.")


def _invoke(argv: list[str]) -> None:
    """Invoke a rekol subcommand in-process, surfacing failure without aborting init.

    Lazy import of the CLI group avoids the cli -> cli_init -> cli import cycle.
    standalone_mode=False makes Click return/raise instead of calling sys.exit,
    so one failed step does not kill the whole onboarding flow.
    """
    from rekol.cli import main as rekol_cli

    click.echo(f"  rekol {' '.join(argv)}", err=True)
    try:
        rekol_cli(argv, standalone_mode=False)
    except SystemExit as exc:  # some leaf commands still sys.exit on error paths
        if exc.code not in (0, None):
            click.echo(f"  (warning: rekol {argv[0]} exited {exc.code})", err=True)
    except click.ClickException as exc:
        exc.show()


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 2: Register `init` in the CLI group**

`src/rekol/cli.py` imports each leaf command as `... as <name>_cmd` (lines 11–18) and registers it with `main.add_command(<name>_cmd, name="...")` (lines 30–37). Add the import alongside the others:

```python
from rekol.cli_init import main as init_cmd
```

and the registration after the `import_cmd` line (line 37):

```python
main.add_command(init_cmd, name="init")
```

- [ ] **Step 3: Extend the CLI-group test**

`tests/test_cli_group.py` defines `EXPECTED_COMMANDS` (a set of the eight command names, lines 15–24) and asserts `set(rekol_main.commands.keys()) == EXPECTED_COMMANDS` (line 33). Add `"init",` to `EXPECTED_COMMANDS`.

Run: `.venv-dev/bin/pytest tests/test_cli_group.py -v`
Expected: PASS once `init` is registered. (`test_help_exits_zero_and_lists_subcommands` also iterates `EXPECTED_COMMANDS`, so it covers `init` too.)

### Task 5.3 — Wire `install.sh` to offer `rekol init`

**Files:** `install.sh`

- [ ] **Step 1: Offer onboarding at the end of a non-test install**

After the (now opt-in) migrate step and before "Done", add:

```bash
if [[ "${TEST_MODE}" != "1" ]]; then
  say "run 'rekol init' to index existing Claude Code history and import notes"
fi
```

(We *print the suggestion* rather than auto-launching an interactive prompt mid-install, so `install.sh` stays non-interactive and CI-safe. `rekol init` is the interactive entry point the user runs next.)

### Task 5.4 — Phase 5 GATE + commit

- [ ] **Step 1: Gates**

```bash
.venv-dev/bin/ruff check . && .venv-dev/bin/ruff format --check .
.venv-dev/bin/mypy src/rekol
.venv-dev/bin/pytest -q
```
Expected: green.

- [ ] **Step 2: Commit**

```bash
git add -A
git commit -m "feat: add interactive 'rekol init' onboarding

Detect past Claude Code transcripts (headline), offer corpus import, surface
cloud-sync folders, and offer opt-in migration. Detection logic is pure and
unit-tested; install.sh points new users at 'rekol init'."
```

---

## Phase 6 — Fresh-start validation + docs

### Task 6.1 — Re-enable the full-install bats test against generic content

**Files:** `tests/test_install.bats`

- [ ] **Step 1: Remove the skip and update the test**

In `tests/test_install.bats`, the test at line 159 (`install runs rekol migrate auto and succeeds when no legacy`) is skipped pending Plan 2. Migration is now opt-in, so rewrite it as a **from-zero full install** test (no `--migrate`, non-test-mode) that asserts a working search over the seeded generic template:

```bash
@test "full install seeds generic template and yields a working search" {
  # Plan 2: migration is now opt-in (no --migrate here), and the template is
  # genericized, so the from-zero install path can run end-to-end in CI.
  unset TEST_MODE || true
  run env -u TEST_MODE \
    REKOL_HOME="$TEST_TMP/mem" \
    HOME="$TEST_TMP/home" \
    "$BATS_TEST_DIRNAME/../install.sh" \
      --no-hook --no-skill --no-shellrc \
      --tools-home "$TEST_TMP/tools" --bin-dir "$TEST_TMP/bin"
  [ "$status" -eq 0 ]
  # Template seeded REKOL.md + identity example into the empty root
  [ -f "$TEST_TMP/mem/REKOL.md" ]
  # Search over the seeded content returns a hit (index was built by install)
  run env REKOL_HOME="$TEST_TMP/mem" "$TEST_TMP/tools/.venv/bin/rekol" search "identity" --top 3
  [ "$status" -eq 0 ]
}
```

(We use `--no-hook --no-skill --no-shellrc` to keep the test from mutating the developer's real `~/.claude`/`~/.zshrc`; the seed→index→search core is what this asserts. The `.dropboxignore` / skill-shim behaviors are covered by their own unit tests.)

- [ ] **Step 2: Run the bats suite**

Run: `bats tests/test_install.bats`
Expected: all pass, including the rewritten full-install test. Also update the earlier `.dropboxignore` assertion (line 76) if it referenced `MEMORY_HOME` — switch to `REKOL_HOME` to match the new default while keeping a back-compat case if present.

### Task 6.2 — README quickstart + CHANGELOG

**Files:** `README.md`, `CHANGELOG.md`

- [ ] **Step 1: Add the fresh-install quickstart to the README**

Add a "Quickstart (fresh install)" subsection documenting the from-zero path and the new onboarding entry point:

```markdown
## Quickstart (fresh install)

1. `git clone https://github.com/leonkatz/rekol && cd rekol`
2. Point REKOL at a folder you own: `export REKOL_HOME=~/rekol-memory`
   (sync it via Dropbox/iCloud/git/Syncthing or keep it local — the `.index/`
   directory stays local and is excluded from sync).
3. `./install.sh` — seeds the empty root from `template/`, builds the first
   index, and installs the hook + skill.
4. `rekol init` — indexes any existing Claude Code history and offers to import
   your notes.
5. Edit `always/identity.md`, then try `rekol search "..."` / `rekol capture`.
```

- [ ] **Step 2: Add a CHANGELOG entry**

Under the Unreleased / `0.1.0` heading, add:

```markdown
- Data-level names branded REKOL (`rekol.config.yaml`, `REKOL.md`, `rekol`
  skill) with back-compat reads of the legacy names (`memory.config.yaml`,
  `MEMORY.md`, `/memory` shim).
- `scope: private` frontmatter field reserved (validated, unread in v0.1).
- Legacy migration is now opt-in (`install.sh --migrate`).
- `rekol import` gained `--include`/`--exclude` for file-type selection.
- Sync reframed as local-first; `REKOL_HOME` is any folder you own.
- New `rekol init` interactive onboarding (transcript indexing, corpus import,
  cloud-sync detection, opt-in migration).
```

### Task 6.3 — Phase 6 GATE + commit

- [ ] **Step 1: Full gate**

```bash
.venv-dev/bin/ruff check . && .venv-dev/bin/ruff format --check .
.venv-dev/bin/mypy src/rekol
.venv-dev/bin/pytest -q
bats tests/test_install.bats
```
Expected: all green.

- [ ] **Step 2: Commit**

```bash
git add -A
git commit -m "test+docs: re-enable from-zero install test; add quickstart + changelog

Validate the genericized fresh-start path end-to-end and document the
60-second install + 'rekol init' onboarding."
```

---

## Self-Review Checklist (run before opening the PR)

- [ ] **Back-compat proven, not assumed:** new tests show `rekol.config.yaml` and `REKOL.md` win, and `memory.config.yaml` / `MEMORY.md` still load.
- [ ] **No rename without a fallback** landed in any single commit (config, index file, skill).
- [ ] **autoMemory `MEMORY.md` references in `migrate/*` untouched** — `grep -n MEMORY.md src/rekol/migrate/*.py` still present and correct.
- [ ] **Two skill dirs installed:** `~/.claude/skills/rekol/skill.md` (canonical, carries the trigger keywords) + `~/.claude/skills/memory/skill.md` (shim with a deliberately non-triggering description so it does not double-fire).
- [ ] **`scope:` reserved, not validated:** default is `private`, any value is preserved, and an unknown value (`scope: work`) still parses + indexes — never dropped by a `ValidationError`. Templates seed `scope: private`.
- [ ] **No subprocess/PATH dependency:** `rekol import` (`--index`) and `rekol init` invoke sibling subcommands in-process (`standalone_mode=False`), not via `subprocess` + `shutil.which`.
- [ ] **bats runs in Phase 1:** the install tests assert the renamed seeded files (`REKOL.md`, `rekol.config.yaml`) and pass in the Phase 1 gate, not just Phase 6.
- [ ] **Personal data gone:** `grep -rniI "leon\b\|dropbox" src/ tests/ template/ skill/ hooks/` returns only maintainer metadata + the `.dropboxignore` mechanism.
- [ ] **Migration is opt-in:** a default `install.sh` run (no `--migrate`) does not call `rekol migrate`.
- [ ] **docs-convert flags work:** `--include html` indexes HTML; `--exclude json` drops JSON; default unchanged.
- [ ] **`rekol init` is no-op-safe:** all-Enter (or `--yes` on an empty machine) changes nothing destructive; `init` is in the CLI-group test set.
- [ ] **Fresh-start test runs for real:** the previously-skipped install test is re-enabled and green.
- [ ] **Static gates green:** `ruff check`, `ruff format --check`, `mypy src/rekol`, `pytest -q`, `bats tests/test_install.bats`.
- [ ] **Stale post-rebrand names fixed in `cli_docs_convert.py`** (`claude-session-index`/`memory-docs-convert`/`memory-search` → `rekol …`).

## Out of scope (deferred)

- **Core/adapter boundary refactor** (the original "Plan 3") — `core/` vs `adapters/claude_code/` physical split and the boundary test. Not touched here.
- **MCP adapter, `memory digest`, shared-team server, PyPI publish, Homebrew tap** — roadmap items per the extraction design.
- **docs-convert generic-record retarget** (it still emits synthetic Claude `.jsonl`) — a v0.2 cleanup once the core/adapter seam is firm.
