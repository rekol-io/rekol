"""Refuse writes that would destroy real user data with test-built data.

WHY THIS EXISTS: on 2026-08-18 a `rekol index rebuild` running the **test
embedder** replaced a live user's curated index. Search over curated memory
returned nothing for two days and nobody noticed, because the write itself
succeeded — a test-built index is a perfectly valid index, just one whose
vectors mean nothing.

The check that should have caught it could not. :meth:`IndexStore.check_model_identity`
compares the configured model against the one recorded in the index and raises
on a mismatch — but `rebuild` deliberately builds into a temp DB and swaps it
over `index.db` atomically (so a kill mid-rebuild cannot leave an empty index).
Nothing reads the OLD index's identity on that path, so the swap replaces a real
index with a test one and reports success.

**The guard has to live where the destructive act happens, not where the
convenient check already was.** That is the recurring shape of this bug family
in this codebase: an invariant asserted where it is easy to assert rather than
where it can actually be violated.

WHY THE TEST EMBEDDER SPECIFICALLY: `test-hashing` is a deterministic stand-in
with no ML model behind it. Nothing legitimate uses it — which makes it an
unusually reliable signal. If it is about to overwrite an index that a real
model built, something has escaped its sandbox, and that is worth failing
loudly for rather than detecting afterwards.

Deliberately NOT path-based. Both 2026-08 incidents involved isolation that was
genuinely attempted and silently outranked: the sandbox redirected
``REKOL_HOME``/``XDG_CACHE_HOME`` while an inherited ``REKOL_INDEX_DIR`` (higher
precedence, used verbatim) still pointed at the real cache. A guard that asks
"does this path look like a sandbox?" would have been fooled in exactly the same
way. Asking "what built the thing I am about to destroy?" cannot be.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

# The stand-in embedders. `get_embedder` maps these to hashing implementations
# with no model weights, so an index built by one is meaningless outside a test.
TEST_EMBEDDER_NAMES = frozenset({"test-hashing"})

# The escape hatch, for the rare case of deliberately rebuilding a real index
# with a test embedder (fixture regeneration). Deliberately verbose: it must be
# something no one sets by reflex, and something that reads as alarming in a
# shell history.
OVERRIDE_ENV_VAR = "REKOL_ALLOW_TEST_EMBEDDER_TO_OVERWRITE_REAL_INDEX"


class RealIndexClobberError(RuntimeError):
    """Raised when test-built data would replace an index a real model produced."""

    def __init__(self, db_path: Path, stored_model: str, incoming_model: str) -> None:
        self.db_path = db_path
        self.stored_model = stored_model
        self.incoming_model = incoming_model
        super().__init__(
            f"refusing to overwrite a real index with test-built data.\n"
            f"  index:          {db_path}\n"
            f"  built by:       {stored_model!r}\n"
            f"  would rebuild with: {incoming_model!r} (a TEST embedder)\n"
            f"\n"
            f"This almost always means a test or throwaway script escaped its sandbox and\n"
            f"resolved to your real index. Check REKOL_INDEX_DIR — it is the HIGHEST\n"
            f"precedence setting and is used verbatim, so it overrides REKOL_HOME and\n"
            f"XDG_CACHE_HOME redirection.\n"
            f"\n"
            f"If you genuinely mean to do this, set {OVERRIDE_ENV_VAR}=1."
        )


def is_test_embedder(embedding_model: str | None) -> bool:
    """True when ``embedding_model`` names a test stand-in rather than a real model."""
    return embedding_model is not None and embedding_model.lower() in TEST_EMBEDDER_NAMES


def _index_holds_data(db_path: Path) -> bool | None:
    """True if the index contains indexed content; None if that cannot be read.

    The discriminator between the two states that both present as "no recorded
    identity": a freshly-created index the caller is about to populate (empty —
    nothing to destroy) versus a LEGACY index full of a user's real data written
    before identity stamping existed (precious, and exactly what must be refused).

    Without this, refusing on unknown provenance also refuses every ordinary
    first build, because `init_schema()` creates the file before anything stamps
    it — a guard that fires on the healthy path is worse than none, since it
    trains people to set the override reflexively.
    """
    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        # Probe readability FIRST. Without this, a corrupt file reports "no data"
        # rather than "cannot tell": sqlite3.connect is lazy, so the corruption
        # only surfaces on a query, and swallowing that per-table error made an
        # unreadable DB look empty — i.e. safe to replace. Exactly the
        # can't-tell-treated-as-nothing-to-lose mistake this guard exists for.
        try:
            connection.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
        except sqlite3.Error:
            return None  # unreadable/corrupt — provenance and content both unknown

        for table in ("chunks", "files"):
            try:
                row = connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone()
            except sqlite3.Error:
                continue  # table genuinely absent in this schema generation
            if row is not None:
                return True
        return False
    finally:
        connection.close()


def _stored_embedding_model(db_path: Path) -> str | None:
    """The model recorded in an existing index, or None if unknown/absent.

    Returns None when the identity cannot be read for ANY reason — absent file,
    pre-identity schema, missing row, unreadable DB. The caller treats None as
    "provenance unproven" and refuses, because only a test-embedder write ever
    reaches that decision; a real-model repair is unaffected.
    """
    if not db_path.exists():
        return None
    try:
        connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        row = connection.execute(
            "SELECT value FROM metadata WHERE key = 'embedding_model'"
        ).fetchone()
    except sqlite3.Error:
        # No `metadata` table (pre-C4 index) — provenance unknown, so allow.
        return None
    finally:
        connection.close()
    if row is None or row[0] is None:
        return None
    return str(row[0])


def assert_not_clobbering_real_index(db_path: Path, incoming_model: str | None) -> None:
    """Refuse a test-embedder rebuild over an index a real model built.

    Call this immediately before any operation that REPLACES an index wholesale
    (a rebuild-and-swap), where the existing index's recorded identity would
    otherwise never be consulted.

    Args:
        db_path: The index that is about to be replaced.
        incoming_model: The embedding model the new index will be built with.

    Raises:
        RealIndexClobberError: When ``incoming_model`` is a test embedder and the
            existing index records a real one, unless the override env var is set.
    """
    if not is_test_embedder(incoming_model):
        return
    if os.environ.get(OVERRIDE_ENV_VAR, "").strip() not in ("", "0", "false", "no"):
        return

    db_path = Path(db_path)
    if not db_path.exists():
        return  # nothing to destroy

    stored_model = _stored_embedding_model(db_path)
    if stored_model is not None:
        if is_test_embedder(stored_model):
            return  # already test-built — a suite replacing its own sandbox index
        # Explicitly recorded a REAL model. That is a positive statement of
        # provenance and is refused regardless of how much data it currently
        # holds — an empty real index is still not ours to replace with test data.
        raise RealIndexClobberError(db_path, stored_model, str(incoming_model))

    # From here the identity is UNKNOWN, and the two states that look identical
    # are distinguished only by content.
    holds_data = _index_holds_data(db_path)
    if holds_data is False:
        # Exists but empty: a just-created index about to be populated. Nothing
        # to destroy, and refusing here would break every ordinary first build.
        return

    # An existing DB that HOLDS DATA whose provenance cannot be PROVEN test-built
    # is refused (as is one we cannot read at all — `holds_data is None`).
    #
    # This used to fail OPEN here — a pre-identity index, a missing metadata row,
    # or an unreadable file were all treated like "no file at all". The stated
    # reasoning was that a guard which blocks the repair of a damaged index is
    # worse than the bug. That reasoning was on the wrong axis, and external
    # review caught it: this function only ever refuses when the INCOMING model
    # is a test embedder. A legitimate repair uses a REAL model and never reaches
    # this branch, so failing closed here blocks no repair at all — while failing
    # open left the original destructive scenario wide open for exactly the
    # indexes most likely to be old and precious.
    raise RealIndexClobberError(
        db_path, stored_model or "unknown (no recorded identity)", str(incoming_model)
    )
