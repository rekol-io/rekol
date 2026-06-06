# Durable Transcript Archiving — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Every step is test-first (see superpowers:test-driven-development): write the failing test, watch it fail for the right reason, write the minimal code, watch it pass, commit.

**Status:** ready to implement, 2026-06-05 · branch `feat/transcript-archiving` · implements [#8](https://github.com/rekol-io/rekol/issues/8). Authoritative spec: [`docs/transcript-archiving-design.md`](transcript-archiving-design.md) (approved, do not re-litigate).

**Goal:** Insert a durable, rekol-owned transcript archive between the ephemeral `~/.claude/projects/**/*.jsonl` source and the disposable `sessions.db` index, and make the index always rebuild *from the archive* — so a session survives even if Claude Code deletes the original and the cache is wiped.

**Architecture:** A new **DB-free** module `src/rekol/sessions/archive.py` does pure file ops (copy-if-changed with a divergence sidecar) against a JSON manifest. A new `src/rekol/cli_archive.py` (`rekol archive`) exposes manual sync, `--from-index` backfill, and `--prune`/`--clear`. `cli_session_index.py` is edited to **archive-sync first, then ingest from the archive** (soft-failing back to live on `OSError`). It writes `sessions.db` (NOT the curated `index.db`), so it relies on that DB's own WAL + 30s `busy_timeout` for concurrency and does **not** take the curated `index_write_lock` — reusing it would hang the SessionEnd hook behind a curated rebuild and couple two independent subsystems (see the design's "Locking" section). Archive-sync only writes flat files (idempotent reconcile), needing no lock. `config.py` gains three keys + `resolve_archive_dir()` + a shared `path_is_excluded()` matcher (the reusable foundation for #5). `cli_doctor.py` gains an archive health line. Install/uninstall gain archive flags, a disclosure line, and prompt-before-delete.

**Tech Stack:** Python 3.11+, Click 8, stdlib `json`/`pathlib`/`fnmatch`/`hashlib`/`shutil`, pytest (hermetic via conftest), bats (install/uninstall). CI gate: ruff · ruff format · mypy · pytest · bats install.

**House style (enforced everywhere):** verbose descriptive names; full type hints; WHY-comments only; no bare `except` (catch `OSError` for filesystem ops, `sqlite3.DatabaseError` for DB ops); soft-fail discipline — **archiving never blocks indexing and the hook exits 0**.

---

## Scope

**In (v1):** core durability (archive sink + ingest-from-archive + lossless rebuild), the copy-if-changed primitive with divergence sidecar, backfill-from-index (auto-once + manual), the **archive-side exclude slice** of #5 (config + matcher + one consumer), manual prune, doctor line, install/uninstall/README.

**Out (deferred — do NOT build):** index purge (destructive removal from `sessions.db`/`index.db`), export (`rekol export`), edit/delete individual indexed data, auto-compaction/retention. These are separate issues per the design's "Deferred work" section.

---

## File Structure

**Create:**
- `src/rekol/sessions/archive.py` — the archive sink. DB-free. Owns: `ArchiveStats`/`ArchiveFileResult`/`BackfillStats`/`PruneStats` dataclasses; `load_manifest`/`save_manifest`; `archive_file` (copy-if-changed + divergence sidecar primitive); `archive_directory` (reconcile live∩not-excluded → archive); `backfill_from_index` (reconstruct missing `.jsonl` from `sessions.db`, exclude-aware); `prune` (flat-file clear/trim). Pure filesystem + JSON; **imports no DB module** (it receives an already-open `SessionStore` for backfill, and only reads it).
- `src/rekol/cli_archive.py` — `rekol archive` Click command: default = sync; `--from-index` = backfill; `--prune`/`--clear` = manual retention. Resolves paths from `Config`, soft-fails. Takes NO lock (it touches `sessions.db` and flat files, never the curated `index.db`; see the design's "Locking").
- `tests/test_sessions_archive.py` — unit tests for `archive.py` (copy-if-changed matrix, reconcile, backfill round-trip, prune).
- `tests/test_config_archive.py` — `resolve_archive_dir` precedence + `archive_dir` property + the three new keys + `path_is_excluded`/`.rekolignore`.
- `tests/test_cli_archive.py` — `rekol archive` command smoke + backfill marker/notice + soft-fail.
- `tests/test_archive_integration.py` — the **headline regression** (archive → delete live `.jsonl` → `session-index --full` → still searchable), soft-fail-falls-back-to-live, exclude end-to-end.
- `tests/fixtures/diverged_session.jsonl` — a rewritten/compacted variant of `sample_session.jsonl` (different uuids, shorter) to exercise the divergence sidecar.

**Modify:**
- `src/rekol/config.py` — add `archive_enabled`/`archive_dir`/`exclude_paths` to `DEFAULTS`; add `resolve_archive_dir()`; add `Config.archive_enabled`/`Config.exclude_paths` fields + `Config.archive_dir` property; add module-level `path_is_excluded(path, patterns)` + `load_rekolignore_patterns(root)`; extend `load_config()`.
- `src/rekol/cli_session_index.py` — before ingest: archive-sync (when `archive_enabled`), then ingest from the **archive dir** instead of the live projects dir; soft-fail to live on `OSError`; auto-backfill-once. Takes NO curated `index_write_lock` (it writes `sessions.db`, not `index.db`; concurrency is the session DB's WAL + busy_timeout — see the design's "Locking").
- `src/rekol/cli_doctor.py` — add `_check_archive(cfg)` returning a `Finding`; call it from `run_doctor`; placeholder-mount warning reuses `onboarding.detect`.
- `src/rekol/cli.py` — register `main.add_command(archive_cmd, name="archive")`.
- `install.sh` — `--no-archive`, `--archive-dir P`, a `--help` block, a disclosure line, and a post-backfill `rekol archive --from-index`.
- `uninstall.sh` — preserve the archive by default; delete only on prompt or `--purge-archive`; `--yes` keeps it; `--help` updated.
- `README.md` — "Install options" section listing every flag incl. archive flags + the disclosure.
- `tests/test_config.py` — extend for the three new keys in the returned `Config` (forward-compat: unknown-key drop still holds).
- `tests/test_cli_doctor.py` — extend with the archive finding.
- `tests/test_install.bats` — `--no-archive` seeds `archive_enabled:false`; `--archive-dir` is honored; disclosure line present.
- `tests/test_uninstall.bats` — archive preserved by default; removed with `--purge-archive`; `--yes` keeps it.

---

## Phase ordering & dependencies

```
Phase 1  config + matcher (foundation)        ─┐
Phase 2  archive.py copy-if-changed primitive ─┤ (needs P1 manifest/types only)
Phase 3  archive_directory reconcile + exclude ┤ (needs P2 archive_file + P1 matcher)
Phase 4  backfill_from_index                   ┤ (needs P1 + reads SessionStore.iter)
Phase 5  rekol archive CLI                     ┤ (needs P2–P4 + P1)
Phase 6  ingest-from-archive wiring  ★HEADLINE ┤ (needs P3 + P5; this is the data-loss fix)
Phase 7  doctor archive line                   ┤ (needs P1; independent of P6)
Phase 8  install / uninstall / README / bats   ─┘ (needs P1 keys + P5 CLI)
```

Phases 1→6 are strictly sequential on the durability path. Phase 7 (doctor) depends only on Phase 1 and may be done any time after it. Phase 8 (shell) depends on Phase 1 (the `archive_enabled` config key) and Phase 5 (the `rekol archive --from-index` command) — do it last. The exclude-side slice is woven into Phases 1 (matcher), 3 (reconcile honors + retroactively removes), and 4 (backfill skips); its end-to-end integration test lands in Phase 6.

**Commit cadence:** one commit per task (after its tests are green). Branch is already `feat/transcript-archiving`; commit there, do not open a PR until the whole plan is green and the user asks.

---

## Phase 1: Config keys + resolve_archive_dir + exclude matcher

The reusable foundation. No archive behavior yet — just resolution and the shared exclude matcher (which full #5 reuses later with **no rework**).

### Task 1.1: Three new config keys flow through `Config`

**Files:**
- Modify: `src/rekol/config.py`
- Test: `tests/test_config_archive.py` (create)

- [ ] **Step 1.1.1: Write the failing test**

Create `tests/test_config_archive.py`:

```python
"""Tests for archive config keys, resolve_archive_dir precedence, and the
shared exclude matcher (the reusable #5 foundation)."""

from __future__ import annotations

import os
from pathlib import Path

from rekol.config import load_config


def test_archive_keys_have_documented_defaults(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("REKOL_HOME", str(tmp_path))
    cfg = load_config()
    # archive is on by default (default-ON honesty: disclosure, not opt-in).
    assert cfg.archive_enabled is True
    # exclude_paths defaults to empty (nothing excluded until the user opts in).
    assert cfg.exclude_paths == []


def test_archive_enabled_false_from_yaml(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("REKOL_HOME", str(tmp_path))
    (tmp_path / "rekol.config.yaml").write_text("archive_enabled: false\n")
    cfg = load_config()
    assert cfg.archive_enabled is False


def test_exclude_paths_list_from_yaml(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("REKOL_HOME", str(tmp_path))
    (tmp_path / "rekol.config.yaml").write_text(
        "exclude_paths:\n  - '*/secret-project/*'\n  - '*/clientwork/*'\n"
    )
    cfg = load_config()
    assert cfg.exclude_paths == ["*/secret-project/*", "*/clientwork/*"]
```

- [ ] **Step 1.1.2: Run to verify it fails**

Run: `pytest tests/test_config_archive.py -v`
Expected: FAIL — `AttributeError: 'Config' object has no attribute 'archive_enabled'`.

- [ ] **Step 1.1.3: Add the keys to `DEFAULTS`**

In `src/rekol/config.py`, extend the `DEFAULTS` dict (after `temporal_confirm_interval_days=180,`):

```python
    # --- Durable transcript archive (#8) ---
    archive_enabled=True,  # default-ON: we disclose at install, not opt-in
    archive_dir=None,  # None → resolve_archive_dir() picks the XDG default
    exclude_paths=[],  # glob patterns for project/cwd paths never archived/indexed
```

- [ ] **Step 1.1.4: Add the fields to `Config` and the loader**

Add to the `Config` dataclass (after `temporal_confirm_interval_days: int`):

```python
    archive_enabled: bool
    exclude_paths: list[str]
    # NOTE: the RESOLVED archive_dir is intentionally NOT a stored field — it is
    # resolved lazily via the archive_dir property (mirrors index_dir), so an env
    # override is honored at call time rather than frozen at load time. We store
    # only the RAW config value here (no leading underscore — this is a public
    # dataclass field; the resolved value comes from the property).
    archive_dir_raw: str | None
```

In `load_config()`, extend the `Config(...)` constructor call (after `temporal_confirm_interval_days=...`):

```python
        archive_enabled=bool(data["archive_enabled"]),
        exclude_paths=list(data["exclude_paths"]),
        archive_dir_raw=(
            str(data["archive_dir"]) if data["archive_dir"] is not None else None
        ),
```

- [ ] **Step 1.1.5: Run to verify the three tests pass**

Run: `pytest tests/test_config_archive.py -v`
Expected: PASS (3 tests). Then `pytest tests/test_config.py tests/test_config_backcompat.py -v` — still green (unknown-key drop is unaffected; the new keys are in `DEFAULTS`).

- [ ] **Step 1.1.6: Commit**

```bash
git add src/rekol/config.py tests/test_config_archive.py
git commit -m "feat: add archive_enabled/archive_dir/exclude_paths config keys

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 1.2: `resolve_archive_dir()` + `archive_dir` property

Mirrors `resolve_index_dir`, but the archive is **machine-level, NOT hashed per `$REKOL_HOME`** (transcripts come from `~/.claude/projects` regardless of which memory home is active) and lives under `XDG_DATA_HOME`, not `XDG_CACHE_HOME` (durable, not a cache).

> **Known tension (accepted, not fixed in v1):** the archive is machine-level (one per machine) but `exclude_paths` is per-`$REKOL_HOME` config, so two homes sharing one machine could apply different excludes to the same shared archive (whichever ran last wins the forward/retroactive sweep). This is acceptable for v1 — multi-home-on-one-machine is rare and excludes are coarse — and is noted so a future per-home or machine-level exclude policy is a deliberate follow-up, not a surprise.

**Files:**
- Modify: `src/rekol/config.py`
- Test: `tests/test_config_archive.py`

- [ ] **Step 1.2.1: Write the failing test**

Append to `tests/test_config_archive.py`:

```python
from rekol.config import resolve_archive_dir


def test_resolve_archive_dir_env_override_wins(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("REKOL_ARCHIVE_DIR", str(tmp_path / "explicit"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert resolve_archive_dir(config_archive_dir="/from/config") == tmp_path / "explicit"


def test_resolve_archive_dir_config_key_second(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("REKOL_ARCHIVE_DIR", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert resolve_archive_dir(config_archive_dir=str(tmp_path / "cfg")) == tmp_path / "cfg"


def test_resolve_archive_dir_xdg_default_last(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("REKOL_ARCHIVE_DIR", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert resolve_archive_dir(config_archive_dir=None) == tmp_path / "xdg" / "rekol" / "archive"


def test_resolve_archive_dir_is_not_hashed_per_home(tmp_path: Path, monkeypatch) -> None:
    """One archive per machine: the path must NOT depend on REKOL_HOME (unlike
    the index cache, which is hashed per-home). Two different homes resolve to
    the SAME archive dir."""
    monkeypatch.delenv("REKOL_ARCHIVE_DIR", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    first = resolve_archive_dir(config_archive_dir=None)
    monkeypatch.setenv("REKOL_HOME", str(tmp_path / "home-b"))
    second = resolve_archive_dir(config_archive_dir=None)
    assert first == second


def test_config_archive_dir_property_uses_resolver(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("REKOL_HOME", str(tmp_path))
    monkeypatch.delenv("REKOL_ARCHIVE_DIR", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    cfg = load_config()
    assert cfg.archive_dir == tmp_path / "xdg" / "rekol" / "archive"
```

- [ ] **Step 1.2.2: Run to verify it fails**

Run: `pytest tests/test_config_archive.py -k resolve_archive_dir -v`
Expected: FAIL — `ImportError: cannot import name 'resolve_archive_dir'`.

- [ ] **Step 1.2.3: Implement `resolve_archive_dir`**

In `src/rekol/config.py`, add after `resolve_index_dir`:

```python
def resolve_archive_dir(config_archive_dir: str | None) -> Path:
    """Resolve the durable, rekol-owned transcript archive directory.

    Unlike :func:`resolve_index_dir`, the archive is **machine-level** — NOT
    hashed per ``$REKOL_HOME``. Transcripts come from ``~/.claude/projects``
    regardless of which memory home is active, so one archive per machine is
    correct; splitting per-home would only duplicate the same transcripts.

    SECURITY: the archive holds verbatim prompts (and any pasted secrets), so it
    defaults to a LOCAL, non-synced, non-cache location — the same posture that
    moved ``sessions.db`` out of ``$REKOL_HOME`` (#10/#13). It lives under
    ``XDG_DATA_HOME`` (durable) rather than ``XDG_CACHE_HOME`` (disposable),
    because it is the source of truth a rebuild reads from, not a rebuildable
    cache.

    Resolution order:
        1. ``$REKOL_ARCHIVE_DIR`` — explicit override, used verbatim (expanded).
        2. ``config_archive_dir`` — the ``archive_dir`` config key, if set.
        3. ``${XDG_DATA_HOME:-~/.local/share}/rekol/archive``.

    Args:
        config_archive_dir: The raw ``archive_dir`` config value, or ``None``.

    Returns:
        The absolute archive directory path (not created here; the archive
        module ``mkdir(parents=True)``s it on first write).
    """
    override = os.environ.get("REKOL_ARCHIVE_DIR")
    if override:
        return Path(os.path.expanduser(override))
    if config_archive_dir:
        return Path(os.path.expanduser(config_archive_dir))
    data_home = os.environ.get("XDG_DATA_HOME")
    data_root = Path(os.path.expanduser(data_home)) if data_home else Path.home() / ".local" / "share"
    return data_root / "rekol" / "archive"
```

Add the `archive_dir` property to `Config` (after the `sessions_db_path` property):

```python
    @property
    def archive_dir(self) -> Path:
        """Absolute path to the durable transcript archive (see resolve_archive_dir).

        Machine-level and NOT synced by default; holds verbatim transcripts.
        Resolved lazily so an env override is honored at call time.
        """
        return resolve_archive_dir(self.archive_dir_raw)
```

- [ ] **Step 1.2.4: Run to verify the resolver tests pass**

Run: `pytest tests/test_config_archive.py -v`
Expected: PASS (all tests in the file). Run `mypy src/rekol/config.py` — clean.

- [ ] **Step 1.2.5: Commit**

```bash
git add src/rekol/config.py tests/test_config_archive.py
git commit -m "feat: resolve durable archive dir (machine-level, XDG_DATA_HOME)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 1.3: `path_is_excluded` matcher + `.rekolignore` discovery

The shared exclude foundation. `fnmatch`-based glob matching over a path's string form, plus per-folder `.rekolignore` (gitignore-style) discovery. Full #5 reuses this verbatim later.

**Files:**
- Modify: `src/rekol/config.py`
- Test: `tests/test_config_archive.py`

- [ ] **Step 1.3.1: Write the failing test**

Append to `tests/test_config_archive.py`:

```python
from rekol.config import load_rekolignore_patterns, path_is_excluded


def test_path_is_excluded_matches_glob() -> None:
    patterns = ["*/secret-project/*", "*/clientwork/*"]
    assert path_is_excluded("/Users/x/code/secret-project/run.py", patterns) is True
    assert path_is_excluded("/Users/x/code/clientwork/notes.md", patterns) is True
    assert path_is_excluded("/Users/x/code/public/app.py", patterns) is False


def test_path_is_excluded_empty_patterns_excludes_nothing() -> None:
    assert path_is_excluded("/anything/at/all", []) is False


def test_path_is_excluded_matches_bare_segment() -> None:
    # A bare name like "secret-project" should match a path containing that
    # segment, so users don't have to write the full glob.
    assert path_is_excluded("/Users/x/secret-project/a.py", ["secret-project"]) is True


def test_load_rekolignore_reads_patterns_skipping_comments(tmp_path: Path) -> None:
    (tmp_path / ".rekolignore").write_text(
        "# a comment\n*/secret/*\n\n  */private/*  \n"
    )
    patterns = load_rekolignore_patterns(tmp_path)
    assert patterns == ["*/secret/*", "*/private/*"]


def test_load_rekolignore_absent_returns_empty(tmp_path: Path) -> None:
    assert load_rekolignore_patterns(tmp_path) == []
```

- [ ] **Step 1.3.2: Run to verify it fails**

Run: `pytest tests/test_config_archive.py -k "excluded or rekolignore" -v`
Expected: FAIL — `ImportError: cannot import name 'path_is_excluded'`.

- [ ] **Step 1.3.3: Implement the matcher and discovery**

In `src/rekol/config.py`, add `import fnmatch` to the imports and add these module-level functions (after `resolve_archive_dir`):

```python
def path_is_excluded(path: str, patterns: list[str]) -> bool:
    """True when ``path`` matches any exclude glob in ``patterns``.

    The shared exclude matcher — the reusable foundation for #5; the archive
    sink is its first consumer. Matching is over the path's STRING form with
    ``fnmatch`` (case-sensitive, ``*`` spans any characters incl. ``/`` since we
    match the whole path, not per-segment). A bare segment like
    ``secret-project`` is matched anywhere in the path by also testing
    ``*/<pattern>/*`` and ``*<pattern>*``, so users need not always write a full
    glob.

    Empty ``patterns`` excludes nothing (the default — nothing is excluded until
    the user opts in).
    """
    for pattern in patterns:
        if fnmatch.fnmatch(path, pattern):
            return True
        # A bare segment (no glob chars) should still match anywhere in the path
        # so a user can write `secret-project` instead of `*/secret-project/*`.
        if "*" not in pattern and "?" not in pattern:
            if fnmatch.fnmatch(path, f"*/{pattern}/*") or fnmatch.fnmatch(path, f"*{pattern}*"):
                return True
    return False


def load_rekolignore_patterns(root: Path) -> list[str]:
    """Read ``<root>/.rekolignore`` (gitignore-style) into a pattern list.

    Honored IN ADDITION to ``exclude_paths`` from config. Blank lines and lines
    starting with ``#`` are skipped; each remaining line is stripped of
    surrounding whitespace and used as an ``fnmatch`` glob. A missing file
    yields an empty list (no error — absence means "ignore nothing here").
    """
    ignore_file = root / ".rekolignore"
    if not ignore_file.is_file():
        return []
    patterns: list[str] = []
    # OSError (permission, race) must not crash a sync — an unreadable ignore
    # file degrades to "no extra patterns", logged by the caller, never fatal.
    try:
        text = ignore_file.read_text(encoding="utf-8")
    except OSError:
        return []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        patterns.append(line)
    return patterns
```

- [ ] **Step 1.3.4: Run to verify the matcher tests pass**

Run: `pytest tests/test_config_archive.py -v`
Expected: PASS (whole file). `ruff check src/rekol/config.py && mypy src/rekol/config.py` — clean.

- [ ] **Step 1.3.5: Commit**

```bash
git add src/rekol/config.py tests/test_config_archive.py
git commit -m "feat: add path_is_excluded matcher + .rekolignore discovery (#5 foundation)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Phase 2: The copy-if-changed primitive (`archive_file`)

The heart of durability: a whole-file copy with a manifest skip and a **divergence sidecar** for the compaction/rewrite case. DB-free.

### Task 2.1: Module scaffold + manifest load/save

**Files:**
- Create: `src/rekol/sessions/archive.py`
- Test: `tests/test_sessions_archive.py` (create)

- [ ] **Step 2.1.1: Write the failing test**

Create `tests/test_sessions_archive.py`:

```python
"""Unit tests for the DB-free transcript archive sink (sessions/archive.py)."""

from __future__ import annotations

import json
from pathlib import Path

from rekol.sessions.archive import load_manifest, save_manifest


def test_manifest_round_trips(tmp_path: Path) -> None:
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    manifest = {"proj/sess.jsonl": {"mtime_unix": 100, "size_bytes": 42}}
    save_manifest(archive_dir, manifest)
    assert (archive_dir / ".manifest.json").is_file()
    assert load_manifest(archive_dir) == manifest


def test_load_manifest_absent_returns_empty(tmp_path: Path) -> None:
    assert load_manifest(tmp_path) == {}


def test_load_manifest_corrupt_returns_empty(tmp_path: Path) -> None:
    """A corrupt manifest must NOT crash a sync — it degrades to 'archive
    everything fresh' (copy-if-changed is idempotent), never a traceback."""
    (tmp_path / ".manifest.json").write_text("{ not json")
    assert load_manifest(tmp_path) == {}
```

- [ ] **Step 2.1.2: Run to verify it fails**

Run: `pytest tests/test_sessions_archive.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'rekol.sessions.archive'`.

- [ ] **Step 2.1.3: Implement the scaffold + manifest helpers**

Create `src/rekol/sessions/archive.py`:

```python
"""Durable, rekol-owned transcript archive — the sink between Claude Code's
ephemeral ``~/.claude/projects/**/*.jsonl`` and the disposable ``sessions.db``.

DB-FREE BY DESIGN: this module is pure filesystem + a JSON manifest. It owns the
copy-if-changed primitive (with a divergence sidecar for the compaction/rewrite
case), the directory reconcile (copy new, skip unchanged, remove now-excluded),
the index→archive backfill, and the manual prune. Keeping it DB-free means the
archive can be rebuilt or inspected with no SQLite dependency, and a bug here can
never corrupt the index.

SOFT-FAIL DISCIPLINE: every public entry point that touches the filesystem is
written so an ``OSError`` (disk full, dir unwritable, permission) surfaces as a
caught, logged degradation — never an uncaught crash — because archiving must
never block indexing (see cli_session_index).
"""

from __future__ import annotations

import hashlib
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

# Manifest file name (hidden, inside the archive root). Maps a live-relative
# path -> {"mtime_unix": int, "size_bytes": int} recorded at last archive.
MANIFEST_FILENAME = ".manifest.json"
# One-time backfill guard marker; presence means "we already backfilled from the
# index on upgrade", so the auto-once backfill never re-runs.
BACKFILL_MARKER_FILENAME = ".backfilled-from-index"


def load_manifest(archive_dir: Path) -> dict[str, dict[str, int]]:
    """Read the archive manifest, or ``{}`` when absent/corrupt.

    A missing or corrupt manifest degrades to an empty mapping rather than
    raising: copy-if-changed is idempotent, so the worst case is re-archiving a
    file that was already current. We never let a bad manifest crash a sync.
    """
    manifest_path = archive_dir / MANIFEST_FILENAME
    if not manifest_path.is_file():
        return {}
    try:
        raw = manifest_path.read_text(encoding="utf-8")
        loaded = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        # Corrupt/unreadable manifest → treat as empty (re-archive everything).
        return {}
    if not isinstance(loaded, dict):
        return {}
    return loaded


def save_manifest(archive_dir: Path, manifest: dict[str, dict[str, int]]) -> None:
    """Atomically write the archive manifest.

    Writes to a temp file in the same dir then ``os.replace``s it, so a crash
    mid-write can never leave a half-written manifest (which would otherwise read
    back as corrupt and force a full re-archive).
    """
    archive_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = archive_dir / MANIFEST_FILENAME
    tmp_path = manifest_path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(manifest_path)
```

- [ ] **Step 2.1.4: Run to verify the manifest tests pass**

Run: `pytest tests/test_sessions_archive.py -v`
Expected: PASS (3 tests). `mypy src/rekol/sessions/archive.py` — clean.

- [ ] **Step 2.1.5: Commit**

```bash
git add src/rekol/sessions/archive.py tests/test_sessions_archive.py
git commit -m "feat: archive module scaffold + JSON manifest load/save

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2.2: `archive_file` — copy-if-changed matrix + divergence sidecar

The five cases from the design's "copy-if-changed primitive" section:
1. **No archived copy** → copy it.
2. **Unchanged** (live mtime+size == manifest) → skip.
3. **Live grew AND archived content is a true prefix of live** (normal append) → replace, update manifest.
4. **Live shorter OR diverged** (archived is NOT a prefix — the compaction/rewrite signature) → do NOT overwrite; write `<session-id>.<shorthash>.jsonl` sidecar; both get ingested; never silently lost.

**Files:**
- Modify: `src/rekol/sessions/archive.py`
- Test: `tests/test_sessions_archive.py`

- [ ] **Step 2.2.1: Write the failing tests (the full matrix)**

Append to `tests/test_sessions_archive.py`:

```python
from rekol.sessions.archive import ArchiveFileResult, archive_file


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_archive_file_copies_when_no_archived_copy(tmp_path: Path) -> None:
    live = tmp_path / "live" / "sess.jsonl"
    archived = tmp_path / "arch" / "sess.jsonl"
    _write(live, "line1\n")
    manifest: dict = {}
    result = archive_file(live, archived, manifest, manifest_key="sess.jsonl")
    assert result.action == "copied"
    assert archived.read_text() == "line1\n"
    assert manifest["sess.jsonl"]["size_bytes"] == len("line1\n")


def test_archive_file_skips_when_unchanged(tmp_path: Path) -> None:
    live = tmp_path / "live" / "sess.jsonl"
    archived = tmp_path / "arch" / "sess.jsonl"
    _write(live, "line1\n")
    manifest: dict = {}
    archive_file(live, archived, manifest, manifest_key="sess.jsonl")
    # Second call with no change to live → skip (manifest mtime+size match).
    result = archive_file(live, archived, manifest, manifest_key="sess.jsonl")
    assert result.action == "skipped_unchanged"


def test_archive_file_replaces_on_append_prefix(tmp_path: Path) -> None:
    live = tmp_path / "live" / "sess.jsonl"
    archived = tmp_path / "arch" / "sess.jsonl"
    _write(live, "line1\n")
    manifest: dict = {}
    archive_file(live, archived, manifest, manifest_key="sess.jsonl")
    # Live grows; archived content is a true prefix of the new live → replace.
    _write(live, "line1\nline2\n")
    result = archive_file(live, archived, manifest, manifest_key="sess.jsonl")
    assert result.action == "replaced_append"
    assert archived.read_text() == "line1\nline2\n"


def test_archive_file_sidecars_on_divergence(tmp_path: Path) -> None:
    """Compaction/rewrite signature: live is shorter OR not a prefix of the
    archived copy. We must keep the existing archive AND write a divergence
    sidecar <stem>.<shorthash>.jsonl — never overwrite, never silently lose."""
    live = tmp_path / "live" / "sess.jsonl"
    archived = tmp_path / "arch" / "sess.jsonl"
    _write(live, "line1\nline2\nline3\n")
    manifest: dict = {}
    archive_file(live, archived, manifest, manifest_key="sess.jsonl")
    # Live is REWRITTEN to a shorter, non-prefix content (compaction).
    _write(live, "rewritten\n")
    result = archive_file(live, archived, manifest, manifest_key="sess.jsonl")
    assert result.action == "diverged_sidecar"
    # Original archive is untouched.
    assert archived.read_text() == "line1\nline2\nline3\n"
    # A sidecar exists alongside it with the new content.
    sidecars = list(archived.parent.glob("sess.*.jsonl"))
    assert len(sidecars) == 1
    assert sidecars[0].read_text() == "rewritten\n"


def test_archive_file_sidecar_is_idempotent(tmp_path: Path) -> None:
    """Re-running a diverged sync must not pile up duplicate sidecars: the
    shorthash is content-derived, so the same divergence yields the same path."""
    live = tmp_path / "live" / "sess.jsonl"
    archived = tmp_path / "arch" / "sess.jsonl"
    _write(live, "original\n")
    manifest: dict = {}
    archive_file(live, archived, manifest, manifest_key="sess.jsonl")
    _write(live, "rewritten\n")
    archive_file(live, archived, manifest, manifest_key="sess.jsonl")
    archive_file(live, archived, manifest, manifest_key="sess.jsonl")
    sidecars = list(archived.parent.glob("sess.*.jsonl"))
    assert len(sidecars) == 1  # same content → same shorthash → one sidecar
```

- [ ] **Step 2.2.2: Run to verify it fails**

Run: `pytest tests/test_sessions_archive.py -k archive_file -v`
Expected: FAIL — `ImportError: cannot import name 'archive_file'`.

- [ ] **Step 2.2.3: Implement `archive_file` + `ArchiveFileResult`**

Append to `src/rekol/sessions/archive.py`:

```python
@dataclass
class ArchiveFileResult:
    """Outcome of archiving one live file.

    ``action`` is one of: ``copied`` (no prior archive), ``skipped_unchanged``
    (manifest mtime+size match), ``replaced_append`` (live grew, archived was a
    true prefix), ``diverged_sidecar`` (live shorter/diverged — kept both copies
    via a sidecar). ``sidecar_path`` is set only for ``diverged_sidecar``.
    """

    action: str
    sidecar_path: Path | None = None


def _archived_is_prefix_of_live(archived_path: Path, live_path: Path) -> bool:
    """True when the archived file's bytes are a true prefix of the live file's.

    This is the "normal append" signature: Claude Code appended rows to the
    session and the archived copy is the earlier, shorter version. We compare
    bytes (not text) so an encoding quirk can never misclassify. Read errors
    return False (treat as divergence → keep both copies; the safe direction).
    """
    try:
        archived_bytes = archived_path.read_bytes()
        live_bytes = live_path.read_bytes()
    except OSError:
        return False
    if len(archived_bytes) > len(live_bytes):
        return False
    return live_bytes[: len(archived_bytes)] == archived_bytes


def _divergence_sidecar_path(archived_path: Path, live_path: Path) -> Path:
    """Path for the divergence sidecar: ``<stem>.<shorthash>.jsonl``.

    The shorthash is the first 8 hex chars of the SHA-256 of the live content,
    so the SAME divergent content always maps to the SAME sidecar path —
    re-running a sync is idempotent and never piles up duplicates. (DB-level
    uuid dedupe folds the two copies at ingest, so a duplicate sidecar would be
    harmless but messy.)
    """
    digest = hashlib.sha256(live_path.read_bytes()).hexdigest()[:8]
    # archived_path is e.g. <dir>/<session-id>.jsonl; insert the shorthash before
    # the .jsonl suffix → <dir>/<session-id>.<shorthash>.jsonl.
    return archived_path.with_suffix("").with_suffix(f".{digest}.jsonl")


def archive_file(
    live_path: Path,
    archived_path: Path,
    manifest: dict[str, dict[str, int]],
    *,
    manifest_key: str,
) -> ArchiveFileResult:
    """Copy-if-changed primitive: reconcile one live file into the archive.

    Mutates ``manifest[manifest_key]`` in place on copy/replace (caller persists
    it once per directory). The five cases (see module docstring + design):

    * no archived copy             → copy, record manifest
    * unchanged (mtime+size match)  → skip
    * live grew, archived is prefix → replace (normal append), record manifest
    * live shorter / not a prefix   → keep archive, write divergence sidecar

    Raises ``OSError`` to the caller (``archive_directory``), which counts it and
    keeps going — one unreadable file must not abort the whole sync.
    """
    live_stat = live_path.stat()
    live_mtime = int(live_stat.st_mtime)
    live_size = int(live_stat.st_size)

    if not archived_path.exists():
        archived_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(live_path, archived_path)
        manifest[manifest_key] = {"mtime_unix": live_mtime, "size_bytes": live_size}
        return ArchiveFileResult(action="copied")

    recorded = manifest.get(manifest_key)
    if recorded is not None and recorded.get("mtime_unix") == live_mtime and recorded.get(
        "size_bytes"
    ) == live_size:
        # Steady-state cheap path: nothing changed since we last archived it.
        return ArchiveFileResult(action="skipped_unchanged")

    if _archived_is_prefix_of_live(archived_path, live_path):
        # Normal append: the archived copy is an earlier prefix; replace wholesale.
        shutil.copy2(live_path, archived_path)
        manifest[manifest_key] = {"mtime_unix": live_mtime, "size_bytes": live_size}
        return ArchiveFileResult(action="replaced_append")

    # Divergence (compaction/rewrite): live is shorter or not a prefix. NEVER
    # overwrite — keep the existing archive and write the new version beside it.
    sidecar_path = _divergence_sidecar_path(archived_path, live_path)
    if not sidecar_path.exists():
        shutil.copy2(live_path, sidecar_path)
    # Intentionally do NOT update the manifest key here: the canonical archived
    # file is unchanged, and the sidecar is found by ingest's directory glob.
    return ArchiveFileResult(action="diverged_sidecar", sidecar_path=sidecar_path)
```

- [ ] **Step 2.2.4: Run to verify the matrix passes**

Run: `pytest tests/test_sessions_archive.py -v`
Expected: PASS (all archive_file cases + manifest). `ruff check && mypy src/rekol/sessions/archive.py` — clean.

- [ ] **Step 2.2.5: Commit**

```bash
git add src/rekol/sessions/archive.py tests/test_sessions_archive.py
git commit -m "feat: copy-if-changed primitive with divergence sidecar

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Phase 3: `archive_directory` reconcile (+ exclude slice forward & retroactive)

Walk the live root, copy new/changed non-excluded files, skip unchanged, and **remove already-archived files that now match an exclude** (the safe retroactive flat-file delete).

### Task 3.1: `archive_directory` happy path (copy new, skip unchanged)

**Files:**
- Modify: `src/rekol/sessions/archive.py`
- Test: `tests/test_sessions_archive.py`

- [ ] **Step 3.1.1: Write the failing test**

Append to `tests/test_sessions_archive.py`:

```python
from rekol.sessions.archive import ArchiveStats, archive_directory


def test_archive_directory_copies_all_then_skips_on_rerun(tmp_path: Path) -> None:
    live_root = tmp_path / "projects"
    archive_dir = tmp_path / "archive"
    _write(live_root / "projA" / "s1.jsonl", "a\n")
    _write(live_root / "projB" / "s2.jsonl", "b\n")

    first = archive_directory(live_root, archive_dir, exclude_patterns=[])
    assert first.files_copied == 2
    assert first.files_skipped_unchanged == 0
    # Mirror layout is preserved under the archive root.
    assert (archive_dir / "projA" / "s1.jsonl").read_text() == "a\n"
    assert (archive_dir / "projB" / "s2.jsonl").read_text() == "b\n"

    # Re-run with nothing changed → all skipped via the manifest.
    second = archive_directory(live_root, archive_dir, exclude_patterns=[])
    assert second.files_copied == 0
    assert second.files_skipped_unchanged == 2


def test_archive_directory_counts_os_errors_without_aborting(tmp_path: Path, monkeypatch) -> None:
    """One unreadable file must not abort the whole sync: it is counted in
    files_errored and the rest still archive."""
    live_root = tmp_path / "projects"
    archive_dir = tmp_path / "archive"
    _write(live_root / "good.jsonl", "ok\n")
    _write(live_root / "bad.jsonl", "boom\n")

    import rekol.sessions.archive as archive_mod

    real_archive_file = archive_mod.archive_file

    def flaky(live_path, archived_path, manifest, *, manifest_key):
        if live_path.name == "bad.jsonl":
            raise OSError("simulated unreadable file")
        return real_archive_file(live_path, archived_path, manifest, manifest_key=manifest_key)

    monkeypatch.setattr(archive_mod, "archive_file", flaky)
    stats = archive_directory(live_root, archive_dir, exclude_patterns=[])
    assert stats.files_copied == 1
    assert stats.files_errored == 1
    assert (archive_dir / "good.jsonl").read_text() == "ok\n"
```

- [ ] **Step 3.1.2: Run to verify it fails**

Run: `pytest tests/test_sessions_archive.py -k archive_directory -v`
Expected: FAIL — `ImportError: cannot import name 'archive_directory'`.

- [ ] **Step 3.1.3: Implement `archive_directory` + `ArchiveStats`**

Append to `src/rekol/sessions/archive.py`:

```python
@dataclass
class ArchiveStats:
    """Tally of an ``archive_directory`` reconcile run."""

    files_seen: int = 0
    files_copied: int = 0
    files_replaced: int = 0
    files_skipped_unchanged: int = 0
    files_skipped_excluded: int = 0  # live file matched an exclude → never archived
    files_diverged_sidecar: int = 0  # compaction/rewrite → kept both copies
    files_removed_excluded: int = 0  # already-archived file now matches an exclude → rm'd
    files_errored: int = 0  # OSError on one file; counted, sync continues


def _read_cwd_from_jsonl(jsonl_path: Path) -> str | None:
    """Read the session's REAL cwd from the first message row that carries one.

    The exclude matcher must run against the project path the user actually worked
    in (e.g. ``/Users/x/secret``), NOT Claude Code's URL-encoded folder slug
    (``-Users-x-secret``) — otherwise a natural pattern like ``*/secret/*`` would
    silently never match the on-disk path. Every Claude Code transcript row carries
    a ``cwd`` field, so we scan from the top and return the first non-empty one.

    Reads only the first few lines (the cwd is on row 1 in practice; we cap the scan
    so a huge transcript is not slurped just to find it). Returns ``None`` when the
    file is unreadable or no row has a cwd — the caller then does NOT exclude it
    (fail-open: a missing cwd must not silently drop a session from the archive).
    """
    try:
        with jsonl_path.open(encoding="utf-8") as handle:
            for line_number, raw_line in enumerate(handle):
                if line_number >= 50:  # cwd is on the first row in practice
                    break
                stripped = raw_line.strip()
                if not stripped:
                    continue
                try:
                    row = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                cwd = row.get("cwd")
                if isinstance(cwd, str) and cwd:
                    return cwd
    except OSError:
        return None
    return None


def archive_directory(
    live_root: Path,
    archive_dir: Path,
    exclude_patterns: list[str],
) -> ArchiveStats:
    """Reconcile the archive to ``(live ∩ not-excluded)``.

    Walks every ``*.jsonl`` under ``live_root`` (typically
    ``~/.claude/projects``), and for each non-excluded file runs the
    copy-if-changed primitive (:func:`archive_file`). Then it sweeps the archive
    for files that now match an exclude and removes them (the safe retroactive
    flat-file delete — see exclude slice).

    EXCLUDE MATCHES THE REAL cwd, NOT the slug. The forward skip reads the first
    message row's ``cwd`` from each ``.jsonl`` (the project path the user actually
    worked in, e.g. ``/Users/x/secret``) and matches ``exclude_patterns`` against
    THAT — never against Claude Code's URL-encoded folder name
    (``-Users-x-secret``), which would make a natural pattern like ``*/secret/*``
    silently never match. This is symmetric with the retroactive removal and the
    backfill, which also match the real cwd. ``.rekolignore`` patterns are merged
    in by the caller (``cli_archive`` / ``cli_session_index``) before calling this,
    so this function takes a single already-combined pattern list.

    SOFT-FAIL: an ``OSError`` on one file is caught, counted in
    ``files_errored``, and the walk continues — one bad file never aborts the
    sync. The manifest is persisted once at the end (after all copies).

    Returns the :class:`ArchiveStats` tally.
    """
    stats = ArchiveStats()
    manifest = load_manifest(archive_dir)
    live_root = Path(live_root)

    for live_path in sorted(live_root.glob("**/*.jsonl")):
        stats.files_seen += 1
        # manifest_key / archive layout mirror the live tree relative to root.
        relative = live_path.relative_to(live_root)
        manifest_key = str(relative)
        # Exclude on the session's REAL cwd (read from the file), NOT the slug-
        # encoded path on disk — so a pattern like `*/secret/*` matches the project
        # the user worked in. A file with no readable cwd is never excluded here
        # (it falls through to archiving; an unreadable file is caught below). Only
        # read the cwd when there ARE excludes — otherwise every sync would pay a
        # per-file read for nothing (the common no-exclude case stays cheap).
        if exclude_patterns:
            session_cwd = _read_cwd_from_jsonl(live_path)
            if session_cwd and path_is_excluded(session_cwd, exclude_patterns):
                stats.files_skipped_excluded += 1
                continue
        archived_path = archive_dir / relative
        try:
            result = archive_file(live_path, archived_path, manifest, manifest_key=manifest_key)
        except OSError:
            # One unreadable/unwritable file must not abort the run; the next
            # successful sync catches it (copy-if-changed is idempotent).
            stats.files_errored += 1
            continue
        if result.action == "copied":
            stats.files_copied += 1
        elif result.action == "replaced_append":
            stats.files_replaced += 1
        elif result.action == "skipped_unchanged":
            stats.files_skipped_unchanged += 1
        elif result.action == "diverged_sidecar":
            stats.files_diverged_sidecar += 1

    _remove_now_excluded_from_archive(archive_dir, exclude_patterns, manifest, stats)
    save_manifest(archive_dir, manifest)
    return stats
```

`path_is_excluded` is imported from config — add to the top-of-file imports:

```python
from rekol.config import path_is_excluded
```

(The `_remove_now_excluded_from_archive` helper is added in Task 3.2; for this step, add a temporary no-op stub so the happy-path tests pass:)

```python
def _remove_now_excluded_from_archive(
    archive_dir: Path,
    exclude_patterns: list[str],
    manifest: dict[str, dict[str, int]],
    stats: ArchiveStats,
) -> None:
    """Placeholder — real retroactive-removal logic lands in Task 3.2."""
    return
```

- [ ] **Step 3.1.4: Run to verify the happy-path tests pass**

Run: `pytest tests/test_sessions_archive.py -k archive_directory -v`
Expected: PASS (copy-then-skip + os-error-continue). `mypy src/rekol/sessions/archive.py` — clean.

- [ ] **Step 3.1.5: Commit**

```bash
git add src/rekol/sessions/archive.py tests/test_sessions_archive.py
git commit -m "feat: archive_directory reconcile (copy new, skip unchanged, soft-fail)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3.2: Exclude — forward skip + retroactive removal

**Files:**
- Modify: `src/rekol/sessions/archive.py`
- Test: `tests/test_sessions_archive.py`

- [ ] **Step 3.2.1: Write the failing tests**

Append to `tests/test_sessions_archive.py`:

```python
def _write_jsonl_with_cwd(p: Path, cwd: str) -> None:
    """Write a one-row transcript whose `cwd` is the REAL project path.

    The exclude matcher runs against this cwd (e.g. `/Users/x/secret-project`),
    NOT the on-disk folder name — so these fixtures deliberately put the matchable
    path in `cwd`, while the slug folder on disk is a distinct, non-matching name,
    proving the match is cwd-driven (the design decision: exclude the real cwd).
    """
    p.parent.mkdir(parents=True, exist_ok=True)
    row = {"type": "user", "uuid": "u1", "sessionId": p.stem, "cwd": cwd,
           "message": {"role": "user", "content": "hi"}}
    p.write_text(json.dumps(row) + "\n", encoding="utf-8")


def test_archive_directory_forward_skips_excluded(tmp_path: Path) -> None:
    live_root = tmp_path / "projects"
    archive_dir = tmp_path / "archive"
    # On-disk slug folders are URL-encoded and do NOT contain a `/secret-project/`
    # segment; the matchable path lives in each row's cwd. The exclude must match
    # the cwd, not the slug.
    _write_jsonl_with_cwd(
        live_root / "-Users-x-secret-project" / "s1.jsonl", "/Users/x/secret-project"
    )
    _write_jsonl_with_cwd(live_root / "-Users-x-public" / "s2.jsonl", "/Users/x/public")

    # Pattern matches the cwd VALUE (which has no trailing slash), so `*/secret-project*`
    # — not `*/secret-project/*`, which would require a path segment AFTER the dir.
    stats = archive_directory(
        live_root, archive_dir, exclude_patterns=["*/secret-project*"]
    )
    assert stats.files_skipped_excluded == 1
    assert stats.files_copied == 1
    # The excluded project is NEVER archived (matched on its real cwd).
    assert not (archive_dir / "-Users-x-secret-project").exists()
    assert (archive_dir / "-Users-x-public" / "s2.jsonl").exists()


def test_archive_directory_retroactively_removes_now_excluded(tmp_path: Path) -> None:
    """An already-archived file that becomes excluded must be removed on the
    next sync — the safe retroactive flat-file delete in rekol's own folder. The
    match is on the file's real cwd, symmetric with the forward skip."""
    live_root = tmp_path / "projects"
    archive_dir = tmp_path / "archive"
    _write_jsonl_with_cwd(
        live_root / "-Users-x-clientwork" / "s1.jsonl", "/Users/x/clientwork"
    )
    _write_jsonl_with_cwd(live_root / "-Users-x-public" / "s2.jsonl", "/Users/x/public")

    # First sync with NO excludes archives everything.
    archive_directory(live_root, archive_dir, exclude_patterns=[])
    assert (archive_dir / "-Users-x-clientwork" / "s1.jsonl").exists()

    # User adds an exclude; next sync removes the now-excluded archived copy
    # (matched on the archived file's real cwd, not its slug folder name).
    stats = archive_directory(
        live_root, archive_dir, exclude_patterns=["*/clientwork*"]
    )
    assert stats.files_removed_excluded == 1
    assert not (archive_dir / "-Users-x-clientwork" / "s1.jsonl").exists()
    assert (archive_dir / "-Users-x-public" / "s2.jsonl").exists()
    # The manifest entry for the removed file is also dropped.
    manifest = load_manifest(archive_dir)
    assert "-Users-x-clientwork/s1.jsonl" not in manifest


def test_archive_directory_matches_cwd_not_slug(tmp_path: Path) -> None:
    """The design decision, asserted directly: a pattern that matches the cwd but
    NOT the URL-encoded slug excludes; a pattern that matches only the slug does
    NOT. The slug `-Users-x-secret` never contains a `/secret/` segment, so a
    natural `*/secret/*` pattern must reach the cwd to work at all."""
    live_root = tmp_path / "projects"
    archive_dir = tmp_path / "archive"
    _write_jsonl_with_cwd(live_root / "-Users-x-secret" / "s.jsonl", "/Users/x/secret")

    # A slug-shaped pattern (matches the on-disk folder, not the cwd) must NOT
    # exclude — we deliberately do not match the slug.
    kept = archive_directory(live_root, archive_dir, exclude_patterns=["*-Users-x-secret*"])
    assert kept.files_skipped_excluded == 0
    assert kept.files_copied == 1

    # A natural cwd-shaped pattern DOES exclude (on the retroactive sweep here).
    excluded = archive_directory(live_root, archive_dir, exclude_patterns=["*/secret*"])
    assert excluded.files_removed_excluded == 1
```

- [ ] **Step 3.2.2: Run to verify it fails**

Run: `pytest tests/test_sessions_archive.py -k "excluded" -v`
Expected: the forward test passes already (Task 3.1), the retroactive test FAILS (`files_removed_excluded == 0`, file still present).

- [ ] **Step 3.2.3: Implement the real retroactive-removal helper**

Replace the no-op `_remove_now_excluded_from_archive` stub in `src/rekol/sessions/archive.py` with:

```python
def _remove_now_excluded_from_archive(
    archive_dir: Path,
    exclude_patterns: list[str],
    manifest: dict[str, dict[str, int]],
    stats: ArchiveStats,
) -> None:
    """Remove already-archived files (and sidecars) that now match an exclude.

    The retroactive half of the exclude slice. Because the archive is flat files
    in rekol's OWN folder, a retroactive delete here is safe (unlike the index,
    where a destructive purge is deferred). We glob the archive, test each file's
    REAL cwd against the excludes, ``unlink`` matches, and drop the manifest entry
    so a later un-exclude re-copies cleanly.

    EXCLUDE MATCHES THE REAL cwd, NOT the slug — symmetric with the forward skip in
    :func:`archive_directory`. We read each archived file's ``cwd`` (the project
    path the user worked in) and match against THAT, never the URL-encoded folder
    name on disk, so a pattern like ``*/secret/*`` removes the right sessions. A
    file whose cwd cannot be read is left in place (fail-open: never delete on a
    read error).

    SOFT-FAIL: an ``OSError`` removing one file is swallowed (counted nowhere —
    the next sync retries); we never abort the sweep.
    """
    if not exclude_patterns:
        return
    if not archive_dir.is_dir():
        return
    for archived_path in sorted(archive_dir.glob("**/*.jsonl")):
        archived_cwd = _read_cwd_from_jsonl(archived_path)
        if not archived_cwd or not path_is_excluded(archived_cwd, exclude_patterns):
            continue
        try:
            archived_path.unlink()
        except OSError:
            # Best-effort: a file we cannot remove now is retried next sync.
            continue
        stats.files_removed_excluded += 1
        # Drop the manifest entry (keyed by the live-relative path) so a later
        # un-exclude triggers a fresh copy rather than a false "unchanged" skip.
        manifest_key = str(archived_path.relative_to(archive_dir))
        manifest.pop(manifest_key, None)
```

- [ ] **Step 3.2.4: Run to verify both exclude tests pass**

Run: `pytest tests/test_sessions_archive.py -v`
Expected: PASS (whole file). `ruff check && mypy src/rekol/sessions/archive.py` — clean.

- [ ] **Step 3.2.5: Commit**

```bash
git add src/rekol/sessions/archive.py tests/test_sessions_archive.py
git commit -m "feat: exclude slice — forward skip + retroactive archive removal

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Phase 4: `backfill_from_index` (reconstruct missing `.jsonl` from `sessions.db`)

On upgrade, sessions already in `sessions.db` but missing from the archive are reconstructed as minimal (text-only/lossy) `.jsonl` so they are not lost. Exclude-aware. Guarded by the one-time marker.

### Task 4.1: `backfill_from_index` round-trips through the store

**Files:**
- Modify: `src/rekol/sessions/archive.py`
- Test: `tests/test_sessions_archive.py`

- [ ] **Step 4.1.1: Write the failing test**

Append to `tests/test_sessions_archive.py`:

```python
from rekol.sessions.archive import BackfillStats, backfill_from_index
from rekol.sessions.ingest import iter_messages_in_file
from rekol.sessions.store import SessionStore


def _seed_store_with_session(db_path: Path, *, cwd: str = "/Users/x/projA") -> SessionStore:
    store = SessionStore(db_path=db_path, dim=4, use_sqlite_vec=False)
    store.init_schema()
    store.insert_message(
        dict(
            session_id="sess-1",
            message_uuid="u1",
            parent_uuid=None,
            role="user",
            content="how do I configure the proxy base_url",
            cwd=cwd,
            timestamp_iso="2026-05-01T10:00:00Z",
            timestamp_unix=1777622400,
            jsonl_path="/gone/projA/sess-1.jsonl",
            line_number=1,
        )
    )
    return store


def test_backfill_reconstructs_missing_session(tmp_path: Path) -> None:
    archive_dir = tmp_path / "archive"
    store = _seed_store_with_session(tmp_path / "sessions.db")
    try:
        stats = backfill_from_index(store, archive_dir, exclude_patterns=[])
    finally:
        store.close()
    assert stats.sessions_reconstructed == 1
    # The reconstructed file is a real .jsonl that re-ingests cleanly.
    reconstructed = list(archive_dir.glob("**/*.jsonl"))
    assert len(reconstructed) == 1
    msgs = list(iter_messages_in_file(reconstructed[0]))
    assert len(msgs) == 1
    assert msgs[0]["session_id"] == "sess-1"
    assert msgs[0]["content"].startswith("how do I configure")


def test_backfill_is_exclude_aware(tmp_path: Path) -> None:
    archive_dir = tmp_path / "archive"
    store = _seed_store_with_session(tmp_path / "sessions.db", cwd="/Users/x/secret-project")
    try:
        stats = backfill_from_index(
            store, archive_dir, exclude_patterns=["*/secret-project*"]
        )
    finally:
        store.close()
    assert stats.sessions_skipped_excluded == 1
    assert stats.sessions_reconstructed == 0
    assert list(archive_dir.glob("**/*.jsonl")) == []


def test_backfill_skips_sessions_already_archived(tmp_path: Path) -> None:
    archive_dir = tmp_path / "archive"
    # Pre-create an archive file for the session so backfill leaves it alone.
    existing = archive_dir / "projA" / "sess-1.jsonl"
    existing.parent.mkdir(parents=True)
    existing.write_text('{"type":"user"}\n')
    store = _seed_store_with_session(tmp_path / "sessions.db")
    try:
        stats = backfill_from_index(store, archive_dir, exclude_patterns=[])
    finally:
        store.close()
    assert stats.sessions_skipped_present == 1
    assert stats.sessions_reconstructed == 0
```

- [ ] **Step 4.1.2: Run to verify it fails**

Run: `pytest tests/test_sessions_archive.py -k backfill -v`
Expected: FAIL — `ImportError: cannot import name 'backfill_from_index'`.

- [ ] **Step 4.1.3: Add a read-only session enumerator to `SessionStore`**

Backfill needs to read sessions out of the DB. Add a minimal accessor to `src/rekol/sessions/store.py` (TDD: first add its own test).

Append to `tests/test_sessions_store.py`:

```python
def test_iter_sessions_for_backfill_groups_by_session(tmp_path):
    store = SessionStore(db_path=tmp_path / "s.db", dim=4, use_sqlite_vec=False)
    store.init_schema()
    for uuid, line in (("u1", 1), ("u2", 2)):
        store.insert_message(_make_msg(uuid=uuid, session="sess-1", line=line))
    sessions = list(store.iter_sessions_for_backfill())
    assert len(sessions) == 1
    session_id, jsonl_path, messages = sessions[0]
    assert session_id == "sess-1"
    assert len(messages) == 2
    # Messages carry the fields needed to reconstruct a minimal .jsonl row.
    assert messages[0]["message_uuid"] == "u1"
    assert messages[0]["role"] == "user"
    store.close()
```

Run: `pytest tests/test_sessions_store.py -k iter_sessions_for_backfill -v` → FAIL (`AttributeError`).

Add to `SessionStore` in `src/rekol/sessions/store.py`:

```python
    def iter_sessions_for_backfill(
        self,
    ) -> Iterator[tuple[str, str | None, list[dict]]]:
        """Yield ``(session_id, jsonl_path, messages)`` for archive backfill.

        Reads the messages table grouped by session, in ``(session_id,
        line_number)`` order, so the archive backfill can reconstruct a minimal,
        text-only ``.jsonl`` per session that was indexed before the archive
        existed. ``jsonl_path`` is the original live path recorded at ingest
        (used only to derive a project-slug folder); it may no longer exist on
        disk — that is the whole point of backfilling from the index.

        This is a LOSSY reconstruction: the DB never stored tool_use/thinking
        rows, so the rebuilt file contains only the indexed user/assistant text
        turns. A lossy copy beats a lost session.
        """
        rows = self.conn.execute(
            "SELECT session_id, message_uuid, parent_uuid, role, content, cwd, "
            "       timestamp_iso, timestamp_unix, jsonl_path, line_number "
            "FROM messages ORDER BY session_id, line_number"
        ).fetchall()
        current_session: str | None = None
        current_path: str | None = None
        bucket: list[dict] = []
        for row in rows:
            session_id = row["session_id"]
            if session_id != current_session:
                if current_session is not None:
                    yield current_session, current_path, bucket
                current_session = session_id
                current_path = row["jsonl_path"]
                bucket = []
            bucket.append(dict(row))
        if current_session is not None:
            yield current_session, current_path, bucket
```

Add `Iterator` to the `store.py` typing imports (`from collections.abc import Iterator`).

Run: `pytest tests/test_sessions_store.py -k iter_sessions_for_backfill -v` → PASS.

- [ ] **Step 4.1.4: Implement `backfill_from_index` + `BackfillStats`**

Append to `src/rekol/sessions/archive.py` (and add `from typing import TYPE_CHECKING`; under it, `if TYPE_CHECKING: from rekol.sessions.store import SessionStore` — keep the module import-light and avoid a hard DB import at module load):

```python
@dataclass
class BackfillStats:
    """Tally of a ``backfill_from_index`` run."""

    sessions_seen: int = 0
    sessions_reconstructed: int = 0
    sessions_skipped_present: int = 0  # already had an archive file
    sessions_skipped_excluded: int = 0
    sessions_errored: int = 0


def _project_slug_from_jsonl_path(jsonl_path: str | None) -> str:
    """Derive the archive sub-folder for a reconstructed session.

    Mirrors how the live tree is laid out under ``~/.claude/projects/<slug>/``.
    Uses the parent directory name of the original jsonl path; falls back to
    ``_backfilled`` when the path is missing or has no usable parent, so a
    reconstructed file always has a home.
    """
    # WHY parent-dir-name: this assumes Claude Code's on-disk layout, where each
    # transcript lives at ``<projects>/<url-encoded-slug>/<session-id>.jsonl`` — so
    # the immediate parent directory IS the project slug. We deliberately reuse
    # that slug (not the real cwd) for the archive folder so the backfilled file
    # mirrors the live tree exactly; if Claude Code ever changes that layout, this
    # is the single place to revisit. (Excludes still match the real cwd, not this
    # slug — see backfill_from_index.)
    if not jsonl_path:
        return "_backfilled"
    parent_name = Path(jsonl_path).parent.name
    return parent_name or "_backfilled"


def _reconstruct_jsonl_line(message: dict) -> str:
    """Render one indexed message back into a minimal Claude-Code-shaped JSONL row.

    Only the fields ``iter_messages_in_file`` reads are emitted (type, uuid,
    sessionId, timestamp, cwd, message.role/content), so the reconstructed file
    round-trips through the existing ingest path with no special-casing. This is
    deliberately lossy — tool_use/thinking rows were never indexed — which is
    acceptable: a text-only copy beats a lost session.
    """
    row = {
        "type": message["role"] if message["role"] in ("user", "assistant") else "user",
        "uuid": message["message_uuid"],
        "parentUuid": message.get("parent_uuid"),
        "sessionId": message["session_id"],
        "timestamp": message["timestamp_iso"],
        "cwd": message.get("cwd"),
        "message": {"role": message["role"], "content": message["content"]},
    }
    return json.dumps(row, ensure_ascii=False)


def backfill_from_index(
    store: SessionStore,
    archive_dir: Path,
    exclude_patterns: list[str],
) -> BackfillStats:
    """Reconstruct archive ``.jsonl`` files for sessions present in the index but
    absent from the archive (exclude-aware, text-only/lossy).

    Used both for the one-time auto-backfill on upgrade and the explicit
    ``rekol archive --from-index``. For each session in ``sessions.db``: skip if
    its REAL cwd matches an exclude; skip if an archive file already exists;
    otherwise write a minimal reconstructed ``.jsonl`` under
    ``<archive>/<project-slug>/<session-id>.jsonl`` and record it in the manifest.

    EXCLUDE MATCHES THE REAL cwd, NOT the slug. ``exclude_paths`` globs are matched
    against the session's actual working directory (e.g. ``/Users/x/secret``) read
    from the indexed rows — NOT against Claude Code's URL-encoded project slug
    (``-Users-x-secret``). Matching the slug would silently break a natural pattern
    like ``*/secret/*`` (it never contains a ``/secret/`` segment). This makes the
    retroactive (backfill) and forward (archive-sync) checks symmetric — both match
    the real cwd. See ``archive_directory`` for the forward side.

    SOFT-FAIL: an ``OSError`` writing one session is counted in
    ``sessions_errored`` and the loop continues.

    Returns the :class:`BackfillStats` tally. Does NOT write the marker — the
    caller (``cli_archive`` / ``cli_session_index``) owns the one-time guard.
    """
    stats = BackfillStats()
    manifest = load_manifest(archive_dir)
    for session_id, jsonl_path, messages in store.iter_sessions_for_backfill():
        stats.sessions_seen += 1
        # Match on the session's REAL cwd (the project path the user worked in),
        # falling back to jsonl_path only when no row carried a cwd. We never match
        # the URL-encoded slug — a pattern like `*/secret/*` must see `/secret/`.
        cwd = next((m.get("cwd") for m in messages if m.get("cwd")), jsonl_path)
        if cwd and path_is_excluded(str(cwd), exclude_patterns):
            stats.sessions_skipped_excluded += 1
            continue
        slug = _project_slug_from_jsonl_path(jsonl_path)
        relative = Path(slug) / f"{session_id}.jsonl"
        archived_path = archive_dir / relative
        if archived_path.exists():
            stats.sessions_skipped_present += 1
            continue
        try:
            archived_path.parent.mkdir(parents=True, exist_ok=True)
            archived_path.write_text(
                "\n".join(_reconstruct_jsonl_line(m) for m in messages) + "\n",
                encoding="utf-8",
            )
        except OSError:
            stats.sessions_errored += 1
            continue
        archived_stat = archived_path.stat()
        manifest[str(relative)] = {
            "mtime_unix": int(archived_stat.st_mtime),
            "size_bytes": int(archived_stat.st_size),
        }
        stats.sessions_reconstructed += 1
    save_manifest(archive_dir, manifest)
    return stats
```

- [ ] **Step 4.1.5: Run to verify the backfill tests pass**

Run: `pytest tests/test_sessions_archive.py tests/test_sessions_store.py -v`
Expected: PASS. `ruff check && mypy src/rekol/sessions/archive.py src/rekol/sessions/store.py` — clean.

- [ ] **Step 4.1.6: Commit**

```bash
git add src/rekol/sessions/archive.py src/rekol/sessions/store.py \
        tests/test_sessions_archive.py tests/test_sessions_store.py
git commit -m "feat: reconstruct missing sessions into the archive from the index

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4.2: `prune` (manual clear/trim)

**Files:**
- Modify: `src/rekol/sessions/archive.py`
- Test: `tests/test_sessions_archive.py`

- [ ] **Step 4.2.1: Write the failing test**

Append to `tests/test_sessions_archive.py`:

```python
from rekol.sessions.archive import PruneStats, prune


def test_prune_clear_removes_all_archive_files(tmp_path: Path) -> None:
    archive_dir = tmp_path / "archive"
    _write(archive_dir / "projA" / "s1.jsonl", "a\n")
    _write(archive_dir / "projB" / "s2.jsonl", "b\n")
    save_manifest(archive_dir, {"projA/s1.jsonl": {"mtime_unix": 1, "size_bytes": 2}})

    stats = prune(archive_dir, clear=True)
    assert stats.files_removed == 2
    assert list(archive_dir.glob("**/*.jsonl")) == []
    # Clearing also resets the manifest so a later sync re-copies cleanly.
    assert load_manifest(archive_dir) == {}


def test_prune_on_empty_archive_is_noop(tmp_path: Path) -> None:
    stats = prune(tmp_path / "nonexistent", clear=True)
    assert stats.files_removed == 0
```

- [ ] **Step 4.2.2: Run to verify it fails**

Run: `pytest tests/test_sessions_archive.py -k prune -v`
Expected: FAIL — `ImportError: cannot import name 'prune'`.

- [ ] **Step 4.2.3: Implement `prune` + `PruneStats`**

Append to `src/rekol/sessions/archive.py`:

```python
@dataclass
class PruneStats:
    """Tally of a ``prune`` run."""

    files_removed: int = 0


def prune(archive_dir: Path, *, clear: bool) -> PruneStats:
    """Manual retention: flat-file removal of archive contents.

    v1 supports only ``clear=True`` (remove every archived ``.jsonl`` and reset
    the manifest) — the manual off-ramp. Auto-compaction/retention by age or
    size is a deferred fast-follow. SOFT-FAIL: an ``OSError`` removing one file
    is skipped; the rest still go.

    Returns the :class:`PruneStats` tally. A non-existent archive is a no-op.
    """
    stats = PruneStats()
    archive_dir = Path(archive_dir)
    if not archive_dir.is_dir():
        return stats
    if not clear:
        # v1 has no partial-trim policy; without clear there is nothing to do.
        return stats
    for archived_path in sorted(archive_dir.glob("**/*.jsonl")):
        try:
            archived_path.unlink()
        except OSError:
            continue
        stats.files_removed += 1
    # Reset the manifest so a later sync treats every live file as new.
    save_manifest(archive_dir, {})
    return stats
```

- [ ] **Step 4.2.4: Run to verify it passes**

Run: `pytest tests/test_sessions_archive.py -v`
Expected: PASS (whole file). `ruff check && mypy src/rekol/sessions/archive.py` — clean.

- [ ] **Step 4.2.5: Commit**

```bash
git add src/rekol/sessions/archive.py tests/test_sessions_archive.py
git commit -m "feat: manual archive prune --clear

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Phase 5: `rekol archive` CLI

Manual sync, `--from-index` backfill (with the one-time marker + the one-line notice), `--prune`/`--clear`. No lock (writes `sessions.db` + flat files, never the curated `index.db`); soft-fails.

### Task 5.1: `rekol archive` command + registration

**Files:**
- Create: `src/rekol/cli_archive.py`
- Modify: `src/rekol/cli.py`
- Test: `tests/test_cli_archive.py` (create)

- [ ] **Step 5.1.1: Write the failing test**

Create `tests/test_cli_archive.py`:

```python
"""Smoke + behavior tests for the `rekol archive` CLI."""

from __future__ import annotations

import shutil
from pathlib import Path

from click.testing import CliRunner

from rekol.cli_archive import main as archive_cmd
from rekol.sessions.archive import BACKFILL_MARKER_FILENAME

FIXTURE = Path(__file__).parent / "fixtures" / "sample_session.jsonl"


def _home_with_projects(tmp_path: Path, monkeypatch) -> tuple[Path, Path]:
    home = tmp_path / "memhome"
    home.mkdir()
    projects = tmp_path / "projects" / "proj-a"
    projects.mkdir(parents=True)
    shutil.copy(FIXTURE, projects / "session.jsonl")
    archive = tmp_path / "archive"
    monkeypatch.setenv("REKOL_HOME", str(home))
    monkeypatch.setenv("REKOL_ARCHIVE_DIR", str(archive))
    (home / "rekol.config.yaml").write_text(
        f"claude_projects_dir: {projects.parent}\nembedding_model: test-hashing\n"
    )
    return home, archive


def test_archive_sync_copies_live_into_archive(tmp_path: Path, monkeypatch) -> None:
    _home, archive = _home_with_projects(tmp_path, monkeypatch)
    result = CliRunner().invoke(archive_cmd, [])
    assert result.exit_code == 0, result.output
    assert (archive / "proj-a" / "session.jsonl").exists()
    assert "files_copied=1" in result.output


def test_archive_disabled_is_a_clean_noop(tmp_path: Path, monkeypatch) -> None:
    home, archive = _home_with_projects(tmp_path, monkeypatch)
    (home / "rekol.config.yaml").write_text(
        "archive_enabled: false\nembedding_model: test-hashing\n"
    )
    result = CliRunner().invoke(archive_cmd, [])
    assert result.exit_code == 0, result.output
    assert "archive_enabled=false" in result.output
    assert not archive.exists()


def test_archive_prune_clear_empties_archive(tmp_path: Path, monkeypatch) -> None:
    _home, archive = _home_with_projects(tmp_path, monkeypatch)
    CliRunner().invoke(archive_cmd, [])
    assert (archive / "proj-a" / "session.jsonl").exists()
    result = CliRunner().invoke(archive_cmd, ["--prune", "--clear"])
    assert result.exit_code == 0, result.output
    assert list(archive.glob("**/*.jsonl")) == []


def test_archive_command_registered_in_group() -> None:
    from rekol.cli import main as group

    assert "archive" in group.commands


def test_from_index_writes_marker(tmp_path: Path, monkeypatch) -> None:
    """`--from-index` runs the backfill and writes the one-time guard marker, so
    the auto-once path (session-index) never re-runs it."""
    _home, archive = _home_with_projects(tmp_path, monkeypatch)
    # Need a sessions.db to backfill from: build one with session-index first.
    from rekol.cli_session_index import main as session_index_cmd

    CliRunner().invoke(session_index_cmd, ["--full"])
    result = CliRunner().invoke(archive_cmd, ["--from-index"])
    assert result.exit_code == 0, result.output
    assert (archive / BACKFILL_MARKER_FILENAME).exists()
    assert "backfill sessions_reconstructed=" in result.output
```

- [ ] **Step 5.1.2: Run to verify it fails**

Run: `pytest tests/test_cli_archive.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'rekol.cli_archive'`. (The registration + marker cases need the module, the CLI registration, and `_run_backfill`'s marker write — all in Step 5.1.3.)

- [ ] **Step 5.1.3: Implement `cli_archive.py`**

Create `src/rekol/cli_archive.py`:

```python
"""rekol archive: manual transcript-archive operations.

Default (no flags)   sync the durable archive from ~/.claude/projects
--from-index         reconstruct archive files for sessions present in the
                     index but missing from the archive (the upgrade backfill;
                     also auto-run once by session-index — see that module)
--prune --clear      flat-file removal of the whole archive (manual retention)

SOFT-FAIL: archiving never blocks anything; an OSError degrades to a logged
warning and exit 0 (the SessionEnd-hook contract — a broken archive step must
not stall a session).

LOCKING: deliberately none. This command touches the durable archive (flat
files) and reads ``sessions.db``; it NEVER touches the curated ``index.db``, so
it must not take the curated ``index_write_lock`` (#24/#25) — doing so would hang
the SessionEnd hook behind a curated rebuild and couple two independent
subsystems. ``sessions.db`` concurrency is the DB's own WAL + 30s busy_timeout;
the flat-file reconcile is idempotent. See the design's "Locking" section.
"""

from __future__ import annotations

import sys

import click

from rekol.config import load_config, load_rekolignore_patterns
from rekol.sessions.archive import (
    BACKFILL_MARKER_FILENAME,
    archive_directory,
    backfill_from_index,
    prune,
)
from rekol.sessions.store import SessionStore


@click.command()
@click.option(
    "--from-index",
    "from_index",
    is_flag=True,
    help="Reconstruct archive files for sessions in the index but missing from "
    "the archive (text-only/lossy). The upgrade backfill; safe to re-run.",
)
@click.option(
    "--prune",
    "do_prune",
    is_flag=True,
    help="Manual retention. With --clear, removes every archived transcript.",
)
@click.option(
    "--clear",
    "do_clear",
    is_flag=True,
    help="With --prune: remove ALL archived transcripts and reset the manifest.",
)
def main(from_index: bool, do_prune: bool, do_clear: bool) -> None:
    """Sync, backfill, or prune the durable transcript archive."""
    cfg = load_config()
    archive_dir = cfg.archive_dir

    if not cfg.archive_enabled:
        # The off-switch: report and exit cleanly so scripts/tests can detect it.
        click.echo("archive_enabled=false in config; nothing to do.")
        sys.exit(0)

    # Combine config excludes with any per-folder .rekolignore at the projects
    # root, so a sensitive project is never archived from either source.
    exclude_patterns = list(cfg.exclude_paths) + load_rekolignore_patterns(
        cfg.claude_projects_dir
    )

    # NO LOCK: this writes flat files (and reads sessions.db), never the curated
    # index.db, so the curated index_write_lock must not be reused here (it would
    # hang the SessionEnd hook behind a curated rebuild). The flat-file reconcile
    # is idempotent and sessions.db has its own WAL + busy_timeout.
    try:
        if do_prune:
            stats = prune(archive_dir, clear=do_clear)
            click.echo(f"pruned files_removed={stats.files_removed}")
        elif from_index:
            _run_backfill(cfg, archive_dir, exclude_patterns)
        else:
            stats = archive_directory(
                cfg.claude_projects_dir, archive_dir, exclude_patterns
            )
            click.echo(
                f"files_seen={stats.files_seen} "
                f"files_copied={stats.files_copied} "
                f"files_replaced={stats.files_replaced} "
                f"files_skipped_unchanged={stats.files_skipped_unchanged} "
                f"files_skipped_excluded={stats.files_skipped_excluded} "
                f"files_diverged_sidecar={stats.files_diverged_sidecar} "
                f"files_removed_excluded={stats.files_removed_excluded} "
                f"files_errored={stats.files_errored}"
            )
    except OSError as exc:
        # Soft-fail: never block on a filesystem error (disk full, unwritable).
        click.echo(f"archive operation degraded (non-fatal): {exc}", err=True)
        sys.exit(0)


def _run_backfill(cfg, archive_dir, exclude_patterns) -> None:
    """Run the index→archive backfill and write the one-time marker + notice."""
    sessions_db = cfg.sessions_db_path
    if not sessions_db.exists():
        click.echo("no sessions.db to backfill from; nothing to do.")
        return
    # Write the one-time guard marker BEFORE running the backfill. backfill is
    # idempotent (it skips sessions already present), so the marker firing first
    # means a crash MID-backfill still leaves the marker set — the auto-once notice
    # then fires exactly once and the next normal sync (or an explicit
    # `rekol archive --from-index`) finishes any leftover work. Writing the marker
    # only on success would re-run the whole backfill (and re-emit the notice) on
    # every run until one completes uninterrupted.
    archive_dir.mkdir(parents=True, exist_ok=True)
    (archive_dir / BACKFILL_MARKER_FILENAME).touch()
    with SessionStore(db_path=sessions_db, dim=384) as store:
        store.init_schema()
        stats = backfill_from_index(store, archive_dir, exclude_patterns)
    click.echo(
        f"backfill sessions_reconstructed={stats.sessions_reconstructed} "
        f"sessions_skipped_present={stats.sessions_skipped_present} "
        f"sessions_skipped_excluded={stats.sessions_skipped_excluded} "
        f"sessions_errored={stats.sessions_errored}"
    )


if __name__ == "__main__":
    sys.exit(main())
```

Register it in `src/rekol/cli.py`: add the import beside the others —

```python
from rekol.cli_archive import main as archive_cmd
```

— and the registration line (after the `session-index` line):

```python
main.add_command(archive_cmd, name="archive")
```

- [ ] **Step 5.1.4: Run to verify the CLI tests pass**

Run: `pytest tests/test_cli_archive.py tests/test_cli_group.py -v`
Expected: PASS (all five cases — sync, disabled-noop, prune-clear, group-registration, and the `--from-index` marker contract). `test_cli_group` asserts the subcommand list; `test_archive_command_registered_in_group` is the explicit per-command check.

- [ ] **Step 5.1.5: Commit**

```bash
git add src/rekol/cli_archive.py src/rekol/cli.py tests/test_cli_archive.py
git commit -m "feat: add rekol archive command (sync / --from-index / --prune)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

> The backfill marker + registration are exercised by the `test_archive_command_registered_in_group` and `test_from_index_writes_marker` cases folded into Step 5.1.1 above — they ride the same TDD cycle and commit as the command itself (no separate task; the marker write lives in `_run_backfill` in Step 5.1.3).

---

## Phase 6: Ingest-from-archive wiring (★ the data-loss fix + headline regression)

This is the payoff. `session-index` archive-syncs first, then ingests **from the archive**, soft-failing to live on `OSError`, auto-backfilling once. NO curated `index_write_lock` (it writes `sessions.db`, not `index.db`; the session DB's WAL + busy_timeout is the only serialization — see the design's "Locking").

### Task 6.1: `session-index` archives first, then ingests from the archive

**Files:**
- Modify: `src/rekol/cli_session_index.py`
- Test: `tests/test_archive_integration.py` (create), `tests/test_cli_session_index.py`

- [ ] **Step 6.1.1: Write the HEADLINE REGRESSION test first**

Create `tests/test_archive_integration.py`:

```python
"""Integration tests for the durable-archive data-loss fix (#8).

The headline regression: archive a session, DELETE the live .jsonl, rebuild the
index from scratch, and assert the session is still searchable — proving the
archive (not the ephemeral live file) is the source of truth for rebuilds."""

from __future__ import annotations

import shutil
from pathlib import Path

from click.testing import CliRunner

from cache_helpers import cache_dir_for
from rekol.cli_session_index import main as session_index_cmd
from rekol.sessions.store import SessionStore

FIXTURE = Path(__file__).parent / "fixtures" / "sample_session.jsonl"


def _home(tmp_path: Path, monkeypatch) -> tuple[Path, Path, Path]:
    home = tmp_path / "memhome"
    home.mkdir()
    projects = tmp_path / "projects" / "proj-a"
    projects.mkdir(parents=True)
    live_jsonl = projects / "session.jsonl"
    shutil.copy(FIXTURE, live_jsonl)
    archive = tmp_path / "archive"
    monkeypatch.setenv("REKOL_HOME", str(home))
    monkeypatch.setenv("REKOL_ARCHIVE_DIR", str(archive))
    (home / "rekol.config.yaml").write_text(
        f"claude_projects_dir: {projects.parent}\nembedding_model: test-hashing\n"
    )
    return home, live_jsonl, archive


def test_session_survives_live_deletion_then_full_rebuild(tmp_path: Path, monkeypatch) -> None:
    home, live_jsonl, archive = _home(tmp_path, monkeypatch)
    runner = CliRunner()

    # 1. Index once — this archive-syncs the session, then ingests from archive.
    first = runner.invoke(session_index_cmd, ["--incremental"])
    assert first.exit_code == 0, first.output
    assert (archive / "proj-a" / "session.jsonl").exists()

    # 2. Claude Code "cleans up" the original — delete the live .jsonl.
    live_jsonl.unlink()
    assert not live_jsonl.exists()

    # 3. Wipe the cache (sessions.db) and do a FULL rebuild from scratch.
    sessions_db = cache_dir_for(home) / "sessions.db"
    if sessions_db.exists():
        sessions_db.unlink()
    rebuilt = runner.invoke(session_index_cmd, ["--full"])
    assert rebuilt.exit_code == 0, rebuilt.output

    # 4. The session is STILL searchable — rebuilt losslessly from the archive.
    store = SessionStore(db_path=sessions_db, dim=384)
    store.init_schema()
    try:
        hits = store.search_fts("hello there", top_k=5)
        assert any("hello" in h["content"] for h in hits), [h["content"] for h in hits]
    finally:
        store.close()


def test_unwritable_archive_falls_back_to_live_and_exits_zero(tmp_path: Path, monkeypatch) -> None:
    """SOFT-FAIL: an archive dir that raises OSError on write must NOT block
    indexing — the run degrades to ingesting from live and exits 0 (the hook
    contract). Same cycle/commit as the wiring; not a separate task."""
    home, _live, archive = _home(tmp_path, monkeypatch)
    runner = CliRunner()

    import rekol.cli_session_index as session_mod

    def boom(live_root, archive_dir, exclude_patterns):
        raise OSError("simulated unwritable archive dir")

    monkeypatch.setattr(session_mod, "archive_directory", boom)
    result = runner.invoke(session_index_cmd, ["--incremental"])
    # Soft-fail: still indexes (from live), exit 0, with a non-fatal notice.
    assert result.exit_code == 0, result.output
    assert "degraded (non-fatal)" in result.output
    # The session was still ingested from live despite the archive failure.
    store = SessionStore(db_path=cache_dir_for(home) / "sessions.db", dim=384)
    store.init_schema()
    try:
        assert store.count_messages() > 0
    finally:
        store.close()


def test_excluded_project_never_archived_or_indexed(tmp_path: Path, monkeypatch) -> None:
    """End-to-end exclude: a project whose REAL cwd matches an exclude is neither
    archived nor indexed; a non-excluded project is both. Match is on cwd, not the
    slug folder (the design decision). Same cycle/commit as the wiring."""
    home = tmp_path / "memhome"
    home.mkdir()
    # On-disk slug folders are URL-encoded; the matchable path is the row cwd.
    secret = tmp_path / "projects" / "-Users-x-secret-project"
    public = tmp_path / "projects" / "-Users-x-public"
    secret.mkdir(parents=True)
    public.mkdir(parents=True)
    _write_one_row(secret / "s.jsonl", session_id="sess-secret", cwd="/Users/x/secret-project")
    _write_one_row(public / "p.jsonl", session_id="sess-public", cwd="/Users/x/public")
    archive = tmp_path / "archive"
    monkeypatch.setenv("REKOL_HOME", str(home))
    monkeypatch.setenv("REKOL_ARCHIVE_DIR", str(archive))
    (home / "rekol.config.yaml").write_text(
        f"claude_projects_dir: {tmp_path / 'projects'}\n"
        "embedding_model: test-hashing\n"
        "exclude_paths:\n  - '*/secret-project*'\n"
    )

    result = CliRunner().invoke(session_index_cmd, ["--full"])
    assert result.exit_code == 0, result.output
    # Excluded project is neither archived nor indexed; the public one is both.
    assert not (archive / "-Users-x-secret-project").exists()
    assert (archive / "-Users-x-public" / "p.jsonl").exists()
    store = SessionStore(db_path=cache_dir_for(home) / "sessions.db", dim=384)
    store.init_schema()
    try:
        cwds = {r["cwd"] for r in store.conn.execute("SELECT DISTINCT cwd FROM messages")}
        assert not any(c and "secret-project" in c for c in cwds), cwds
    finally:
        store.close()
```

Add the `_write_one_row` helper near the top of the file (used by the exclude case to
write a transcript whose `cwd` is the matchable real project path):

```python
import json


def _write_one_row(path: Path, *, session_id: str, cwd: str) -> None:
    """Write a single-row transcript carrying a real `cwd` for exclude matching."""
    row = {
        "type": "user",
        "uuid": f"{session_id}-u1",
        "sessionId": session_id,
        "cwd": cwd,
        "timestamp": "2026-05-01T10:00:00Z",
        "message": {"role": "user", "content": "hello there from the test"},
    }
    path.write_text(json.dumps(row) + "\n", encoding="utf-8")
```

- [ ] **Step 6.1.2: Run to verify it fails**

Run: `pytest tests/test_archive_integration.py -v`
Expected: FAIL — the headline regression fails first (after deleting the live `.jsonl`
and rebuilding, the session is gone — today's behavior reads only live files). That
failure IS the data-loss bug; the wiring below fixes it (and makes the soft-fail and
exclude cases pass too).

- [ ] **Step 6.1.3: Wire archive-sync + ingest-from-archive into `session-index`**

Modify `src/rekol/cli_session_index.py`. Add imports:

```python
from rekol.config import load_config, load_rekolignore_patterns
from rekol.sessions.archive import (
    BACKFILL_MARKER_FILENAME,
    archive_directory,
    backfill_from_index,
)
```

NOTE: do NOT import `index_write_lock` here. `session-index` writes `sessions.db`,
a separate database from the curated `index.db`; the curated `index_write_lock`
(#24/#25) must not be reused — it would hang the SessionEnd hook behind a curated
rebuild and couple two independent subsystems. `sessions.db` concurrency is the
DB's own WAL + 30s `busy_timeout`; archive-sync writes idempotent flat files. (See
the design's "Locking" section. A dedicated `.session-index.lock` is YAGNI for v1.)

Inside `main()`, after the `projects_root` existence guard and embedder setup, replace the existing `with SessionStore(...) as store:` block so it archives first, then ingests from the archive. The shape:

```python
    # Determine where ingest reads FROM. With the archive on, we archive-sync the
    # live projects dir into the durable archive, then ingest from the ARCHIVE —
    # so a rebuild is lossless even if Claude Code deleted the live originals
    # (#8). If archiving soft-fails (OSError), we degrade to ingesting from live
    # (today's behavior); the next successful run catches up.
    archive_dir = cfg.archive_dir
    ingest_root = projects_root
    if cfg.archive_enabled:
        exclude_patterns = list(cfg.exclude_paths) + load_rekolignore_patterns(projects_root)
        try:
            archive_stats = archive_directory(projects_root, archive_dir, exclude_patterns)
            ingest_root = archive_dir
            if progress:
                click.echo(
                    f"... archived files_copied={archive_stats.files_copied} "
                    f"files_replaced={archive_stats.files_replaced} "
                    f"files_diverged_sidecar={archive_stats.files_diverged_sidecar}",
                    err=True,
                )
        except OSError as exc:
            # SOFT-FAIL: archiving must never block indexing. Fall back to live.
            click.echo(
                f"archive-sync degraded (non-fatal): {exc}; ingesting from live", err=True
            )
            ingest_root = projects_root

    # NO LOCK around this block: sessions.db is a separate DB from the curated
    # index.db, so the curated index_write_lock (#24/#25) is NOT reused — it would
    # hang the SessionEnd hook behind a curated rebuild for no correctness benefit.
    # sessions.db concurrency is its own WAL + busy_timeout (set in SessionStore);
    # archive-sync above writes idempotent flat files. See the design's "Locking".
    repaired = 0
    try:
        with SessionStore(db_path=cfg.sessions_db_path, dim=store_dim) as store:
            store.init_schema()
            if embedder is not None:
                store.reconcile_embedding_dim(embedder.dim)
            # Auto-once backfill: if the archive is on and we have never
            # backfilled, reconstruct any indexed-but-unarchived sessions so
            # nothing predating the archive is lost. Marker guards re-runs.
            if cfg.archive_enabled and ingest_root == archive_dir:
                _maybe_backfill_once(cfg, archive_dir, store)
            stats = ingest_directory(
                ingest_root, store, force=mode_full, embedder=embedder, progress_cb=progress_cb
            )
            if mode_full:
                store.rebuild_fts()
            if embedder is not None:
                def _repair_progress(done: int) -> None:
                    click.echo(f"... {done} messages embedded (repair)", err=True)
                repaired = embed_missing(
                    store, embedder, progress_cb=_repair_progress if progress else None
                )
    except SessionStoreDimMismatchError as exc:
        click.echo(str(exc), err=True)
        sys.exit(2)
```

Add the auto-once helper (module level, with the one-time notice line):

```python
def _maybe_backfill_once(cfg, archive_dir, store) -> None:
    """Run the index→archive backfill exactly once on upgrade.

    Guarded by ``archive/.backfilled-from-index``: if present, no-op. Otherwise
    reconstruct any session that is in ``sessions.db`` but missing from the
    archive (so history predating the archive isn't lost on a future rebuild),
    write the marker, and emit ONE non-blocking notice line disclosing what we
    did, where it lives, and the off-switch. Soft-fails on OSError.
    """
    marker = archive_dir / BACKFILL_MARKER_FILENAME
    if marker.exists():
        return
    exclude_patterns = list(cfg.exclude_paths) + load_rekolignore_patterns(cfg.claude_projects_dir)
    try:
        # Touch the marker BEFORE the backfill. backfill is idempotent (skips
        # already-present sessions), so writing the marker first guarantees the
        # one-time notice fires exactly once even if the process is killed
        # mid-backfill — a later sync finishes any leftover work. Marking only on
        # success would re-run + re-notify on every run until one completes.
        marker.touch()
        stats = backfill_from_index(store, archive_dir, exclude_patterns)
    except OSError as exc:
        click.echo(f"archive backfill degraded (non-fatal): {exc}", err=True)
        return
    if stats.sessions_reconstructed > 0:
        # The one-time disclosure: default-ON honesty, no per-session nag.
        click.echo(
            f"rekol built a local archive of {stats.sessions_reconstructed} past "
            f"session(s) so they're not lost — at {archive_dir}. "
            f"It's on your disk, never uploaded. Turn it off with "
            f"`archive_enabled: false` in rekol.config.yaml.",
            err=True,
        )
```

NOTE: the original `with SessionStore(...)` body and its surrounding `try/except SessionStoreDimMismatchError` are fully replaced by the block above — verify no duplicate `store.init_schema()`/`ingest_directory` call remains. (No lock is added; see the "NO LOCK" comment in the block.)

#### Expected transition behavior: `files_seen` is re-keyed on the first post-upgrade run

`files_seen` (the `sessions.db` table keyed by `jsonl_path`, via `should_skip_file`/
`record_file_seen` in `store.py`) records the path that was ingested. Before this
change, ingest read the **live** projects dir, so the keys are absolute live paths
(e.g. `~/.claude/projects/<slug>/<id>.jsonl`). After the repoint, ingest reads the
**archive** dir, so the keys become absolute archive paths
(e.g. `<archive>/<slug>/<id>.jsonl`). These differ, so on the FIRST `--incremental`
run after upgrade NONE of the archive paths are found in `files_seen` and every file
is re-walked once.

This is benign and self-healing, NOT a bug:
- Every re-walked message hits the message-level `UNIQUE(session_id, message_uuid)`
  dedupe (`messages_skipped_dupe` rises; `messages_inserted` stays 0 for already-indexed
  content), so nothing is duplicated.
- The first run `record_file_seen`s each archive path, so the SECOND and all later
  incremental runs hit the mtime+size skip again (`files_skipped_unchanged`) — the
  steady-state cheap path is restored automatically. The stale live-path rows in
  `files_seen` are simply never consulted again (harmless dead rows; a later `--full`
  ignores `files_seen` entirely).

No migration code is needed — document it and assert the self-heal in the test below.

- [ ] **Step 6.1.4: Update `test_session_index_incremental_is_idempotent` for the archive repoint**

The existing test (`tests/test_cli_session_index.py`, ~line 56) seeds a live projects
dir, runs `--full`, then asserts `--incremental` reports `files_skipped_unchanged=1`.
With the repoint, the second run's skip is now gated on the **archive's** `files_seen`
key, not the live one. Point the archive at a hermetic `tmp_path` dir via
`REKOL_ARCHIVE_DIR`, seed `archive_enabled: true` explicitly so the test does not depend
on the default, and assert the self-heal: the second run skips the **archive-keyed** file.

Replace the test body with:

```python
def test_session_index_incremental_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "memhome"
    home.mkdir()
    projects = tmp_path / "fake-projects" / "proj-a"
    projects.mkdir(parents=True)
    shutil.copy(FIXTURE, projects / "session.jsonl")

    # Archive on, pointed at a hermetic tmp dir so the test never writes a real
    # archive. The repoint means ingest reads from here, so files_seen is keyed on
    # the ARCHIVE path — the second incremental run must still skip it (self-heal).
    archive = tmp_path / "archive"
    monkeypatch.setenv("MEMORY_HOME", str(home))
    monkeypatch.setenv("REKOL_ARCHIVE_DIR", str(archive))
    (home / "memory.config.yaml").write_text(
        f"claude_projects_dir: {projects.parent}\n"
        "embedding_model: test-hashing\n"
        "archive_enabled: true\n"
    )

    runner = CliRunner()
    setup_result = runner.invoke(cli_main, ["--full"])
    assert setup_result.exit_code == 0, setup_result.output
    # The session was archived and (after --full) re-keyed in files_seen by its
    # archive path.
    assert (archive / "proj-a" / "session.jsonl").exists()

    result = runner.invoke(cli_main, ["--incremental"])
    assert result.exit_code == 0, result.output
    # Incremental against the archive-keyed files_seen → mtime+size skip, no inserts.
    assert "messages_inserted=0" in result.output
    assert "files_skipped_unchanged=1" in result.output
```

- [ ] **Step 6.1.5: Run the full archive + session-index suite, then the whole gate**

Run: `pytest tests/test_archive_integration.py tests/test_cli_session_index.py -v`
Expected: PASS — ALL cases now green:
- the headline regression (session searchable after live deletion + full rebuild);
- the soft-fail case (`test_unwritable_archive_falls_back_to_live_and_exits_zero` — the
  `except OSError` fallback degrades to ingesting from live, exit 0);
- the exclude end-to-end case (`test_excluded_project_never_archived_or_indexed` —
  excluded project, matched on its real cwd, is neither archived nor indexed);
- the updated idempotent-incremental test (now archive-keyed) and the remaining
  pre-existing session-index tests (FTS-in-sync-after-full, embed-heals, dim mismatch).

If any other existing session-index test asserts an exact `files_seen`/
`files_skipped_unchanged` count tied to the live dir, apply the same archive-keyed
treatment (set `REKOL_ARCHIVE_DIR` under `tmp_path`).

Then run the whole gate before committing — this phase is the integration payoff, so
hold it to the full CI bar: `ruff check . && ruff format --check . && mypy src && pytest`.
Fix any fallout before the commit.

- [ ] **Step 6.1.6: Commit**

```bash
git add src/rekol/cli_session_index.py tests/test_archive_integration.py tests/test_cli_session_index.py
git commit -m "feat: ingest from the durable archive; index rebuilds losslessly (#8)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

> The soft-fail and exclude-end-to-end cases (folded into Step 6.1.1 above) ride this
> same RED→GREEN→commit cycle — they are additional assertions on the one wiring change,
> not separate TDD commits. (Previously split into Tasks 6.2/6.3; consolidated to avoid
> three near-empty "test-only" commits with no coverage loss.)

---

## Phase 7: Doctor archive health line

Add an archive finding: dir present/writable, N sessions archived, last-archive time; **warn if the archive resolves to a cloud/placeholder mount** (Dropbox/iCloud/Drive/OneDrive), reusing `onboarding.detect`.

### Task 7.1: `_check_archive` + report wiring

**Files:**
- Modify: `src/rekol/cli_doctor.py`
- Test: `tests/test_cli_doctor.py`

- [ ] **Step 7.1.1: Write the failing tests**

Append to `tests/test_cli_doctor.py` (match the file's existing fixture/import style):

```python
def test_doctor_reports_archive_present(tmp_path, monkeypatch):
    from rekol.cli_doctor import _check_archive
    from rekol.config import load_config

    monkeypatch.setenv("REKOL_HOME", str(tmp_path))
    monkeypatch.setenv("REKOL_ARCHIVE_DIR", str(tmp_path / "archive"))
    (tmp_path / "archive" / "projA").mkdir(parents=True)
    (tmp_path / "archive" / "projA" / "s.jsonl").write_text('{"type":"user"}\n')

    cfg = load_config()
    findings = _check_archive(cfg)
    archive_findings = [f for f in findings if "archive" in f.label]
    assert archive_findings, [f.label for f in findings]
    detail = " ".join(f.detail for f in archive_findings)
    assert "1" in detail  # one archived session counted


def test_doctor_warns_on_cloud_synced_archive(tmp_path, monkeypatch):
    """A synced archive puts verbatim secrets in the cloud and on-demand sync can
    dehydrate files — doctor must flag it (PROBLEM/warn), not stay silent."""
    from rekol.cli_doctor import _check_archive
    from rekol.config import load_config

    cloud = tmp_path / "Dropbox" / "rekol-archive"
    cloud.mkdir(parents=True)
    monkeypatch.setenv("REKOL_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("REKOL_ARCHIVE_DIR", str(cloud))

    cfg = load_config()
    findings = _check_archive(cfg)
    detail = " ".join(f.detail.lower() for f in findings)
    assert "cloud" in detail or "sync" in detail
```

- [ ] **Step 7.1.2: Run to verify it fails**

Run: `pytest tests/test_cli_doctor.py -k archive -v`
Expected: FAIL — `ImportError: cannot import name '_check_archive'`.

- [ ] **Step 7.1.3: Implement `_check_archive` and wire it into `run_doctor`**

In `src/rekol/cli_doctor.py`, add the import:

```python
from rekol.onboarding.detect import default_cloud_sync_candidates
```

Add the check function (after `_check_session_index`):

```python
def _check_archive(cfg: Config) -> list[Finding]:
    """Inspect the durable transcript archive: presence, writability, count, age,
    and a SECURITY warning when it resolves under a cloud-sync mount.

    The archive holds verbatim prompts (and any pasted secrets). The default is a
    local, non-synced dir; if a user relocated it under Dropbox/iCloud/Drive/
    OneDrive, on-demand/streaming sync can dehydrate files (a rebuild then reads
    placeholders) AND the secrets leave the machine. We flag that loudly.
    """
    findings: list[Finding] = []
    if not cfg.archive_enabled:
        findings.append(
            Finding(
                label="transcript archive",
                status=Status.INFO,
                detail="archive_enabled=false (durable archive off; rebuilds read live only)",
            )
        )
        return findings

    archive_dir = cfg.archive_dir

    # Cloud-mount heuristic: is the resolved archive under a known sync root?
    for label, sync_root in default_cloud_sync_candidates().items():
        try:
            archive_dir.relative_to(sync_root)
        except ValueError:
            continue
        findings.append(
            Finding(
                label="archive location",
                status=Status.PROBLEM,
                detail=(
                    f"archive resolves under {label} ({archive_dir}) — a synced archive "
                    f"puts verbatim transcripts (and any pasted secrets) in the cloud, and "
                    f"on-demand sync can dehydrate files so a rebuild reads placeholders"
                ),
                remedy="move it local: set REKOL_ARCHIVE_DIR (or archive_dir) to a non-synced path",
            )
        )
        break

    if not archive_dir.exists():
        findings.append(
            Finding(
                label="transcript archive",
                status=Status.INFO,
                detail=f"not built yet (no {archive_dir})",
                remedy="rekol archive",
            )
        )
        return findings

    # Count archived sessions (flat .jsonl files) and find the newest mtime.
    archived = list(archive_dir.glob("**/*.jsonl"))
    newest_mtime: int | None = None
    for path in archived:
        try:
            mtime = int(path.stat().st_mtime)
        except OSError:
            continue
        if newest_mtime is None or mtime > newest_mtime:
            newest_mtime = mtime
    writable = os.access(archive_dir, os.W_OK)
    findings.append(
        Finding(
            label="transcript archive",
            status=Status.OK if writable else Status.PROBLEM,
            detail=(
                f"{len(archived)} archived session file(s) at {archive_dir}; "
                f"last archived {_format_last_indexed(newest_mtime)}; "
                f"{'writable' if writable else 'NOT writable'}"
            ),
            remedy=None if writable else "fix permissions on the archive dir, or relocate it",
        )
    )
    return findings
```

Add `import os` if not already present at the top of `cli_doctor.py`. Wire it into `run_doctor` (after `findings.extend(_check_session_index(cfg, embedder))`):

```python
    findings.extend(_check_archive(cfg))
```

- [ ] **Step 7.1.4: Run to verify the doctor tests pass**

Run: `pytest tests/test_cli_doctor.py -v`
Expected: PASS (new + existing). `ruff check && mypy src/rekol/cli_doctor.py` — clean.

- [ ] **Step 7.1.5: Commit**

```bash
git add src/rekol/cli_doctor.py tests/test_cli_doctor.py
git commit -m "feat: doctor reports archive health + warns on cloud-synced archive

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Phase 8: install.sh / uninstall.sh / README / bats

Shell + docs. Depends on Phase 1 (the `archive_enabled` key) and Phase 5 (`rekol archive --from-index`). Bats tests run under the `bats install` CI gate.

### Task 8.1: install.sh — `--no-archive`, `--archive-dir`, `--help`, disclosure, backfill

**Files:**
- Modify: `install.sh`
- Test: `tests/test_install.bats`

- [ ] **Step 8.1.1: Write the failing bats tests**

Append to `tests/test_install.bats` (follow the file's `setup`/helper conventions; `REKOL_ARCHIVE_DIR` is sandboxed under `TESTROOT` so no real archive is written):

```bash
@test "--no-archive seeds archive_enabled:false in the config" {
    export REKOL_ARCHIVE_DIR="${TESTROOT}/archive"
    run "${COMPONENT_DIR}/install.sh" \
        --tools-home "${TOOLS_HOME}" --bin-dir "${BIN_DIR}" \
        --test-mode --no-archive
    [ "$status" -eq 0 ]
    grep -q '^archive_enabled: false' "${MEMORY_HOME}/rekol.config.yaml"
}

@test "--archive-dir is recorded so the archive lands where asked" {
    export REKOL_ARCHIVE_DIR=""
    run "${COMPONENT_DIR}/install.sh" \
        --tools-home "${TOOLS_HOME}" --bin-dir "${BIN_DIR}" \
        --test-mode --archive-dir "${TESTROOT}/custom-archive"
    [ "$status" -eq 0 ]
    # The chosen archive_dir is persisted to config.
    grep -q "archive_dir: ${TESTROOT}/custom-archive" "${MEMORY_HOME}/rekol.config.yaml"
}

@test "--help prints usage including the archive flags" {
    run "${COMPONENT_DIR}/install.sh" --help
    [ "$status" -eq 0 ]
    [[ "$output" == *"--no-archive"* ]]
    [[ "$output" == *"--archive-dir"* ]]
}

@test "install output discloses the local archive" {
    export REKOL_ARCHIVE_DIR="${TESTROOT}/archive"
    run "${COMPONENT_DIR}/install.sh" \
        --tools-home "${TOOLS_HOME}" --bin-dir "${BIN_DIR}" --test-mode
    [ "$status" -eq 0 ]
    [[ "$output" == *"local copy of your sessions"* ]]
}
```

- [ ] **Step 8.1.2: Run to verify they fail**

Run: `bats tests/test_install.bats -f "archive|help|discloses"`
Expected: FAIL (`--no-archive`/`--archive-dir`/`--help` unknown args; no disclosure line).

- [ ] **Step 8.1.3: Implement the install.sh changes**

In `install.sh`:

1. Add to the header comment's "Optional flags" block:
```bash
#   --no-archive    disable the durable transcript archive (seeds archive_enabled:false)
#   --archive-dir P set the durable archive location (default ~/.local/share/rekol/archive)
#   --help          print this usage block and exit
```

2. Add mutable config defaults near the other flag vars (after `BIN_DIR="$BIN_DIR_DEFAULT"`):
```bash
DO_ARCHIVE=1
ARCHIVE_DIR=""
```

3. Add a `usage()` function (mirror uninstall.sh's) and handle `--help`, `--no-archive`, `--archive-dir` in the arg-parse `case`:
```bash
    --no-archive)  DO_ARCHIVE=0;                       shift ;;
    --archive-dir) ARCHIVE_DIR="$2";                   shift 2 ;;
    --help|-h)     usage; exit 0 ;;
```

4. After Step 5 (template seeding) — where `rekol.config.yaml` exists — seed the archive keys idempotently. Add a new step that appends `archive_enabled: false` when `--no-archive`, and `archive_dir: <path>` when `--archive-dir` was given, only if the key is not already present. **Define `CONFIG_YAML_SEED` explicitly at the top of the block** (it does not exist yet at Step 5.5 — Step 8.5's `CONFIG_YAML` is resolved far later in the script), mirroring the real Step 8.5 resolution (prefer `rekol.config.yaml`, fall back to `memory.config.yaml`):
```bash
# Step 5.5 — seed archive config keys when overridden by flags.
# Default-ON: with no flag we write nothing (the code default is archive_enabled:true).
#
# Resolve the config file to seed into, mirroring the Step 8.5 CONFIG_YAML block:
# prefer rekol.config.yaml; fall back to memory.config.yaml so a root created by an
# older install is still seeded. (Step 8.5 runs much later in the script, so we must
# resolve our own copy here rather than reuse its CONFIG_YAML.)
CONFIG_YAML_SEED="${RESOLVED_HOME}/rekol.config.yaml"
[[ -f "${CONFIG_YAML_SEED}" ]] || CONFIG_YAML_SEED="${RESOLVED_HOME}/memory.config.yaml"
if [[ "$DO_ARCHIVE" == "0" ]]; then
  if ! grep -qs '^archive_enabled:' "${CONFIG_YAML_SEED}" 2>/dev/null; then
    run "printf 'archive_enabled: false\n' >> '${CONFIG_YAML_SEED}'"
  fi
fi
if [[ -n "$ARCHIVE_DIR" ]]; then
  if ! grep -qs '^archive_dir:' "${CONFIG_YAML_SEED}" 2>/dev/null; then
    run "printf 'archive_dir: %s\n' '${ARCHIVE_DIR}' >> '${CONFIG_YAML_SEED}'"
  fi
fi
```

5. **Resolve and record `ARCHIVE_DIR` in the install manifest** so uninstall (Task 8.2) can find it deterministically. Mirror exactly how `INDEX_DIR` is resolved + recorded:
   - Resolve `ARCHIVE_DIR_RESOLVED` right after the `INDEX_DIR` resolution block (after `readonly INDEX_DIR`), with the same env → flag → venv precedence the runtime uses: `$REKOL_ARCHIVE_DIR`, else the `--archive-dir` flag (`$ARCHIVE_DIR`), else ask the freshly installed venv's `resolve_archive_dir` (so the manifest matches what `rekol` resolves at runtime). Skip the venv call in `--dry-run` (the venv may not exist), as the `INDEX_DIR` block does.
```bash
# --- Resolve the durable archive dir for the manifest (mirrors INDEX_DIR) ---
# Precedence matches the runtime resolver: REKOL_ARCHIVE_DIR > --archive-dir flag >
# the venv's resolve_archive_dir default. Recorded so uninstall can find/remove it
# without re-deriving the XDG default. (Note: the archive is machine-level, NOT
# hashed per REKOL_HOME — resolve_archive_dir takes the raw config value only.)
ARCHIVE_DIR_RESOLVED=""
if [[ -n "${REKOL_ARCHIVE_DIR:-}" ]]; then
  ARCHIVE_DIR_RESOLVED="${REKOL_ARCHIVE_DIR}"
elif [[ -n "$ARCHIVE_DIR" ]]; then
  ARCHIVE_DIR_RESOLVED="${ARCHIVE_DIR}"
elif [[ "$DRY_RUN" == "1" ]]; then
  say "DRY-RUN: resolve archive dir via '${TOOLS_HOME}/.venv/bin/python'"
else
  ARCHIVE_DIR_RESOLVED="$(
    "${TOOLS_HOME}/.venv/bin/python" -c \
      'from rekol.config import resolve_archive_dir; print(resolve_archive_dir(None))' \
      2>/dev/null || true
  )"
fi
readonly ARCHIVE_DIR_RESOLVED
```
   - Add the manifest line inside the manifest `{ ... } > "${MANIFEST}"` block (alongside `printf 'INDEX_DIR=%s\n' "${INDEX_DIR}"`):
```bash
    printf 'ARCHIVE_DIR=%s\n' "${ARCHIVE_DIR_RESOLVED}"
```

6. After the existing Step 9.5 session backfill (and not in `--test-mode`), run the archive backfill:
```bash
# Step 9.7 — backfill the durable archive from the freshly built index.
if [[ "${TEST_MODE}" != "1" && "${DO_ARCHIVE}" == "1" ]]; then
  say "building the durable transcript archive (rekol archive --from-index)"
  if "${TOOLS_HOME}/.venv/bin/rekol" archive --from-index 2>&1 | sed 's/^/  /'; then
    log_journal "BACKFILLED archive (archive --from-index)"
  else
    say "archive backfill skipped or failed (non-fatal) — run 'rekol archive --from-index' later"
  fi
fi
```

7. Add the disclosure line to the final "done" block:
```bash
if [[ "${DO_ARCHIVE}" == "1" ]]; then
  say "rekol keeps a local copy of your sessions so your memory survives — it's on your"
  say "disk, never uploaded. Turn it off anytime: set archive_enabled:false in rekol.config.yaml."
fi
```

- [ ] **Step 8.1.4: Run to verify the install bats pass**

Run: `bats tests/test_install.bats`
Expected: PASS (new + existing). The `--test-mode` path must NOT run the archive backfill (no real `~/.claude/projects` walk on CI).

- [ ] **Step 8.1.5: Commit**

```bash
git add install.sh tests/test_install.bats
git commit -m "feat: install.sh archive flags (--no-archive/--archive-dir), disclosure, backfill

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 8.2: uninstall.sh — preserve archive by default, `--purge-archive`

**Files:**
- Modify: `uninstall.sh`
- Test: `tests/test_uninstall.bats`

- [ ] **Step 8.2.1: Write the failing bats tests**

Append to `tests/test_uninstall.bats` (the suite seeds `session_search_enabled: false`; for these tests, also create a fake archive dir and point `REKOL_ARCHIVE_DIR` at it so uninstall can find it):

```bash
@test "uninstall preserves the archive by default" {
    do_full_install
    export REKOL_ARCHIVE_DIR="${TESTROOT}/archive"
    mkdir -p "${REKOL_ARCHIVE_DIR}/projA"
    printf '{"type":"user"}\n' > "${REKOL_ARCHIVE_DIR}/projA/s.jsonl"
    run env -u MEMORY_HOME -u TEST_MODE \
        REKOL_HOME="${REKOLH}" HOME="${SBHOME}" REKOL_ARCHIVE_DIR="${REKOL_ARCHIVE_DIR}" \
        "${COMPONENT_DIR}/uninstall.sh" --yes
    [ "$status" -eq 0 ]
    # --yes must NOT purge the archive (mirrors --yes-keeps-index).
    [ -f "${REKOL_ARCHIVE_DIR}/projA/s.jsonl" ]
}

@test "uninstall --purge-archive removes the archive" {
    do_full_install
    export REKOL_ARCHIVE_DIR="${TESTROOT}/archive"
    mkdir -p "${REKOL_ARCHIVE_DIR}/projA"
    printf '{"type":"user"}\n' > "${REKOL_ARCHIVE_DIR}/projA/s.jsonl"
    run env -u MEMORY_HOME -u TEST_MODE \
        REKOL_HOME="${REKOLH}" HOME="${SBHOME}" REKOL_ARCHIVE_DIR="${REKOL_ARCHIVE_DIR}" \
        "${COMPONENT_DIR}/uninstall.sh" --yes --purge-archive
    [ "$status" -eq 0 ]
    [ ! -d "${REKOL_ARCHIVE_DIR}" ]
}
```

- [ ] **Step 8.2.2: Run to verify they fail**

Run: `bats tests/test_uninstall.bats -f "archive"`
Expected: FAIL (`--purge-archive` unknown arg; archive untouched/unresolved).

- [ ] **Step 8.2.3: Implement the uninstall.sh changes**

In `uninstall.sh`:

1. Add `--purge-archive` to the flags header + `usage()` "What it PRESERVES" / "Flags" sections (mirror `--purge-index` wording; note **markdown memory is never touched** and the archive holds verbatim transcripts so deletion is irreversible in v1 — prompt, don't silently nuke).

2. Add `PURGE_ARCHIVE=0` to the mutable config and handle the flag in the arg-parse `case`:
```bash
    --purge-archive) PURGE_ARCHIVE=1;       shift ;;
```

3. Resolve the archive dir to remove (mirror the INDEX_DIR resolution): prefer `REKOL_ARCHIVE_DIR`, else the manifest's `ARCHIVE_DIR` line (install.sh records it in Task 8.1 item 5 — `printf 'ARCHIVE_DIR=%s\n' "${ARCHIVE_DIR_RESOLVED}"` in the manifest block, resolved via the venv like INDEX_DIR), else ask the venv's `resolve_archive_dir(None)`. Whitelist `ARCHIVE_DIR` in the manifest-reading key list (uninstall reads manifest keys, never sources the file). If unresolved, report a leftover.

4. In Step 6 (the index-purge section), add a parallel gated removal for the archive using the existing `purge_index_dir` helper pattern but with its own gate variable. Add a sibling `purge_archive_dir()` (or generalize `purge_index_dir` to take the gate flag) so:
   - `--purge-archive` → remove
   - `--yes` alone → keep (consistent with `--yes` keeping the index)
   - otherwise → `confirm` prompt "Also delete the durable transcript archive at <dir>? This is irreversible (no export in v1); your markdown memory is kept either way."
```bash
# 6c — the durable transcript archive. Preserved by default; verbatim transcripts
# argue for cleanup, but "it's yours / no export in v1" means deletion is
# irreversible — so prompt (or require --purge-archive), never silently nuke.
if [[ -n "$ARCHIVE_DIR" ]]; then
  if [[ "$PURGE_ARCHIVE" == "1" ]]; then
    say "removing transcript archive ${ARCHIVE_DIR} (--purge-archive)"
    run "rm -rf '${ARCHIVE_DIR}'"
    note_removed "transcript archive ${ARCHIVE_DIR}"
  elif [[ "$ASSUME_YES" == "1" ]]; then
    say "keeping transcript archive ${ARCHIVE_DIR} (--yes does not purge it; pass --purge-archive)"
    note_preserved "transcript archive ${ARCHIVE_DIR}"
  elif confirm "Also delete the durable transcript archive at ${ARCHIVE_DIR}? Irreversible (no export in v1); your markdown is kept either way."; then
    run "rm -rf '${ARCHIVE_DIR}'"
    note_removed "transcript archive ${ARCHIVE_DIR}"
  else
    say "keeping transcript archive ${ARCHIVE_DIR} (pass --purge-archive to remove it)"
    note_preserved "transcript archive ${ARCHIVE_DIR}"
  fi
fi
```

- [ ] **Step 8.2.4: Run to verify the uninstall bats pass**

Run: `bats tests/test_uninstall.bats`
Expected: PASS (new + existing). Confirm the existing "markdown preserved" and "--yes keeps the index" tests still pass.

- [ ] **Step 8.2.5: Commit**

```bash
git add uninstall.sh install.sh tests/test_uninstall.bats
git commit -m "feat: uninstall preserves the archive by default; --purge-archive to remove

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 8.3: README "Install options" section

**Files:**
- Modify: `README.md`

- [ ] **Step 8.3.1: Locate the install section**

Run: `grep -n "install" README.md | head` to find where install usage lives (no test for prose; this is a docs step).

- [ ] **Step 8.3.2: Add an "Install options" section**

Add a subsection listing every install flag (`--dry-run`, `--no-hook`, `--no-skill`, `--no-shellrc`, `--test-mode`, `--tools-home`, `--bin-dir`, `--migrate`, **`--no-archive`**, **`--archive-dir`**, `--help`) and the disclosure line:

> rekol keeps a **local** copy of your sessions so your memory survives even if Claude Code rotates its transcripts — it lives on your disk under `~/.local/share/rekol/archive`, is never uploaded, and is excluded from sync by default. Turn it off with `--no-archive` (or `archive_enabled: false` in `rekol.config.yaml`); relocate it with `--archive-dir`; exclude sensitive projects with `exclude_paths` / `.rekolignore`.

- [ ] **Step 8.3.3: Commit**

```bash
git add README.md
git commit -m "docs: README install options + archive disclosure

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Final verification (whole-gate, before any PR)

- [ ] **Run the full CI gate locally**

```bash
ruff check . && ruff format --check . && mypy src && pytest && \
  bats tests/test_install.bats tests/test_uninstall.bats
```
Expected: all green. This is the exact CI gate (ruff · ruff format · mypy · pytest · bats install).

- [ ] **Manually confirm the headline regression once more**

The single most important assertion in this whole plan lives in `tests/test_archive_integration.py::test_session_survives_live_deletion_then_full_rebuild`. Confirm it passes and genuinely deletes the live `.jsonl` before the rebuild — it is the regression test for the data-loss bug #8 closes.

- [ ] **Do NOT open a PR or merge** until the user asks. The branch `feat/transcript-archiving` holds the work; report completion and wait.

---

## Self-review against the spec (run after drafting; fix inline)

**Spec coverage map (design "Testing" section → task):**
- copy-if-changed matrix (new/unchanged/grown-prefix/shorter→sidecar/diverged→sidecar) → Task 2.2.
- `archive_directory` reconcile (copy new, skip unchanged, rm now-excluded) → Tasks 3.1, 3.2.
- `path_is_excluded` + `.rekolignore` → Task 1.3.
- `backfill_from_index` round-trip through `iter_messages_in_file`, exclude-aware → Task 4.1.
- `resolve_archive_dir` precedence (env > config > default) → Task 1.2.
- **Headline regression** (archive → delete live → `--full` → searchable) → Task 6.1.
- Soft-fail (unwritable archive → ingest from live, exit 0) → Task 6.1 (case folded into Step 6.1.1).
- Exclude integration (excluded never archived; retroactive removal; backfill skips) → Tasks 3.2, 4.1, 6.1 (end-to-end case folded into Step 6.1.1).
- Exclude matches the **real cwd**, not the slug (forward + retroactive + backfill, symmetric) → Tasks 3.1, 3.2, 4.1.
- Backfill auto-once (marker written BEFORE backfill so the notice fires once even on mid-run kill) → Tasks 5.1, 6.1.
- `doctor` archive line + cloud-mount warning → Task 7.1.
- bats `install.sh --no-archive`/`--archive-dir`/disclosure → Task 8.1.
- bats `uninstall.sh` preserves by default / removes with `--purge-archive` → Task 8.2.
- Hermetic (conftest clears `REKOL_HOME`/`MEMORY_HOME`; archive dir → tmp via `REKOL_ARCHIVE_DIR`) → all tests set `REKOL_ARCHIVE_DIR` under `tmp_path`.
- Locking (NO curated `index_write_lock`; `sessions.db` WAL + busy_timeout is the only serialization, flat-file reconcile is idempotent — matches the design's "Locking") → Tasks 5.1, 6.1.

**Type consistency:** `ArchiveStats` / `ArchiveFileResult` / `BackfillStats` / `PruneStats` field names are used identically by `cli_archive.py` (Task 5.1) and `cli_session_index.py` (Task 6.1). `archive_file(..., manifest_key=...)`, `archive_directory(live_root, archive_dir, exclude_patterns)`, `backfill_from_index(store, archive_dir, exclude_patterns)`, `prune(archive_dir, *, clear)`, `path_is_excluded(path, patterns)`, `resolve_archive_dir(config_archive_dir)` signatures match across all call sites in this plan.

**No-placeholder scan:** every code step shows the actual code; every test step shows the actual assertions; every run step states the exact command + expected pass/fail.
