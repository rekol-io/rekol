"""#97: ``rekol search`` self-heals a stale curated schema instead of going silently empty.

A curated schema bump (e.g. v4→v5) leaves an existing index too old to query. The
only prior response was a stderr message + ``exit 1``, which on the agent-facing
``rekol search --json`` path leaves **stdout empty** — indistinguishable from a
legitimate "no results". These tests assert the search path auto-rebuilds on a
genuine schema-version mismatch (and ONLY then), while keeping the loud, actionable
fallback where self-heal is unsafe (lock held, rebuild error) and leaving the
model-identity guard loud (rebuilding *that* would discard the user's model choice).

Hermetic: deterministic ``HashingEmbedder`` (``embedding_model: test-hashing``), no torch.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from click.testing import CliRunner

import rekol.cli_common as cli_common
from cache_helpers import cache_dir_for
from rekol.cli_index import main as index_main
from rekol.cli_search import main as search_main
from rekol.store import CURATED_SCHEMA_VERSION


def _seed_memory(root: Path) -> None:
    (root / "topics").mkdir(parents=True, exist_ok=True)
    (root / "topics" / "prometheus.md").write_text(
        "---\n"
        "name: Prometheus\n"
        "description: URL source\n"
        "type: topic\n"
        "tags: [prometheus, urls]\n"
        "---\n\n"
        "# Prometheus\n\nURL lives in the IaC repo.\n"
    )
    (root / "memory.config.yaml").write_text("embedding_model: test-hashing\n")


def _index_db(home: Path) -> Path:
    return cache_dir_for(home) / "index.db"


def _set_user_version(db: Path, version: int) -> None:
    conn = sqlite3.connect(db)
    try:
        conn.execute(f"PRAGMA user_version = {version}")
        conn.commit()
    finally:
        conn.close()


def _get_user_version(db: Path) -> int:
    conn = sqlite3.connect(db)
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def _build(home: Path) -> None:
    assert CliRunner().invoke(index_main, ["rebuild"]).exit_code == 0


def test_search_autorebuilds_on_stale_schema(tmp_path: Path, monkeypatch) -> None:
    """A one-version-behind index: search rebuilds in place and returns real JSON."""
    _seed_memory(tmp_path)
    monkeypatch.setenv("MEMORY_HOME", str(tmp_path))
    _build(tmp_path)
    db = _index_db(tmp_path)
    # Simulate a post-upgrade schema bump: the on-disk index is one version behind.
    _set_user_version(db, CURATED_SCHEMA_VERSION - 1)
    assert _get_user_version(db) == CURATED_SCHEMA_VERSION - 1

    result = CliRunner().invoke(search_main, ["prometheus url", "--top", "3", "--json"])

    # stdout stays VALID JSON with real hits — not an empty "no results".
    # (result.stdout is the pure stdout stream; result.output mixes in stderr.)
    assert result.exit_code == 0, result.output
    data = json.loads(result.stdout)
    assert any("prometheus" in hit["file_path"].lower() for hit in data["memory"])
    # the one-time rebuild notice went to stderr only (keeps --json stdout clean)
    assert "rebuilding automatically" in result.stderr
    assert "rebuilding automatically" not in result.stdout
    # and the on-disk schema is current again
    assert _get_user_version(db) == CURATED_SCHEMA_VERSION


def test_search_does_not_rebuild_when_schema_current(tmp_path: Path, monkeypatch) -> None:
    """Negative: a current/fresh index must never trigger a spurious rebuild."""
    _seed_memory(tmp_path)
    monkeypatch.setenv("MEMORY_HOME", str(tmp_path))
    _build(tmp_path)

    calls = {"n": 0}
    original_rebuild = cli_common.Indexer.rebuild

    def _spy(self):
        calls["n"] += 1
        return original_rebuild(self)

    monkeypatch.setattr(cli_common.Indexer, "rebuild", _spy)

    result = CliRunner().invoke(search_main, ["prometheus url", "--json"])

    assert result.exit_code == 0, result.output
    assert calls["n"] == 0
    assert "rebuilding automatically" not in result.stderr


def test_stale_schema_with_lock_held_falls_back_loud(tmp_path: Path, monkeypatch) -> None:
    """Concurrency: if another index op holds the write lock, don't pile on — loud-fail."""
    _seed_memory(tmp_path)
    monkeypatch.setenv("MEMORY_HOME", str(tmp_path))
    _build(tmp_path)
    db = _index_db(tmp_path)
    _set_user_version(db, CURATED_SCHEMA_VERSION - 1)

    from rekol.index_lock import IndexBusyError

    # Deterministic across platforms: macOS flock semantics for same-process lock
    # contention are implementation-defined, so simulate "another op holds the lock"
    # by making the non-blocking acquire raise IndexBusyError directly.
    def _busy(*args, **kwargs):
        raise IndexBusyError("index write lock is held")

    monkeypatch.setattr(cli_common, "index_write_lock", _busy)

    result = CliRunner().invoke(search_main, ["prometheus url", "--json"])

    assert result.exit_code == 1
    assert "another rekol index operation is in progress" in result.stderr
    # no rebuild happened — schema is still stale
    assert _get_user_version(db) == CURATED_SCHEMA_VERSION - 1


