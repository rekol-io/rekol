"""Config: the additive include-scope keys (include_dirs + include_deny).

These keys are ADDITIVE — default empty, so an existing user's behaviour is
unchanged until they opt in. The persist helper is what makes the scope
"editable on rerun" (the onboarding flow writes the user's picks back).
"""

from __future__ import annotations

from pathlib import Path

import yaml

from rekol.config import load_config, persist_include_scope


def test_include_scope_defaults_empty(tmp_path: Path, monkeypatch) -> None:
    # Additive: with no config, both are empty -> no behaviour change.
    monkeypatch.setenv("REKOL_HOME", str(tmp_path))
    cfg = load_config()
    assert cfg.include_dirs == []
    assert cfg.include_deny == []


def test_include_scope_loaded_from_yaml(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "rekol.config.yaml").write_text(
        "include_dirs:\n  - ~/Dropbox/research\n  - /abs/notes\n"
        "include_deny:\n  - vendored\n  - old-projects\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("REKOL_HOME", str(tmp_path))
    cfg = load_config()
    assert cfg.include_dirs == ["~/Dropbox/research", "/abs/notes"]
    assert cfg.include_deny == ["vendored", "old-projects"]


def test_include_dirs_resolved_expands_user_and_absolutises(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    (tmp_path / "rekol.config.yaml").write_text("include_dirs:\n  - ~/research\n", encoding="utf-8")
    monkeypatch.setenv("REKOL_HOME", str(tmp_path))
    cfg = load_config()
    resolved = cfg.resolved_include_dirs()
    assert resolved == [home / "research"]
    assert all(p.is_absolute() for p in resolved)


def test_persist_include_scope_writes_keys(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("REKOL_HOME", str(tmp_path))
    persist_include_scope(tmp_path, include_dirs=["~/a", "/b"], include_deny=["junk"])
    data = yaml.safe_load((tmp_path / "rekol.config.yaml").read_text(encoding="utf-8"))
    assert data["include_dirs"] == ["~/a", "/b"]
    assert data["include_deny"] == ["junk"]
    # And a reload reflects it (round-trip).
    cfg = load_config()
    assert cfg.include_dirs == ["~/a", "/b"]
    assert cfg.include_deny == ["junk"]


def test_persist_include_scope_preserves_existing_keys(tmp_path: Path, monkeypatch) -> None:
    # Editing scope must not clobber unrelated config (e.g. embedding_model).
    (tmp_path / "rekol.config.yaml").write_text(
        "embedding_model: custom-model\nchunk_max_bytes: 999\n", encoding="utf-8"
    )
    monkeypatch.setenv("REKOL_HOME", str(tmp_path))
    persist_include_scope(tmp_path, include_dirs=["/x"], include_deny=[])
    data = yaml.safe_load((tmp_path / "rekol.config.yaml").read_text(encoding="utf-8"))
    assert data["embedding_model"] == "custom-model"
    assert data["chunk_max_bytes"] == 999
    assert data["include_dirs"] == ["/x"]


def test_persist_include_scope_editable_on_rerun(tmp_path: Path, monkeypatch) -> None:
    # Rerun shows current state, fully editable: a second persist overwrites.
    monkeypatch.setenv("REKOL_HOME", str(tmp_path))
    persist_include_scope(tmp_path, include_dirs=["/a", "/b"], include_deny=["x"])
    persist_include_scope(tmp_path, include_dirs=["/a"], include_deny=["x", "y"])
    cfg = load_config()
    assert cfg.include_dirs == ["/a"]
    assert cfg.include_deny == ["x", "y"]
