"""#229 and #230: the sessions identity guard crashed, and then advised `rm`.

Two defects in one path, found independently by the 0.6.0 fan-out review and by
QA on 2026-11-06.

**#229** ``SessionStoreModelMismatchError`` was caught at NONE of
``reconcile_embedder``'s four call sites. Three of them caught its *sibling*
``SessionStoreDimMismatchError``, and an ``except`` written for one does not
catch the other — so a user who merely changed ``embedding_model`` in their
config got a full traceback and exit 1 from their next ``session-index`` or
``search``, with the useful message at the bottom. ``session-index`` is the
SessionEnd hook path, whose stated contract is "never a crash".

**#230** the message then told them to ``rm`` the store. The guard fires
precisely when a test-configured run has resolved someone's REAL ``sessions.db``
through an inherited ``REKOL_INDEX_DIR``, so that advice asked the operator to
finish the destruction the guard had just prevented. On an unstamped store it
also rendered as *restore 'unknown (store predates identity stamping)' in your
config* — advice that cannot be followed, because it is not a model name.

The test that matters here is the first one: four sites is HOW #229 was missed,
so the fix is one site, and this asserts there is still only one.
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path

import numpy as np
import pytest
from click.testing import CliRunner

REAL = "BAAI/bge-small-en-v1.5"
OTHER_REAL = "sentence-transformers/all-MiniLM-L6-v2"  # also 384-dim
TEST = "test-hashing"

SOURCE_DIR = Path(__file__).resolve().parent.parent / "src" / "rekol"

# The ONE module allowed to call reconcile_embedder. Every command goes through
# `guard_session_store_identity`, which lives here.
GUARD_MODULE = "cli_common.py"


def _callers_of(method: str) -> dict[str, list[int]]:
    """Every module under src/rekol with a `<something>.<method>(...)` call."""
    callers: dict[str, list[int]] = {}
    for source_file in sorted(SOURCE_DIR.rglob("*.py")):
        lines: list[int] = []
        for node in ast.walk(ast.parse(source_file.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == method
            ):
                lines.append(node.lineno)
        if lines:
            callers[str(source_file.relative_to(SOURCE_DIR))] = lines
    return callers


def test_reconcile_embedder_is_called_from_exactly_one_place() -> None:
    """THE ANTI-RECURRENCE PIN, and the reason the helper exists at all.

    #229 was not a missing `except`; it was four of them, written separately,
    three catching one sibling error and none catching the other. Fixing the
    four without removing the four would leave the next caller free to add a
    fifth with its own partial handling.

    So the call is centralised and this asserts it stays centralised. A new
    command that opens a sessions store cannot reintroduce the bug without
    failing here first.
    """
    callers = _callers_of("reconcile_embedder")
    # The store's own definition is not a call, so it does not appear here.
    assert set(callers) == {GUARD_MODULE}, (
        f"reconcile_embedder must be called only from {GUARD_MODULE} "
        f"(via guard_session_store_identity); found {callers}"
    )
    # NOT VACUOUS: it has to actually be called somewhere, or this passes while
    # the identity check has been removed entirely.
    assert len(callers[GUARD_MODULE]) == 1, callers


def test_the_dim_error_has_exactly_one_raiser_so_the_guard_covers_it() -> None:
    """Why catching both siblings in one place is sufficient rather than lucky.

    `SessionStoreDimMismatchError` is raised from one method, reached only
    through `reconcile_embedder`. That is what made the old
    `except SessionStoreDimMismatchError` in `cli_session_index` dead code once
    the call moved, and it is why removing that handler lost no coverage. If a
    second raiser ever appears this fails, and the helper's claim to cover both
    errors has to be re-examined rather than assumed.
    """
    store_source = (SOURCE_DIR / "sessions" / "store.py").read_text()
    raisers = [
        node.lineno
        for node in ast.walk(ast.parse(store_source))
        if isinstance(node, ast.Raise)
        and isinstance(node.exc, ast.Call)
        and isinstance(node.exc.func, ast.Name)
        and node.exc.func.id == "SessionStoreDimMismatchError"
    ]
    assert len(raisers) == 1, f"expected one raiser, found {raisers}"


# ---------------------------------------------------------------------------
# #230 — the message
# ---------------------------------------------------------------------------


def test_the_message_says_what_to_check_before_what_to_delete() -> None:
    """The guard fires on a leak, so `rm` must not be the first thing it says.

    Asserted on ORDER, not merely on presence: the old message contained the
    path and the `rm` and nothing else, and a test for "mentions REKOL_INDEX_DIR"
    would pass on a message that mentioned it after the delete command.
    """
    from rekol.sessions.store import SessionStoreModelMismatchError

    message = str(SessionStoreModelMismatchError(Path("/real/sessions.db"), OTHER_REAL, REAL))

    assert "REKOL_INDEX_DIR" in message, message
    assert "rm '/real/sessions.db'" in message, message
    assert message.index("REKOL_INDEX_DIR") < message.index("rm '/real/sessions.db'"), (
        "the destructive remedy comes before the check that makes it unnecessary:\n" + message
    )
    # And it says what REKOL_INDEX_DIR does, since "check it" is not actionable
    # on its own — the same words the curated clobber guard uses.
    assert "verbatim" in message, message
    assert "HIGHEST precedence" in message, message


def test_an_unstamped_store_is_not_offered_for_deletion_at_all() -> None:
    """Unknown provenance is a reason to KEEP a store, not to remove it.

    This branch is reached only when the configured model is a test embedder and
    the store already holds vectors — the leak case almost by construction. The
    old message offered `rm` here too, and rendered the sentinel as a model name
    to restore.
    """
    from rekol.sessions.store import SessionStoreModelMismatchError

    message = str(SessionStoreModelMismatchError(Path("/real/sessions.db"), None, TEST))

    assert "rm '" not in message, (
        "a store of unknown provenance was offered for deletion:\n" + message
    )
    assert "unknown (store predates identity stamping)" not in message, message
    assert "restore 'unknown" not in message, message
    # It still says what is wrong and what to do instead.
    assert "predates model stamping" in message, message
    assert "REKOL_INDEX_DIR" in message, message
    assert "real model" in message, message


def test_the_real_raise_path_reports_unknown_provenance_as_unknown(tmp_path: Path) -> None:
    """The branch has to be CHOSEN by the code, not just available to a caller.

    Added because a revert control caught the two tests above passing while the
    sentinel was back: they construct the error directly with ``None``, so they
    say nothing about which argument ``reconcile_embedder`` passes. With the
    prose sentinel restored, ``stored_model`` is not None, the message takes the
    *stamped* branch, and the user is told to restore a model called
    "unknown (store predates identity stamping)" and to ``rm`` their store —
    exactly #230, with both unit tests green.
    """
    from rekol.sessions.store import SessionStore, SessionStoreModelMismatchError

    store = SessionStore(db_path=tmp_path / "sessions.db", dim=384)
    store.init_schema()
    rowid = store.insert_message(
        {
            "session_id": "s1",
            "message_uuid": "m1",
            "parent_uuid": None,
            "role": "user",
            "content": "legacy content",
            "cwd": "/tmp",
            "timestamp_iso": "2026-01-01T00:00:00Z",
            "timestamp_unix": 1767225600,
            "jsonl_path": "/tmp/x.jsonl",
            "line_number": 1,
        }
    )
    assert rowid is not None
    store.upsert_embedding(rowid, np.zeros(384, dtype=np.float32))
    # An unstamped store that already holds vectors: every pre-#193 store.
    store.conn.execute("DELETE FROM metadata")
    store.conn.commit()

    with pytest.raises(SessionStoreModelMismatchError) as excinfo:
        store.reconcile_embedder(TEST, 384)
    store.close()

    assert excinfo.value.stored_model is None, (
        "unknown provenance was passed as prose, so the message cannot branch on it"
    )
    message = str(excinfo.value)
    assert "rm '" not in message, message
    assert "unknown (store predates identity stamping)" not in message, message


def test_the_width_message_has_the_same_remedy_order() -> None:
    """The sibling error had the identical defect; fixing one would be half a fix."""
    from rekol.sessions.store import SessionStoreDimMismatchError

    message = str(SessionStoreDimMismatchError(Path("/real/sessions.db"), 384, 768))

    assert message.index("REKOL_INDEX_DIR") < message.index("rm '/real/sessions.db'"), message


# ---------------------------------------------------------------------------
# #229 — every command, exercised end to end
# ---------------------------------------------------------------------------


def _home_with_a_real_sessions_store(tmp_path: Path) -> Path:
    """A memory home whose sessions.db is stamped with a REAL model and holds a vector."""
    from rekol.sessions.store import SessionStore

    home = tmp_path / "home"
    (home / "topics").mkdir(parents=True)
    (home / "topics" / "t.md").write_text("---\nname: T\ndescription: d\ntype: topic\n---\n\nt\n")
    (home / "REKOL.md").write_text("# Memory Index\n\n- a pointer\n")
    # The CONFIG names the test embedder, because it is the only one that can be
    # instantiated offline; the STORE is stamped with a real model. That is the
    # mismatch, and it is the leak direction too — a test-configured run reaching
    # a store a real model built, which is what the guard is for.
    (home / "rekol.config.yaml").write_text(
        f"embedding_model: {TEST}\nupdate_check: false\ngit_track: false\n"
        "session_search_enabled: true\n"
    )

    cache = tmp_path / "cache"
    cache.mkdir()
    store = SessionStore(db_path=cache / "sessions.db", dim=384)
    store.init_schema()
    store.reconcile_embedder(OTHER_REAL, 384)
    rowid = store.insert_message(
        {
            "session_id": "s1",
            "message_uuid": "m1",
            "parent_uuid": None,
            "role": "user",
            "content": "a real message",
            "cwd": "/tmp",
            "timestamp_iso": "2026-01-01T00:00:00Z",
            "timestamp_unix": 1767225600,
            "jsonl_path": "/tmp/x.jsonl",
            "line_number": 1,
        }
    )
    assert rowid is not None
    store.upsert_embedding(rowid, np.zeros(384, dtype=np.float32))
    store.close()
    return home


# Every entry point that reconciles the sessions store. The store is stamped
# with a real model and the config names the test embedder, so each command hits
# the identity mismatch as soon as it opens the store.
@pytest.mark.parametrize(
    ("name", "argv"),
    [
        ("search", ["a query", "--source", "sessions"]),
        ("session-index", ["--incremental"]),
        # --from-corpus is the mode that recalls from the sessions store; the
        # default mode reads a notes file and never opens it.
        ("propose", ["--from-corpus"]),
        ("bootstrap", []),
    ],
)
def test_a_model_change_is_a_message_and_exit_2_not_a_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, argv: list[str]
) -> None:
    """All four entry points, because all four were wrong in their own way.

    Three caught the width error only and `session-index` caught nothing at all,
    so a parametrised table is the only honest shape: a single test would have
    passed on whichever site someone happened to pick.

    The assertion is on the ABSENCE of a traceback as well as the exit code —
    exit 1 with the message at the bottom of a stack trace is what this looked
    like, and "the message is present" alone would have passed on that too.
    """
    from rekol.cli import main

    home = _home_with_a_real_sessions_store(tmp_path)
    # session-index refuses before it opens the store if this is missing, so the
    # test would pass on the wrong failure.
    claude_projects = tmp_path / "claude" / "projects"
    claude_projects.mkdir(parents=True)
    for key, value in {
        "REKOL_HOME": str(home),
        "REKOL_INDEX_DIR": str(tmp_path / "cache"),
        "CLAUDE_CONFIG_DIR": str(tmp_path / "claude"),
    }.items():
        monkeypatch.setenv(key, value)

    result = CliRunner().invoke(main, [name, *argv], catch_exceptions=True)
    output = result.output + (str(result.exception) if result.exception else "")

    assert result.exit_code == 2, f"{name} exited {result.exit_code}:\n{output}"
    assert "Traceback" not in output, f"{name} printed a traceback:\n{output}"
    assert not isinstance(result.exception, Exception) or isinstance(
        result.exception, SystemExit
    ), f"{name} raised {result.exception!r} rather than exiting"
    assert "sessions index at" in output, f"{name} said nothing useful:\n{output}"
    assert "REKOL_INDEX_DIR" in output, f"{name} did not name the usual cause:\n{output}"


def test_the_sessions_db_is_not_touched_by_the_refusal(tmp_path: Path, monkeypatch) -> None:
    """The point of refusing: the store the run reached is left exactly as it was.

    Measured on bytes, not on existence — a guard that reports correctly while
    something else has already recreated or truncated the store would pass an
    existence check.
    """
    from rekol.cli import main

    home = _home_with_a_real_sessions_store(tmp_path)
    db = tmp_path / "cache" / "sessions.db"
    before = db.read_bytes()

    for key, value in {
        "REKOL_HOME": str(home),
        "REKOL_INDEX_DIR": str(tmp_path / "cache"),
    }.items():
        monkeypatch.setenv(key, value)

    result = CliRunner().invoke(main, ["session-index", "--incremental"])
    assert result.exit_code == 2, result.output
    assert db.read_bytes() == before, "the refused run modified the store it refused to use"


def test_search_json_still_exits_2_rather_than_printing_empty_results(
    tmp_path: Path, monkeypatch
) -> None:
    """`--json` must not turn a refusal into a valid empty result set.

    The #97 shape: on the agent-facing path an exit-1-with-empty-stdout reads to
    the assistant as "no results" rather than "your index is wrong". Exit 2 and
    nothing parseable on stdout is the behaviour that cannot be misread.
    """
    from rekol.cli import main

    home = _home_with_a_real_sessions_store(tmp_path)
    for key, value in {
        "REKOL_HOME": str(home),
        "REKOL_INDEX_DIR": str(tmp_path / "cache"),
    }.items():
        monkeypatch.setenv(key, value)

    result = CliRunner().invoke(main, ["search", "a query", "--source", "sessions", "--json"])
    assert result.exit_code == 2, result.output
    stdout_lines = [line for line in result.output.splitlines() if line.startswith(("{", "["))]
    for line in stdout_lines:
        parsed = json.loads(line)
        assert parsed not in ([], {}), "a refusal rendered as an empty JSON result"


def test_the_guard_closes_the_store_it_refuses(tmp_path: Path) -> None:
    """Called where one caller had no `finally`, so the connection leaked on exit.

    Not a correctness bug on a process that is about to end, but the helper is
    the only cleanup on that path now, so it has to do it.
    """
    import sqlite3

    from rekol.cli_common import guard_session_store_identity
    from rekol.config import load_config
    from rekol.sessions.store import SessionStore

    home = _home_with_a_real_sessions_store(tmp_path)
    os.environ["REKOL_HOME"] = str(home)
    os.environ["REKOL_INDEX_DIR"] = str(tmp_path / "cache")
    try:
        cfg = load_config()
        store = SessionStore(db_path=tmp_path / "cache" / "sessions.db", dim=384)
        store.init_schema()

        class _Embedder:
            dim = 384

        with pytest.raises(SystemExit) as excinfo:
            guard_session_store_identity(store, cfg, _Embedder())
        assert excinfo.value.code == 2
        with pytest.raises(sqlite3.ProgrammingError):
            store.conn.execute("SELECT 1")
    finally:
        os.environ.pop("REKOL_HOME", None)
        os.environ.pop("REKOL_INDEX_DIR", None)