def test_stale_schema_rebuild_oserror_falls_back_loud(tmp_path: Path, monkeypatch) -> None:
    """Read-only / unwritable cache: rebuild raises OSError → loud-fail, live index intact."""
    _seed_memory(tmp_path)
    monkeypatch.setenv("MEMORY_HOME", str(tmp_path))
    _build(tmp_path)
    db = _index_db(tmp_path)
    _set_user_version(db, CURATED_SCHEMA_VERSION - 1)

    def _boom(self):
        raise OSError("read-only file system")

    monkeypatch.setattr(cli_common.Indexer, "rebuild", _boom)

    result = CliRunner().invoke(search_main, ["prometheus url", "--json"])

    assert result.exit_code == 1
    assert "automatic rebuild could not complete" in result.stderr
    # the live index was left untouched (still one version behind) — no worse state
    assert _get_user_version(db) == CURATED_SCHEMA_VERSION - 1


def test_model_identity_mismatch_stays_loud_not_rebuilt(tmp_path: Path, monkeypatch) -> None:
    """A model-identity mismatch (current schema) must stay loud (exit 2), NOT auto-rebuild.

    Auto-rebuilding here would silently re-embed under a different model and discard
    the user's prior model choice — so self-heal is scoped strictly to schema bumps.
    """
    _seed_memory(tmp_path)
    monkeypatch.setenv("MEMORY_HOME", str(tmp_path))
    _build(tmp_path)
    db = _index_db(tmp_path)
    # Rewrite the stored model identity to a DIFFERENT model; schema stays current.
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "UPDATE metadata SET value = ? WHERE key = 'embedding_model'",
            ("some-other-model",),
        )
        conn.commit()
    finally:
        conn.close()

    calls = {"n": 0}
    monkeypatch.setattr(
        cli_common.Indexer, "rebuild", lambda self: calls.__setitem__("n", calls["n"] + 1)
    )

    result = CliRunner().invoke(search_main, ["prometheus url", "--json"])

    assert result.exit_code == 2, result.output
    assert calls["n"] == 0


def test_stale_schema_with_model_mismatch_does_not_autorebuild(tmp_path: Path, monkeypatch) -> None:
    """#97 review (finding #3): a stale schema AND a model-identity mismatch together
    must NOT auto-rebuild — that would silently re-embed under the configured model and
    discard the user's prior model choice. The loud identity guard wins (exit 2)."""
    _seed_memory(tmp_path)
    monkeypatch.setenv("MEMORY_HOME", str(tmp_path))
    _build(tmp_path)
    db = _index_db(tmp_path)
    # BOTH conditions present: schema one version behind AND a different stored model.
    _set_user_version(db, CURATED_SCHEMA_VERSION - 1)
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "UPDATE metadata SET value = ? WHERE key = 'embedding_model'",
            ("some-other-model",),
        )
        conn.commit()
    finally:
        conn.close()

    calls = {"n": 0}
    monkeypatch.setattr(
        cli_common.Indexer, "rebuild", lambda self: calls.__setitem__("n", calls["n"] + 1)
    )

    result = CliRunner().invoke(search_main, ["prometheus url", "--json"])

    assert result.exit_code == 2, result.output  # identity guard, loud — not a silent heal
    assert calls["n"] == 0  # never auto-rebuilt
    assert _get_user_version(db) == CURATED_SCHEMA_VERSION - 1  # index left untouched
