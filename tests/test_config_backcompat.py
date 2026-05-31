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


def test_new_filename_wins_when_both_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REKOL_HOME", str(tmp_path))
    monkeypatch.delenv("MEMORY_HOME", raising=False)
    _write(tmp_path, "rekol.config.yaml", "chunk_max_bytes: 111\n")
    _write(tmp_path, "memory.config.yaml", "chunk_max_bytes: 222\n")
    cfg = load_config()
    assert cfg.chunk_max_bytes == 111
