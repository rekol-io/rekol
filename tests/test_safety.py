"""The guard that stops test-built data replacing a real index (2026-08-18)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np
import pytest

from rekol.safety import (
    OVERRIDE_ENV_VAR,
    RealIndexClobberError,
    assert_not_clobbering_real_index,
    is_test_embedder,
)

REAL_MODEL = "BAAI/bge-small-en-v1.5"
TEST_MODEL = "test-hashing"


def _make_index(db_path: Path, embedding_model: str | None, *, with_data: bool = True) -> Path:
    """An index carrying (or lacking) a recorded model identity.

    `with_data` matters for the unknown-identity case: an EMPTY unstamped index is
    a freshly-created one about to be populated (nothing to destroy), while a
    POPULATED unstamped index is a legacy real index from before identity
    stamping — precious, and the thing that must be refused. Defaults to
    populated so a fixture never accidentally asserts the weaker case.
    """
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT)")
    connection.execute("CREATE TABLE files (path TEXT PRIMARY KEY, content_hash TEXT)")
    if with_data:
        connection.execute("INSERT INTO files VALUES ('/real/precious.md', 'deadbeef')")
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


def test_REFUSES_when_provenance_is_unknown(tmp_path: Path) -> None:
    """An existing index with no recorded identity must NOT be replaceable by test data.

    This test previously asserted the OPPOSITE and was wrong. The reasoning was
    "a guard that blocks the repair of a damaged index is worse than the bug" —
    correct in general, but on the wrong axis here: this function only ever
    refuses when the INCOMING model is a test embedder. A real-model repair never
    reaches this branch, so failing closed blocks no repair at all.

    Meanwhile failing open left the original destructive scenario wide open for
    a pre-identity (legacy) index — precisely the oldest and least replaceable
    kind. Caught in external review.
    """
    db_path = _make_index(tmp_path / "index.db", None, with_data=True)
    with pytest.raises(RealIndexClobberError):
        assert_not_clobbering_real_index(db_path, TEST_MODEL)


def test_allows_an_empty_unstamped_index(tmp_path: Path) -> None:
    """The counterpart: a just-created index is not a legacy one.

    `init_schema()` creates the file before anything stamps it, so refusing on
    unknown identity alone would fire on every ordinary first build — a guard
    that trips on the healthy path trains people to set the override reflexively,
    which is worse than no guard. Content is what distinguishes the two.
    """
    db_path = _make_index(tmp_path / "index.db", None, with_data=False)
    assert_not_clobbering_real_index(db_path, TEST_MODEL)


def test_REFUSES_when_the_index_is_unreadable(tmp_path: Path) -> None:
    """An unreadable DB is unproven provenance, not "nothing to destroy".

    Also inverted. A corrupt index is still repairable — with a REAL model, which
    never reaches this refusal. What is no longer permitted is a TEST embedder
    silently replacing a file whose contents could not be read.
    """
    db_path = tmp_path / "index.db"
    db_path.write_bytes(b"this is not a sqlite database")
    with pytest.raises(RealIndexClobberError):
        assert_not_clobbering_real_index(db_path, TEST_MODEL)


def test_a_real_model_repairs_an_unreadable_index_unimpeded(tmp_path: Path) -> None:
    """The guard must never be the reason a genuine repair cannot run.

    The counterpart to the two inversions above: this is the scenario the old
    fail-open policy was defending, and it is unaffected, because a real model
    never trips a test-embedder guard.
    """
    db_path = tmp_path / "index.db"
    db_path.write_bytes(b"this is not a sqlite database")
    assert_not_clobbering_real_index(db_path, REAL_MODEL)


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


def test_search_schema_selfheal_cannot_clobber_a_legacy_real_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bypass external review found: `rekol search`, not `rekol index rebuild`.

    The guard was originally called only from the `index rebuild` CLI wrapper.
    But `cli_search` reaches the SAME `os.replace` via
    `ensure_curated_schema_current(auto_rebuild=True)`, and a legacy index has no
    metadata table — so `check_model_identity` passes it through as "unknown"
    rather than raising, and the auto-rebuild proceeded unguarded.

    Concretely: a throwaway process configured with `test-hashing` that inherits
    the real `REKOL_INDEX_DIR`, running an ordinary interactive search against a
    pre-identity index, replaced that index with test vectors. Same outcome #185
    claims to refuse, through a different door.

    This test must reach the destructive path through the SEARCH command. A test
    that calls `assert_not_clobbering_real_index` directly would have missed it
    exactly as the original test suite did.
    """
    import sqlite3 as _sqlite3

    from click.testing import CliRunner

    from rekol.cli_search import main as search_cli

    memory_home = tmp_path / "home"
    (memory_home / "topics").mkdir(parents=True)
    (memory_home / "topics" / "a.md").write_text(
        "---\nname: A\ndescription: d\ntype: topic\n---\n\nbody\n"
    )
    (memory_home / "memory.config.yaml").write_text("embedding_model: test-hashing\n")

    # A LEGACY index: real data, but pre-identity schema (no metadata table), which
    # is what makes `check_model_identity` wave it through.
    real_index_dir = tmp_path / "real-cache"
    real_index_dir.mkdir()
    legacy_db = real_index_dir / "index.db"
    conn = _sqlite3.connect(legacy_db)
    conn.execute("CREATE TABLE files (path TEXT PRIMARY KEY, content_hash TEXT)")
    conn.execute("INSERT INTO files VALUES ('/real/precious.md', 'deadbeef')")
    conn.commit()
    conn.close()
    # NOT a byte comparison: opening the DB legitimately rewrites it
    # (`init_schema` adds tables in place). What must survive is the DATA.

    monkeypatch.setenv("REKOL_HOME", str(memory_home))
    monkeypatch.setenv("REKOL_INDEX_DIR", str(real_index_dir))
    monkeypatch.delenv(OVERRIDE_ENV_VAR, raising=False)

    result = CliRunner().invoke(search_cli, ["anything"])

    conn = _sqlite3.connect(legacy_db)
    rows = conn.execute("SELECT path FROM files").fetchall()
    conn.close()
    assert ("/real/precious.md",) in rows, (
        "an ordinary `rekol search` replaced a legacy real index with test vectors"
    )
    # And it must REFUSE cleanly rather than dying with a traceback: a search
    # crashing on an unhandled guard exception is its own defect.
    assert result.exit_code == 1, result.output
    assert "refusing to overwrite a real index" in result.output
    assert not isinstance(result.exception, RealIndexClobberError), (
        "the guard must be reported, not raised uncaught out of `search`"
    )


