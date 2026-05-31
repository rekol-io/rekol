"""Combined search across the curated-memory index and the session-transcript index.

Memory hits and session hits are presented in two visually separated tiers
rather than merged into one ranked list. Memory is curated truth; sessions
are raw transcript. Mixing them dilutes signal: a popular phrase in many
session messages can drown the canonical memory file even when memory is
the right answer.

A query that returns zero memory hits but non-trivial session hits is a
``promotion candidate`` — the topic has come up in conversation but lives
nowhere durable. The caller can surface this via memory-capture.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from .embeddings import BaseEmbedder
from .sessions.store import SessionStore
from .store import IndexStore

Source = Literal["memory", "sessions", "all"]


@dataclass
class CombinedSearchResult:
    """Hits from a combined memory and session search for one query."""

    query: str
    memory_hits: list[dict] = field(default_factory=list)
    session_hits: list[dict] = field(default_factory=list)
    sources_queried: list[str] = field(default_factory=list)

    @property
    def is_promotion_candidate(self) -> bool:
        """True when memory was queried, returned nothing, and sessions hit.

        Requires ``"memory" in sources_queried`` — otherwise the empty
        ``memory_hits`` may just mean memory was never asked. Without this
        guard, a caller using ``source="sessions"`` would get spurious
        promotion signals.
        """
        return (
            "memory" in self.sources_queried
            and len(self.memory_hits) == 0
            and len(self.session_hits) > 0
        )


def _merge_session_hits(
    fts_hits: list[dict],
    vec_hits: list[dict],
    top_k: int,
) -> list[dict]:
    """Combine FTS and vector hits, dedupe on (session_id, message_uuid), keep top_k by score.

    When both FTS5 and vec0 return the same message, we keep the higher-score
    copy. Max-score-wins rather than reciprocal-rank fusion: simpler, and since
    the BM25 wrapper already produces positive higher-is-better scores
    comparable to cosine similarities, mixing them directly is reasonable for
    v1. RRF is a deferred follow-up.

    On a score tie, the FTS hit is kept because ``fts_hits + vec_hits`` lists
    FTS first and the ``>`` comparison is strict. If you swap the argument
    order, vec will win ties instead.
    """
    by_key: dict[tuple[str, str], dict] = {}
    for hit in fts_hits + vec_hits:
        key = (hit["session_id"], hit["message_uuid"])
        existing = by_key.get(key)
        if existing is None or hit["score"] > existing["score"]:
            by_key[key] = hit
    merged = sorted(by_key.values(), key=lambda h: -h["score"])
    return merged[:top_k]


def search_all(
    query: str,
    embedder: BaseEmbedder,
    memory_store: IndexStore | None = None,
    session_store: SessionStore | None = None,
    source: Source = "all",
    memory_top_k: int = 5,
    sessions_top_k: int = 5,
) -> CombinedSearchResult:
    """Run the query against one or both stores and return layered results.

    The query is embedded exactly once and the vector is reused across both
    stores. Either store may be ``None`` and will be skipped silently —
    ``source="all"`` with ``session_store=None`` is a graceful no-op for that
    tier, not an error, so callers can disable sessions via config without
    plumbing a separate code path.
    """
    result = CombinedSearchResult(query=query)
    query_vec = embedder.embed(query)

    if source in ("memory", "all") and memory_store is not None:
        result.memory_hits = memory_store.search(query_vec, top_k=memory_top_k)
        result.sources_queried.append("memory")

    if source in ("sessions", "all") and session_store is not None:
        fts_hits = session_store.search_fts(query, top_k=sessions_top_k)
        vec_hits = session_store.search_vec(query_vec, top_k=sessions_top_k)
        result.session_hits = _merge_session_hits(fts_hits, vec_hits, sessions_top_k)
        result.sources_queried.append("sessions")

    return result
