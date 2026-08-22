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


def _stored_embedding_model(db_path: Path) -> str | None:
    """The model recorded in an existing index, or None if unknown/absent.

    Fail-OPEN on every read problem: a missing DB, a pre-identity schema, or an
    unreadable file all return None, which permits the write. This guard exists
    to stop one specific destructive mistake, and it must never become a reason
    a legitimate rebuild cannot run — a false refusal on a corrupt index would
    block the very repair that fixes it.
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

    stored_model = _stored_embedding_model(Path(db_path))
    if stored_model is None or is_test_embedder(stored_model):
        # Nothing there, unknown provenance, or already test-built — all fine to
        # replace. Only real data is protected.
        return

    raise RealIndexClobberError(Path(db_path), stored_model, str(incoming_model))