# ---------------------------------------------------------------------------
# The chokepoint: every WRITE is guarded, not only the rebuild-and-swap (#216)
# ---------------------------------------------------------------------------
# The guard existed and was strict; it was simply called in one place. Four
# commands -- `capture`, `confirm`, `invalidate`, `flag-suspect` -- plus
# `bootstrap` and `propose`, which the issue did not name, reached a destructive
# write through a different door and were never inspected. These tests go
# through the store's real write methods rather than calling the guard directly,
# because "the invariant is asserted where the destruction happens" is the claim.


def _legacy_populated_index(db_path: Path) -> Path:
    """A real index from before identity stamping: content, no identity row.

    The precious shape. Written with the store's own schema so the tables match
    what production reads, then stripped of its identity so it looks like an
    index built before stamping existed.
    """
    from rekol.store import IndexStore

    store = IndexStore(db_path=db_path, dim=4, embedding_model=REAL_MODEL, use_sqlite_vec=False)
    store.init_schema()
    store.replace_file_and_chunks(
        path="/real/precious.md",
        mtime=1,
        content_hash="deadbeef",
        chunks=[
            {
                "text": "the user's real memory",
                "line_start": 1,
                "line_end": 1,
                "embedding": np.zeros(4, dtype=np.float32),
            }
        ],
    )
    store.conn.execute("DELETE FROM metadata WHERE key IN ('embedding_model','embedding_dim')")
    store.conn.commit()
    store.close()
    return db_path


def _reopen(db_path: Path, model: str | None):
    from rekol.store import IndexStore

    return IndexStore(db_path=db_path, dim=4, embedding_model=model, use_sqlite_vec=False)


