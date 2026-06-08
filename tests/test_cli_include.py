"""Tests for the ``rekol include`` CLI primitive (T1↔T8 seam, #59).

``rekol include`` is the thin CLI surface over T8's ``include_scope`` engine that
the assistant-led onboarding (and a human at the terminal) drives to govern which
external knowledge dirs are in indexing scope:

* ``rekol include discover <root>`` — list candidate knowledge dirs under a root,
  ranked by ``.md`` count (the coverage signal), auto-junk-filtered. Read-only.
* ``rekol include show`` — print the currently-persisted include-scope (dirs +
  deny globs). Read-only.
* ``rekol include add <dir>...`` — add directories to ``include_dirs`` + persist.
* ``rekol include remove <dir>...`` — remove directories from ``include_dirs``.
* ``rekol include deny <glob>...`` — add deny globs (exclude-list → deny-list).
* ``rekol include allow <glob>...`` — remove deny globs.

Hermetic: ``REKOL_HOME`` points at a tmp home with a config whose
``claude_projects_dir`` is a controlled tmp dir; no real ``~`` read, no network,
no LLM. Persistence is verified by reloading the config.
"""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner


def _home(tmp_path: Path, monkeypatch, *, include_dirs=None, include_deny=None) -> Path:
    """Create a REKOL home with a config seeding include-scope, return its path."""
    home = tmp_path / "memhome"
    home.mkdir()
    projects = tmp_path / "projects"
    projects.mkdir()
    nl = "\n"
    dirs = include_dirs or []
    deny = include_deny or []
    dirs_yaml = "".join(f"  - {d}{nl}" for d in dirs) or f"  []{nl}"
    deny_yaml = "".join(f"  - {g}{nl}" for g in deny) or f"  []{nl}"
    (home / "rekol.config.yaml").write_text(
        f"embedding_model: test-hashing{nl}"
        f"claude_projects_dir: {projects}{nl}"
        f"include_dirs:{nl}{dirs_yaml}"
        f"include_deny:{nl}{deny_yaml}",
        encoding="utf-8",
    )
    monkeypatch.setenv("REKOL_HOME", str(home))
    return home


# --- discover (read-only) -----------------------------------------------------


def test_discover_ranks_dirs_by_md_count(tmp_path: Path, monkeypatch) -> None:
    """``discover <root>`` lists knowledge dirs ranked by .md count, junk-filtered."""
    from rekol.cli_include import main as include_main

    root = tmp_path / "notes"
    (root / "lots").mkdir(parents=True)
    for i in range(3):
        (root / "lots" / f"n{i}.md").write_text("x\n", encoding="utf-8")
    (root / "few").mkdir()
    (root / "few" / "one.md").write_text("x\n", encoding="utf-8")
    # Junk dir must be filtered out entirely.
    (root / "node_modules").mkdir()
    (root / "node_modules" / "junk.md").write_text("x\n", encoding="utf-8")

    _home(tmp_path, monkeypatch)
    result = CliRunner().invoke(include_main, ["discover", str(root)])
    assert result.exit_code == 0, result.output
    out = result.output
    # The .md count is surfaced and the busier dir ranks first.
    assert "lots" in out and "few" in out
    assert out.index("lots") < out.index("few")
    assert "node_modules" not in out


def test_discover_silent_skip_empty_root(tmp_path: Path, monkeypatch) -> None:
    """An empty/missing root yields no 'Found 0' noise (spec silent-skip)."""
    from rekol.cli_include import main as include_main

    _home(tmp_path, monkeypatch)
    empty = tmp_path / "empty"
    empty.mkdir()
    result = CliRunner().invoke(include_main, ["discover", str(empty)])
    assert result.exit_code == 0, result.output
    # No discovered dirs — a calm "no knowledge files found", never "Found 0 …?".
    assert "0" not in result.output or "no " in result.output.lower()


