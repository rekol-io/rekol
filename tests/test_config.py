import hashlib
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


def _expected_cache_dir(cache_home: Path, memory_home: Path) -> Path:
    """Mirror config's cache-dir derivation so tests stay in lock-step with it."""
    digest = hashlib.sha256(str(memory_home).encode()).hexdigest()[:16]
    return cache_home / "rekol" / digest


def test_index_db_path_lives_in_cache_not_memory_home(tmp_path: Path, monkeypatch) -> None:
    # SECURITY: the index DB must NOT live under $REKOL_HOME (a possibly-synced
    # folder). It belongs in a local-only cache outside the memory home.
    cache_home = tmp_path / "cache"
    memory_home = tmp_path / "mem"
    memory_home.mkdir()
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_home))
    monkeypatch.setenv("MEMORY_HOME", str(memory_home))
    monkeypatch.delenv("REKOL_INDEX_DIR", raising=False)
    cfg = load_config()
    expected = _expected_cache_dir(cache_home, memory_home)
    assert cfg.index_db_path == expected / "index.db"
    # The path must not be inside the (potentially synced) memory home.
    assert memory_home not in cfg.index_db_path.parents


def test_sessions_db_path_lives_in_cache_not_memory_home(tmp_path: Path, monkeypatch) -> None:
    cache_home = tmp_path / "cache"
    memory_home = tmp_path / "mem"
    memory_home.mkdir()
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_home))
    monkeypatch.setenv("MEMORY_HOME", str(memory_home))
    monkeypatch.delenv("REKOL_INDEX_DIR", raising=False)
    cfg = load_config()
    expected = _expected_cache_dir(cache_home, memory_home)
    assert cfg.sessions_db_path == expected / "sessions.db"
    assert memory_home not in cfg.sessions_db_path.parents
    assert cfg.claude_projects_dir.name == "projects"
    assert cfg.session_search_enabled is True


def test_index_dir_honours_xdg_cache_home(tmp_path: Path, monkeypatch) -> None:
    cache_home = tmp_path / "xdgcache"
    memory_home = tmp_path / "mem"
    memory_home.mkdir()
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_home))
    monkeypatch.setenv("REKOL_HOME", str(memory_home))
    monkeypatch.delenv("REKOL_INDEX_DIR", raising=False)
    cfg = load_config()
    assert cfg.index_dir == _expected_cache_dir(cache_home, memory_home)


def test_index_dir_falls_back_to_dot_cache(tmp_path: Path, monkeypatch) -> None:
    # With no XDG_CACHE_HOME, the cache root defaults to ~/.cache.
    fake_home = tmp_path / "userhome"
    fake_home.mkdir()
    memory_home = tmp_path / "mem"
    memory_home.mkdir()
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.delenv("REKOL_INDEX_DIR", raising=False)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("REKOL_HOME", str(memory_home))
    cfg = load_config()
    assert cfg.index_dir == _expected_cache_dir(fake_home / ".cache", memory_home)


def test_index_dir_hash_is_stable_and_distinct_per_memory_home(tmp_path: Path, monkeypatch) -> None:
    cache_home = tmp_path / "cache"
    home_a = tmp_path / "a"
    home_b = tmp_path / "b"
    home_a.mkdir()
    home_b.mkdir()
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_home))
    monkeypatch.delenv("REKOL_INDEX_DIR", raising=False)

    monkeypatch.setenv("REKOL_HOME", str(home_a))
    a1 = load_config().index_dir
    a2 = load_config().index_dir  # stable across loads
    assert a1 == a2

    monkeypatch.setenv("REKOL_HOME", str(home_b))
    b1 = load_config().index_dir
    # Distinct memory homes must not collide in the cache.
    assert a1 != b1


def test_index_dir_override_via_env(tmp_path: Path, monkeypatch) -> None:
    override = tmp_path / "custom-index-dir"
    memory_home = tmp_path / "mem"
    memory_home.mkdir()
    monkeypatch.setenv("REKOL_HOME", str(memory_home))
    monkeypatch.setenv("REKOL_INDEX_DIR", str(override))
    cfg = load_config()
    assert cfg.index_dir == override
    assert cfg.index_db_path == override / "index.db"
    assert cfg.sessions_db_path == override / "sessions.db"


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