def test_an_incremental_write_refuses_a_test_embedder_over_a_real_index(tmp_path: Path) -> None:
    """THE #216 CASE, which no test covered — hence it shipped.

    `Indexer.update()` writes per file through `replace_file_and_chunks`, never
    through the rebuild-and-swap, so the only guarded call was one the four
    bypassing commands never reached.
    """
    db_path = _make_index(tmp_path / "index.db", REAL_MODEL)
    store = _reopen(db_path, TEST_MODEL)
    with pytest.raises(RealIndexClobberError):
        store.replace_file_and_chunks(
            path="/sandbox/note.md", mtime=1, content_hash="cafe", chunks=[]
        )
    store.close()


@pytest.mark.parametrize(
    "write",
    [
        pytest.param(lambda s: s.upsert_file("/sandbox/n.md", 1, "cafe"), id="upsert_file"),
        pytest.param(lambda s: s.delete_file("/real/precious.md"), id="delete_file"),
        pytest.param(lambda s: s.replace_chunks_for_file("/real/precious.md", []), id="chunks"),
        pytest.param(
            lambda s: s.replace_file_and_chunks(
                path="/s/n.md", mtime=1, content_hash="c", chunks=[]
            ),
            id="file_and_chunks",
        ),
        pytest.param(lambda s: s.reset_schema(), id="reset_schema"),
    ],
)
def test_every_write_entry_point_is_guarded(tmp_path: Path, write) -> None:
    """A deletion destroys as surely as an overwrite.

    `update()` removes every indexed path its memory root cannot see, so a
    leaked incremental run over a real index deletes the user's files as well as
    replacing their chunks. Each entry point is checked separately: covering
    four of five would leave the same bug behind one door.
    """
    db_path = _make_index(tmp_path / "index.db", REAL_MODEL)
    store = _reopen(db_path, TEST_MODEL)
    with pytest.raises(RealIndexClobberError):
        write(store)
    store.close()


def test_a_write_with_no_model_over_a_real_index_is_refused(tmp_path: Path) -> None:
    """`None` is not consent (product, 2026-11-08).

    `is_test_embedder(None)` is False, so the original early return let every
    model-less writer past the guard — which is #216 itself: the bypassing
    commands bypassed it precisely by passing no model.
    """
    db_path = _make_index(tmp_path / "index.db", REAL_MODEL)
    store = _reopen(db_path, None)
    with pytest.raises(RealIndexClobberError):
        store.upsert_file("/sandbox/n.md", 1, "cafe")
    store.close()


def test_a_real_model_write_adopts_a_legacy_unstamped_index(tmp_path: Path) -> None:
    """Adoption belongs to a known real model, and to a WRITE (product's row).

    Leon's own machine and every pre-stamping user has this index: content, no
    identity. A real-model write stamps it, which is what finally closes #217's
    "unknown identity is reachable" state — and it happens at the write rather
    than at a read, because a read cannot know what built the data it is
    looking at. Stamping on a read would assert provenance nobody verified.
    """
    db_path = _legacy_populated_index(tmp_path / "index.db")
    store = _reopen(db_path, REAL_MODEL)
    assert store.get_metadata("embedding_model") is None, "fixture is not unstamped"
    store.replace_file_and_chunks(path="/real/new.md", mtime=2, content_hash="f00d", chunks=[])
    assert store.get_metadata("embedding_model") == REAL_MODEL
    assert store.get_file("/real/precious.md") is not None, "the user's row was destroyed"
    store.close()


def test_a_real_model_read_leaves_a_legacy_unstamped_index_exactly_as_it_was(
    tmp_path: Path,
) -> None:
    """The other half of product's row: a read must not error and must not stamp.

    `cli_review`, `cli_search` and `doctor`'s recall probe all construct a store
    and call `init_schema()` on first run, so they now pass a model — and
    `init_schema` must not use that to brand an index whose provenance it cannot
    know. Exit-0-and-unchanged is the whole requirement.
    """
    db_path = _legacy_populated_index(tmp_path / "index.db")
    before = db_path.read_bytes()
    store = _reopen(db_path, REAL_MODEL)
    store.init_schema()  # what every read path does on open
    assert store.get_metadata("embedding_model") is None, "a read stamped the index"
    assert store.get_file("/real/precious.md") is not None
    store.close()
    assert db_path.read_bytes() == before, "a read path modified the index file"


