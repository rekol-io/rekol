from pathlib import Path

import pytest

from memory_tools.config import load_config


def test_load_config_from_memory_home(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "memory.config.yaml").write_text(
        "embedding_model: test-hashing\n"
        "always_on_budget_bytes: 8192\n"
        "secret_check_on_capture: false\n"
    )
    monkeypatch.setenv("MEMORY_HOME", str(tmp_path))
    cfg = load_config()
    assert cfg.memory_home == tmp_path
    assert cfg.embedding_model == "test-hashing"
    assert cfg.always_on_budget_bytes == 8192
    assert cfg.secret_check_on_capture is False


def test_load_config_defaults_when_no_yaml(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MEMORY_HOME", str(tmp_path))
    cfg = load_config()
    assert cfg.embedding_model == "BAAI/bge-small-en-v1.5"
    assert cfg.always_on_budget_bytes == 8192
    assert cfg.secret_check_on_capture is True


def test_load_config_raises_when_memory_home_missing(monkeypatch) -> None:
    monkeypatch.delenv("MEMORY_HOME", raising=False)
    with pytest.raises(RuntimeError):
        load_config()


def test_index_db_path_default_inside_memory_home(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MEMORY_HOME", str(tmp_path))
    cfg = load_config()
    assert cfg.index_db_path == tmp_path / ".index" / "index.db"


def test_config_exposes_sessions_db_path(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MEMORY_HOME", str(tmp_path))
    cfg = load_config()
    assert cfg.sessions_db_path == tmp_path / ".index" / "sessions.db"
    assert cfg.claude_projects_dir.name == "projects"
    assert cfg.session_search_enabled is True
