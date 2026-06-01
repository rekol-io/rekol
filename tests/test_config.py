from pathlib import Path

import pytest

from rekol.config import load_config, resolve_memory_home


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


def test_rekol_home_takes_precedence(monkeypatch) -> None:
    # REKOL_HOME is the primary data-dir variable; when both are set it wins.
    monkeypatch.setenv("REKOL_HOME", "/a")
    monkeypatch.setenv("MEMORY_HOME", "/b")
    cfg = load_config()
    assert cfg.memory_home == Path("/a")


def test_memory_home_used_when_rekol_home_unset(monkeypatch) -> None:
    # Back-compat: a shell that only exports MEMORY_HOME (no REKOL_HOME) must
    # still resolve via the fallback path.
    monkeypatch.delenv("REKOL_HOME", raising=False)
    monkeypatch.setenv("MEMORY_HOME", "/b")
    cfg = load_config()
    assert cfg.memory_home == Path("/b")


def test_raises_when_neither_home_set(monkeypatch) -> None:
    monkeypatch.delenv("REKOL_HOME", raising=False)
    monkeypatch.delenv("MEMORY_HOME", raising=False)
    with pytest.raises(RuntimeError) as excinfo:
        load_config()
    message = str(excinfo.value)
    assert "REKOL_HOME" in message
    assert "MEMORY_HOME" in message


def test_resolve_memory_home_precedence(monkeypatch) -> None:
    monkeypatch.setenv("REKOL_HOME", "/a")
    monkeypatch.setenv("MEMORY_HOME", "/b")
    assert resolve_memory_home() == "/a"


def test_resolve_memory_home_falls_back_to_memory_home(monkeypatch) -> None:
    monkeypatch.delenv("REKOL_HOME", raising=False)
    monkeypatch.setenv("MEMORY_HOME", "/b")
    assert resolve_memory_home() == "/b"


def test_resolve_memory_home_returns_none_when_unset(monkeypatch) -> None:
    monkeypatch.delenv("REKOL_HOME", raising=False)
    monkeypatch.delenv("MEMORY_HOME", raising=False)
    assert resolve_memory_home() is None


def test_temporal_defaults_loaded(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("REKOL_HOME", str(tmp_path))
    cfg = load_config()
    assert cfg.temporal_exclude_invalidated is True
    assert cfg.temporal_respect_valid_from is True
    assert cfg.temporal_recency_weight == 0.03
    assert cfg.temporal_recency_halflife_days == 180
    assert cfg.temporal_recency_exempt_layers == ["always", "knowledge"]
    assert cfg.temporal_confirm_interval_days == 180


def test_temporal_overrides_from_yaml(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "rekol.config.yaml").write_text(
        "temporal_recency_weight: 0.1\ntemporal_recency_exempt_layers: [always]\n"
    )
    monkeypatch.setenv("REKOL_HOME", str(tmp_path))
    cfg = load_config()
    assert cfg.temporal_recency_weight == 0.1
    assert cfg.temporal_recency_exempt_layers == ["always"]