def test_init_schema_refuses_to_brand_a_populated_unstamped_index(tmp_path: Path) -> None:
    """#217's takeover, arriving through a read (product, 2026-11-08).

    With every caller now passing its model, a leaked test-embedder `search`
    over a populated unstamped legacy index could have stamped it `test-hashing`
    — after which the guard reads that stamp as "already a sandbox index" and
    waves every later test write through. The schema generation without a
    `files` table is the one that reaches this, because it looks fresh.
    """
    db_path = _legacy_populated_index(tmp_path / "index.db")
    connection = sqlite3.connect(db_path)
    connection.execute("DROP TABLE files")  # pre-`files` schema: looks fresh
    connection.commit()
    connection.close()

    store = _reopen(db_path, TEST_MODEL)
    store.init_schema()
    assert store.get_metadata("embedding_model") is None, (
        "a test embedder branded a populated index through init_schema"
    )
    store.close()


# ---------------------------------------------------------------------------
# No verdict outlives the file it was about (QA's T12)
# ---------------------------------------------------------------------------
# These began as tests of a per-store memo keyed on the DB file's
# `(st_dev, st_ino, st_size, st_mtime_ns)`. Review then measured that such a key
# cannot work here: the connection runs in WAL mode, so a committed write --
# including another connection stamping a REAL identity -- lives in `-wal` and
# leaves the main file's size and mtime untouched, after which the cached
# "allowed" answer let a test embedder write into a now-real index. The cache is
# gone rather than patched, and these tests now pin the property that replaced
# it: every write asks again.


def _store_schema_index(db_path: Path, model: str | None) -> Path:
    """An index with the store's REAL schema, stamped `model` (or unstamped).

    `_make_index` above writes a deliberately minimal `files` table, which is
    right for guard-level tests but cannot take `upsert_file` — it has no
    `mtime` column. Anything going through the store's own write methods needs
    the store's own schema.
    """
    from rekol.store import IndexStore

    store = IndexStore(db_path=db_path, dim=4, embedding_model=model, use_sqlite_vec=False)
    store.init_schema()
    store.close()
    return db_path


def _passing_store(tmp_path: Path):
    """A store whose first write is allowed, so the memo is populated."""
    db_path = _store_schema_index(tmp_path / "index.db", TEST_MODEL)
    store = _reopen(db_path, TEST_MODEL)
    store.upsert_file("/sandbox/first.md", 1, "aaaa")  # allowed: test over test
    return db_path, store


def test_a_swapped_in_db_is_re_checked(tmp_path: Path) -> None:
    """`rebuild` swaps via os.replace, so a verdict about the old file is void."""
    import os

    db_path, store = _passing_store(tmp_path)
    real = _store_schema_index(tmp_path / "real.db", REAL_MODEL)
    os.replace(real, db_path)  # the user's real index is now at this path
    with pytest.raises(RealIndexClobberError):
        store.upsert_file("/sandbox/second.md", 2, "bbbb")
    store.close()


def test_an_in_place_overwrite_is_re_checked(tmp_path: Path) -> None:
    """The inode survives an in-place overwrite, which is why a key was wrong.

    QA's T12 ran both shapes. A rename changes the inode and an in-place
    overwrite does not, so any cache keyed on file identity had to grow a term
    for each shape — and WAL made even `(size, mtime)` insufficient. Asking
    again on every write is the only answer that needs no enumeration of the
    ways a file can change underneath it.
    """
    db_path, store = _passing_store(tmp_path)
    real_bytes = _store_schema_index(tmp_path / "real.db", REAL_MODEL).read_bytes()
    db_path.write_bytes(real_bytes)  # same inode, different content
    with pytest.raises(RealIndexClobberError):
        store.upsert_file("/sandbox/second.md", 2, "bbbb")
    store.close()


