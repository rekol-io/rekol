"""Tests for archive config keys, resolve_archive_dir precedence, and the
shared exclude matcher (the reusable #5 foundation)."""

from __future__ import annotations

from pathlib import Path

from rekol.config import load_config, resolve_archive_dir


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