def test_discover_json_machine_readable(tmp_path: Path, monkeypatch) -> None:
    """``discover --json`` emits a parseable array the skill can consume."""
    from rekol.cli_include import main as include_main

    root = tmp_path / "notes"
    (root / "a").mkdir(parents=True)
    (root / "a" / "x.md").write_text("x\n", encoding="utf-8")
    _home(tmp_path, monkeypatch)
    result = CliRunner().invoke(include_main, ["discover", str(root), "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert isinstance(payload, list)
    assert payload and payload[0]["md_count"] == 1
    assert "path" in payload[0] and "text_count" in payload[0]


# --- show (read-only) ---------------------------------------------------------


def test_show_reports_current_scope(tmp_path: Path, monkeypatch) -> None:
    """``show`` prints the persisted include_dirs + include_deny."""
    from rekol.cli_include import main as include_main

    kb = tmp_path / "kb"
    kb.mkdir()
    _home(tmp_path, monkeypatch, include_dirs=[str(kb)], include_deny=["vendored"])
    result = CliRunner().invoke(include_main, ["show"])
    assert result.exit_code == 0, result.output
    assert str(kb) in result.output
    assert "vendored" in result.output


def test_show_empty_scope_is_calm(tmp_path: Path, monkeypatch) -> None:
    """``show`` with no scope configured is a calm message, not an error."""
    from rekol.cli_include import main as include_main

    _home(tmp_path, monkeypatch)
    result = CliRunner().invoke(include_main, ["show"])
    assert result.exit_code == 0, result.output
    assert "no " in result.output.lower()


# --- add / remove (persisting) ------------------------------------------------


def test_add_persists_include_dir(tmp_path: Path, monkeypatch) -> None:
    """``add <dir>`` persists the dir into include_dirs (reload confirms)."""
    from rekol.cli_include import main as include_main
    from rekol.config import load_config

    kb = tmp_path / "kb"
    kb.mkdir()
    _home(tmp_path, monkeypatch)
    result = CliRunner().invoke(include_main, ["add", str(kb)])
    assert result.exit_code == 0, result.output

    cfg = load_config()
    assert str(kb.resolve()) in [str(p) for p in cfg.resolved_include_dirs()]


def test_add_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    """Adding the same dir twice does not duplicate it."""
    from rekol.cli_include import main as include_main
    from rekol.config import load_config

    kb = tmp_path / "kb"
    kb.mkdir()
    _home(tmp_path, monkeypatch)
    CliRunner().invoke(include_main, ["add", str(kb)])
    CliRunner().invoke(include_main, ["add", str(kb)])
    cfg = load_config()
    resolved = [str(p) for p in cfg.resolved_include_dirs()]
    assert resolved.count(str(kb.resolve())) == 1


def test_add_rejects_nonexistent_dir(tmp_path: Path, monkeypatch) -> None:
    """Adding a path that does not exist is an error (no silent bad scope)."""
    from rekol.cli_include import main as include_main
    from rekol.config import load_config

    _home(tmp_path, monkeypatch)
    missing = tmp_path / "does-not-exist"
    result = CliRunner().invoke(include_main, ["add", str(missing)])
    assert result.exit_code != 0
    cfg = load_config()
    assert not cfg.include_dirs


def test_remove_drops_include_dir(tmp_path: Path, monkeypatch) -> None:
    """``remove <dir>`` drops the dir from include_dirs."""
    from rekol.cli_include import main as include_main
    from rekol.config import load_config

    kb = tmp_path / "kb"
    kb.mkdir()
    _home(tmp_path, monkeypatch, include_dirs=[str(kb)])
    result = CliRunner().invoke(include_main, ["remove", str(kb)])
    assert result.exit_code == 0, result.output
    cfg = load_config()
    assert not cfg.include_dirs


def test_remove_unknown_dir_is_noop_not_error(tmp_path: Path, monkeypatch) -> None:
    """Removing a dir that isn't in scope is a calm no-op, not a crash."""
    from rekol.cli_include import main as include_main

    kb = tmp_path / "kb"
    kb.mkdir()
    _home(tmp_path, monkeypatch, include_dirs=[str(kb)])
    other = tmp_path / "other"
    other.mkdir()
    result = CliRunner().invoke(include_main, ["remove", str(other)])
    assert result.exit_code == 0, result.output


# --- deny / allow (persisting deny-list) --------------------------------------


def test_deny_persists_glob(tmp_path: Path, monkeypatch) -> None:
    """``deny <glob>`` adds to include_deny (the deny-list)."""
    from rekol.cli_include import main as include_main
    from rekol.config import load_config

    kb = tmp_path / "kb"
    kb.mkdir()
    _home(tmp_path, monkeypatch, include_dirs=[str(kb)])
    result = CliRunner().invoke(include_main, ["deny", "old-projects", "vendored"])
    assert result.exit_code == 0, result.output
    cfg = load_config()
    assert "old-projects" in cfg.include_deny
    assert "vendored" in cfg.include_deny


def test_deny_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    """Re-denying the same glob does not duplicate it."""
    from rekol.cli_include import main as include_main
    from rekol.config import load_config

    _home(tmp_path, monkeypatch, include_deny=["vendored"])
    CliRunner().invoke(include_main, ["deny", "vendored"])
    cfg = load_config()
    assert cfg.include_deny.count("vendored") == 1


def test_allow_removes_deny_glob(tmp_path: Path, monkeypatch) -> None:
    """``allow <glob>`` removes a previously-denied glob."""
    from rekol.cli_include import main as include_main
    from rekol.config import load_config

    _home(tmp_path, monkeypatch, include_deny=["vendored", "old-projects"])
    result = CliRunner().invoke(include_main, ["allow", "vendored"])
    assert result.exit_code == 0, result.output
    cfg = load_config()
    assert "vendored" not in cfg.include_deny
    assert "old-projects" in cfg.include_deny


def test_add_preserves_other_config_keys(tmp_path: Path, monkeypatch) -> None:
    """Persisting scope never clobbers unrelated config (round-trips the file)."""
    from rekol.cli_include import main as include_main
    from rekol.config import load_config

    kb = tmp_path / "kb"
    kb.mkdir()
    home = _home(tmp_path, monkeypatch)
    # An unrelated key must survive the persist.
    cfg_path = home / "rekol.config.yaml"
    cfg_path.write_text(
        cfg_path.read_text(encoding="utf-8") + "archive_enabled: false\n",
        encoding="utf-8",
    )
    CliRunner().invoke(include_main, ["add", str(kb)])
    cfg = load_config()
    assert cfg.archive_enabled is False
    assert str(kb.resolve()) in [str(p) for p in cfg.resolved_include_dirs()]