# ---------------------------------------------------------------------------
# The override is an allowlist, not a deny-list (#219)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["False", "FALSE", "off", "Off", "OFF", "No", "NO", "nope", "disabled", "2", "-1", "true-ish"],
)
def test_the_override_refuses_every_unlisted_spelling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """A deny-list read `off` as CONSENT, which is the worst way to be wrong.

    The old test was `not in ("", "0", "false", "no")`, so every spelling here
    switched the guard OFF — including `False`, which is what a Python habit
    types, and `off`, which is what a person means by "no". An allowlist cannot
    fail in that direction: an unrecognised value leaves the guard on.
    """
    db_path = _make_index(tmp_path / "index.db", REAL_MODEL)
    monkeypatch.setenv(OVERRIDE_ENV_VAR, value)
    with pytest.raises(RealIndexClobberError):
        assert_not_clobbering_real_index(db_path, TEST_MODEL)


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "True", "yes", "YES", "on", " 1 "])
def test_the_override_accepts_the_listed_spellings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """The escape hatch still works, case-folded and stripped — or it is useless."""
    db_path = _make_index(tmp_path / "index.db", REAL_MODEL)
    monkeypatch.setenv(OVERRIDE_ENV_VAR, value)
    assert_not_clobbering_real_index(db_path, TEST_MODEL)


# ---------------------------------------------------------------------------
# "cannot tell" is not "empty", and "absent" is not "cannot tell" (#218)
# ---------------------------------------------------------------------------


def test_a_content_table_that_exists_but_cannot_be_read_is_not_empty(tmp_path: Path) -> None:
    """Partial corruption used to read as "nothing to destroy".

    The loop `continue`d on any error as "table absent in this schema
    generation" and then fell through to `return False`. A DB that opens, lists
    its schema, and then fails on its content tables is the shape that matters:
    a real index whose pages are damaged, i.e. the one most worth not replacing.

    A VIEW over a missing table is a deterministic stand-in for that: the table
    is listed in `sqlite_master` and every read of it raises.
    """
    from rekol.safety import _index_holds_data

    db_path = tmp_path / "index.db"
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT)")
    connection.execute("CREATE VIEW files AS SELECT * FROM nonexistent_table")
    connection.commit()
    connection.close()

    assert _index_holds_data(db_path) is None, "an unreadable table read as a known state"
    with pytest.raises(RealIndexClobberError):
        assert_not_clobbering_real_index(db_path, TEST_MODEL)


def test_an_absent_content_table_is_not_a_failed_read(tmp_path: Path) -> None:
    """THE ANTI-WOLF CONTROL, and it caught my first attempt at the fix.

    Keying the tri-state on the exception made an EMPTY unstamped index whose
    schema has no `chunks` table report "cannot tell" and be refused — a healthy
    first build, refused. Fourteen tests failed, including
    `test_allows_an_empty_unstamped_index`. Absent is asked of `sqlite_master`
    now, so it can never be confused with unreadable.
    """
    from rekol.safety import _index_holds_data

    db_path = _make_index(tmp_path / "no-chunks.db", None, with_data=False)
    assert _index_holds_data(db_path) is False
    assert_not_clobbering_real_index(db_path, TEST_MODEL)


# ---------------------------------------------------------------------------
# Every store construction declares its model (#217)
# ---------------------------------------------------------------------------


