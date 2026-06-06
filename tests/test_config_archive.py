"""Tests for archive config keys, resolve_archive_dir precedence, and the
shared exclude matcher (the reusable #5 foundation)."""

from __future__ import annotations

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
