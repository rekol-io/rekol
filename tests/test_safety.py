"""The guard that stops test-built data replacing a real index (2026-08-18)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from rekol.safety import (
    OVERRIDE_ENV_VAR,
    RealIndexClobberError,
    assert_not_clobbering_real_index,
    is_test_embedder,
)

REAL_MODEL = "BAAI/bge-small-en-v1.5"
TEST_MODEL = "test-hashing"


def _make_index(db_path: Path, embedding_model: str | None) -> Path:
    """An index carrying (or lacking) a recorded model identity."""
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT)")
    if embedding_model is not None:
        connection.execute(
            "INSERT INTO metadata (key, value) VALUES ('embedding_model', ?)",
            (embedding_model,),
        )
    connection.commit()
    connection.close()
    return db_path


def test_refuses_test_embedder_over_a_real_index(tmp_path: Path) -> None:
    """THE incident: a test-embedder rebuild aimed at a live user's index."""
    db_path = _make_index(tmp_path / "index.db", REAL_MODEL)

    with pytest.raises(RealIndexClobberError) as raised:
        assert_not_clobbering_real_index(db_path, TEST_MODEL)

    message = str(raised.value)
    # The message must name what would be destroyed and point at the cause —
    # REKOL_INDEX_DIR outranking the sandbox is what actually happened, twice.
    assert REAL_MODEL in message
    assert TEST_MODEL in message
    assert "REKOL_INDEX_DIR" in message


def test_allows_a_real_rebuild_over_a_real_index(tmp_path: Path) -> None:
    """The everyday case must not be impeded — this is a repair, not a clobber."""
    db_path = _make_index(tmp_path / "index.db", REAL_MODEL)
    assert_not_clobbering_real_index(db_path, REAL_MODEL)


def test_allows_a_real_rebuild_over_a_test_index(tmp_path: Path) -> None:
    """Recovering a clobbered index is exactly the repair path — never block it."""
    db_path = _make_index(tmp_path / "index.db", TEST_MODEL)
    assert_not_clobbering_real_index(db_path, REAL_MODEL)


def test_allows_test_over_test(tmp_path: Path) -> None:
    """A test suite rebuilding its own sandbox index is the normal case."""
    db_path = _make_index(tmp_path / "index.db", TEST_MODEL)
    assert_not_clobbering_real_index(db_path, TEST_MODEL)


def test_allows_when_no_index_exists_yet(tmp_path: Path) -> None:
    """A fresh sandbox has nothing to destroy."""
    assert_not_clobbering_real_index(tmp_path / "does-not-exist.db", TEST_MODEL)


def test_allows_when_provenance_is_unknown(tmp_path: Path) -> None:
    """Fail OPEN on an index with no recorded identity (pre-C4 schema).

    A false refusal here would block the rebuild that upgrades a legacy index —
    the guard must never become the reason a repair cannot run.
    """
    db_path = _make_index(tmp_path / "index.db", None)
    assert_not_clobbering_real_index(db_path, TEST_MODEL)


def test_allows_when_the_index_is_unreadable(tmp_path: Path) -> None:
    """A corrupt index must still be rebuildable — fail open, not closed."""
    db_path = tmp_path / "index.db"
    db_path.write_bytes(b"this is not a sqlite database")
    assert_not_clobbering_real_index(db_path, TEST_MODEL)


def test_override_permits_the_clobber(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The escape hatch works when someone genuinely means it."""
    db_path = _make_index(tmp_path / "index.db", REAL_MODEL)
    monkeypatch.setenv(OVERRIDE_ENV_VAR, "1")
    assert_not_clobbering_real_index(db_path, TEST_MODEL)


@pytest.mark.parametrize("falsey", ["", "0", "false", "no"])
def test_override_ignores_falsey_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, falsey: str
) -> None:
    """`REKOL_...=0` must not read as consent — a set-but-off var is still off."""
    db_path = _make_index(tmp_path / "index.db", REAL_MODEL)
    monkeypatch.setenv(OVERRIDE_ENV_VAR, falsey)
    with pytest.raises(RealIndexClobberError):
        assert_not_clobbering_real_index(db_path, TEST_MODEL)


@pytest.mark.parametrize("name", ["test-hashing", "TEST-HASHING", "Test-Hashing"])
def test_test_embedder_detection_is_case_insensitive(name: str) -> None:
    assert is_test_embedder(name)


@pytest.mark.parametrize("name", [REAL_MODEL, "sentence-transformers/all-MiniLM-L6-v2", None])
def test_real_models_are_not_test_embedders(name: str | None) -> None:
    assert not is_test_embedder(name)


def test_cli_rebuild_refuses_the_real_incident(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end reproduction of 2026-08-18, through the CLI that caused it.

    The shape that did the damage: a throwaway script pointed REKOL_HOME at a
    temp dir with `embedding_model: test-hashing`, but an inherited
    REKOL_INDEX_DIR (highest precedence, used verbatim) still resolved to the
    REAL cache. Home moved; the index dir did not.

    This asserts the guard is WIRED, not merely present — the unit tests above
    would all pass with the call site missing.
    """
    from click.testing import CliRunner

    from rekol.cli_index import main as index_cli

    memory_home = tmp_path / "home"
    (memory_home / "topics").mkdir(parents=True)
    (memory_home / "topics" / "a.md").write_text(
        "---\nname: A\ndescription: d\ntype: topic\n---\n\nbody\n"
    )
    (memory_home / "memory.config.yaml").write_text("embedding_model: test-hashing\n")

    # The "real" index the sandbox never intended to touch.
    real_index_dir = tmp_path / "real-cache"
    real_index_dir.mkdir()
    _make_index(real_index_dir / "index.db", REAL_MODEL)

    monkeypatch.setenv("REKOL_HOME", str(memory_home))
    monkeypatch.setenv("REKOL_INDEX_DIR", str(real_index_dir))
    monkeypatch.delenv(OVERRIDE_ENV_VAR, raising=False)

    result = CliRunner().invoke(index_cli, ["rebuild"])

    assert result.exit_code == 1, result.output
    assert "refusing to overwrite a real index" in result.output
    assert REAL_MODEL in result.output

    # And the real index is still the one a real model built.
    connection = sqlite3.connect(real_index_dir / "index.db")
    stored = connection.execute(
        "SELECT value FROM metadata WHERE key = 'embedding_model'"
    ).fetchone()
    connection.close()
    assert stored is not None and stored[0] == REAL_MODEL