def test_every_IndexStore_construction_passes_its_embedding_model() -> None:
    """#217's root: an unstamped index is only reachable if a writer is anonymous.

    `capture`, `confirm` and `invalidate` are the three the issue names; a scan
    found `bootstrap` and `propose` (twice) doing the same, plus three paths
    that look like readers and are not — `cli_review`, `cli_search` and
    doctor's recall probe all call `init_schema()` so a missing DB does not
    crash the CLI on first run, which means they create and initialise the file.

    NO ALLOWLIST. An exemption needs a reason, a reason has to be true, and the
    only available reason ("it cannot write") is false for all three.

    THIS CHECK WAS WEAK AND REVIEW BROKE IT (#266). The first version searched
    the call text for the word `embedding_model`, so it accepted
    `embedding_model=None` — the exact anonymous shape it exists to catch — and
    accepted the word appearing in a comment or in another keyword's value. It
    also keyed on the literal `IndexStore(`, so `S = IndexStore; S(...)` slipped
    past, and its `found >= 10` floor would have tolerated four constructions
    vanishing. Each of those is now a case below.
    """
    import re

    source_dir = Path(__file__).resolve().parent.parent / "src" / "rekol"
    # Every name the class is reachable under, so an alias cannot hide a call.
    names = {"IndexStore"}
    for path in sorted(source_dir.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        names.update(re.findall(r"import\s+IndexStore\s+as\s+(\w+)", text))
        names.update(re.findall(r"^\s*(\w+)\s*=\s*IndexStore\s*$", text, re.M))

    anonymous: list[str] = []
    found = 0
    for path in sorted(source_dir.rglob("*.py")):
        raw = path.read_text(encoding="utf-8")
        # Strip comments so a mention in prose cannot satisfy the check.
        text = "\n".join(line.split("#")[0] for line in raw.splitlines())
        for name in sorted(names):
            for match in re.finditer(rf"(?<![\w.]){re.escape(name)}\(", text):
                if text[: match.start()].rstrip().endswith("class"):
                    continue
                found += 1
                depth, i = 1, match.end()
                while i < len(text) and depth:
                    depth += {"(": 1, ")": -1}.get(text[i], 0)
                    i += 1
                call = text[match.end() : i]
                value = re.search(r"embedding_model\s*=\s*([^,)\s]+)", call)
                if value is None or value.group(1) == "None":
                    line = text[: match.start()].count(chr(10)) + 1
                    anonymous.append(f"{path.name}:{line}")

    # EXACT, not a floor: the floor was the weak part. If a construction is
    # added or removed, this number is meant to be updated deliberately —
    # that is the point at which someone looks at whether it declares a model.
    assert found == 14, (
        f"the scan found {found} IndexStore constructions, expected 14. If that is a "
        "deliberate change, update the number; if not, the scan has gone blind."
    )
    assert not anonymous, (
        "these IndexStore constructions do not declare a real embedding model, so they can "
        "write an index with no recorded provenance:\n  " + "\n  ".join(anonymous)
    )


def test_the_refusal_message_does_not_call_an_unknown_writer_a_test_embedder(
    tmp_path: Path,
) -> None:
    """The message has to describe the writer it actually refused (review, #266).

    Refusing a model-less writer is new (#216), and the error still formatted
    its subject as `str(incoming_model)` — so it read
    `would rebuild with: 'None' (a TEST embedder)`: two false statements in one
    line, the first of which sends the reader looking for a model named "None".

    Fixing the message alone did nothing, which is worth remembering: the caller
    passed `str(incoming_model)`, so the exception never saw `None` at all. Only
    running it showed that.
    """
    db_path = _make_index(tmp_path / "index.db", REAL_MODEL)

    with pytest.raises(RealIndexClobberError) as unknown:
        assert_not_clobbering_real_index(db_path, None)
    message = str(unknown.value)
    assert "'None'" not in message, message
    assert "TEST embedder" not in message, message
    assert "did not name its embedding model" in message, message
    assert "unknown provenance" in message, message

    with pytest.raises(RealIndexClobberError) as test_writer:
        assert_not_clobbering_real_index(db_path, TEST_MODEL)
    assert "(a TEST embedder)" in str(test_writer.value)
    assert repr(TEST_MODEL) in str(test_writer.value)


def test_a_write_under_a_switched_model_is_refused_not_silently_restamped(
    tmp_path: Path,
) -> None:
    """A REGRESSION THIS PR WOULD OTHERWISE HAVE INTRODUCED (review, #266).

    Before it, `capture`, `confirm`, `invalidate`, `bootstrap` and `propose`
    passed no model, so they stamped nothing — a user who switched
    `embedding_model` kept the old identity on disk and their next `search`
    failed loudly. Making every writer declare its model means
    `replace_file_and_chunks` keeps the stamp current, so without a check here
    the switched-model `capture` would quietly overwrite the evidence and the
    mixed-model index would then pass every later identity check.

    None of those commands calls `check_model_identity` themselves — only
    `search`, `index update` and `doctor` do — so the check belongs at the write.
    """
    from rekol.store import IndexModelMismatchError

    db_path = _store_schema_index(tmp_path / "index.db", REAL_MODEL)
    other_real = "sentence-transformers/all-MiniLM-L6-v2"
    store = _reopen(db_path, other_real)
    with pytest.raises(IndexModelMismatchError):
        store.upsert_file("/real/n.md", 1, "cafe")
    assert store.get_metadata("embedding_model") == REAL_MODEL, "the stamp was overwritten"
    store.close()


def test_a_populated_pre_files_index_is_not_treated_as_this_stores_own(
    tmp_path: Path,
) -> None:
    """`_created_fresh` was set even when the content check said "populated".

    Review found this one, and it defeated the fix two lines above it: on a
    pre-`files` schema a populated index looks fresh to `is_fresh`, so the stamp
    was correctly withheld — and then the WRITE was let through on the strength
    of "this store created the schema", stamping `test-hashing` after all. The
    flag now means what it says: this store created an EMPTY schema here.
    """
    db_path = _legacy_populated_index(tmp_path / "index.db")
    connection = sqlite3.connect(db_path)
    connection.execute("DROP TABLE files")  # pre-`files` schema: looks fresh
    connection.commit()
    connection.close()

    store = _reopen(db_path, TEST_MODEL)
    store.init_schema()
    with pytest.raises(RealIndexClobberError):
        store.replace_file_and_chunks(path="/sandbox/n.md", mtime=1, content_hash="cafe", chunks=[])
    assert store.get_metadata("embedding_model") is None, "the index was branded after all"
    store.close()


def test_a_model_switch_refusal_names_both_ways_out(tmp_path: Path) -> None:
    """This refusal is reachable by a SUPPORTED action, so the text matters.

    Editing `embedding_model` is something users do. Before this release the
    writers stamped nothing, so a switch surfaced at the next `search`; now it
    surfaces at the next write, which means ordinary people will read this
    message (product, 2026-11-11). It has to name both models and both remedies
    without the reader scrolling back up the paragraph.
    """
    from rekol.store import IndexModelMismatchError

    db_path = _store_schema_index(tmp_path / "index.db", REAL_MODEL)
    other_real = "sentence-transformers/all-MiniLM-L6-v2"
    store = _reopen(db_path, other_real)
    with pytest.raises(IndexModelMismatchError) as caught:
        store.upsert_file("/real/n.md", 1, "cafe")
    store.close()

    message = str(caught.value)
    assert REAL_MODEL in message, message  # what built it
    assert other_real in message, message  # what the config now says
    assert "rekol index rebuild" in message, message  # re-embed
    assert f"set embedding_model back to {REAL_MODEL!r}" in message, message  # or revert
    assert "rekol.config.yaml" in message, message  # and where


def test_a_rebuild_still_succeeds_when_one_real_model_replaces_another(
    tmp_path: Path,
) -> None:
    """The way OUT of a model switch must not be blocked by the new refusal.

    Product's condition: a real model replacing a different real model has to
    keep working, or the remedy the message prints is a dead end. This goes
    through the real `Indexer.rebuild()`, including the swap guard, rather than
    asserting about the store alone.
    """
    import numpy as np  # noqa: F401  (imported for parity with the indexer's deps)

    from rekol.embeddings import HashingEmbedder
    from rekol.indexer import Indexer
    from rekol.store import IndexStore

    memory_root = tmp_path / "mem"
    (memory_root / "topics").mkdir(parents=True)
    (memory_root / "topics" / "t.md").write_text(
        "---\nname: t\ndescription: d\ntype: topic\n"
        "created: 2026-01-01\nupdated: 2026-02-01\n---\nbody\n"
    )
    db_path = tmp_path / "index" / "index.db"

    built_by = IndexStore(db_path=db_path, dim=384, embedding_model="model-A", use_sqlite_vec=False)
    built_by.init_schema()
    Indexer(memory_root=memory_root, store=built_by, embedder=HashingEmbedder(dim=384)).update()
    assert built_by.get_metadata("embedding_model") == "model-A"
    built_by.close()

    switched = IndexStore(db_path=db_path, dim=384, embedding_model="model-B", use_sqlite_vec=False)
    stats = Indexer(
        memory_root=memory_root, store=switched, embedder=HashingEmbedder(dim=384)
    ).rebuild()
    switched.close()
    assert stats.files_indexed == 1, stats

    after = IndexStore(db_path=db_path, dim=384, embedding_model="model-B", use_sqlite_vec=False)
    assert after.get_metadata("embedding_model") == "model-B", "the rebuild did not re-stamp"
    after.close()
