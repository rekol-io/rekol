"""T2 (#40): no-LLM semantic-retrieval candidate recall over the session corpus.

Indexing makes transcript history *searchable*, but nothing *distills* the
recurring "we always do X / repos live in Y / we decided Z" statements buried in
that history into reviewable memory candidates. This module is the recall half of
that distillation (the EPIC #46 boundary): it runs a fixed set of
**instruction-signal queries** against the indexed session corpus (reusing the
same FTS5 + vector infra as ``search``), collects the top candidate messages with
full provenance, and dedupes them against curated memory.

Deliberately NO LLM and NO auto-write — this optimises RECALL and corpus-narrowing
(cheap, zero tokens); T3 (#41) adds the LLM precision/classify pass on top. The
output is a human-review checklist written to the local-only ``pending-review/``
cache dir (#57) by the CLI layer (``rekol propose --from-corpus``). Regex-over-notes stays the
default ``propose`` path; this is the corpus path.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from rekol.config import path_is_excluded

if TYPE_CHECKING:
    from rekol.embeddings import BaseEmbedder
    from rekol.sessions.store import SessionStore
    from rekol.store import IndexStore


# Instruction-signal queries: the phrasings that, across real Claude Code
# transcripts, tend to precede a durable instruction/preference/decision worth
# remembering. Each is run independently against both the FTS5 (keyword) and
# vector (semantic) tiers, so a candidate surfaces if EITHER tier matches —
# keyword catches exact idioms ("we always"), vectors catch paraphrases. These
# are recall probes, not precision filters; T3 does precision.
INSTRUCTION_SIGNAL_QUERIES: list[str] = [
    "we always do this",
    "I prefer",
    "the convention is",
    "we decided to",
    "don't do this",
    "never do this",
    "the repo lives in",
    "the project lives at",
    "always use",
    "make sure you",
    "remember to",
    "going forward",
]


# Cosine similarity at/above which a corpus candidate is treated as already
# present in curated memory and routed to the "already captured" bucket rather
# than offered as new. Matches the regex-propose path's DUPLICATE_THRESHOLD so
# both propose modes apply the same dedupe bar.
MEMORY_DUPLICATE_THRESHOLD = 0.80

# Default ceiling on how many candidates the recall pass returns. T2 is
# corpus-NARROWING: a fixed, reviewable shortlist beats dumping every match, and
# it bounds the (cheap) work T3 then spends precision tokens on.
DEFAULT_MAX_CANDIDATES = 50


@dataclass
class CorpusCandidate:
    """One recalled transcript message offered as a memory candidate.

    Carries the full provenance an operator (or T3) needs to verify the source:
    the originating session, the message uuid, and the on-disk transcript file +
    line number it was ingested from.
    """

    content: str
    session_id: str
    message_uuid: str
    role: str
    cwd: str | None
    timestamp_iso: str
    jsonl_path: str
    line_number: int
    score: float
    matched_query: str


def _candidate_from_hit(hit: dict, matched_query: str) -> CorpusCandidate:
    """Build a :class:`CorpusCandidate` from a SessionStore search hit row."""
    return CorpusCandidate(
        content=hit["content"],
        session_id=hit["session_id"],
        message_uuid=hit["message_uuid"],
        role=hit["role"],
        cwd=hit.get("cwd"),
        timestamp_iso=hit["timestamp_iso"],
        jsonl_path=hit["jsonl_path"],
        line_number=int(hit["line_number"]),
        score=float(hit["score"]),
        matched_query=matched_query,
    )


def recall_corpus_candidates(
    session_store: SessionStore,
    embedder: BaseEmbedder,
    *,
    queries: list[str] | None = None,
    per_query_top_k: int = 5,
    max_candidates: int = DEFAULT_MAX_CANDIDATES,
    exclude_patterns: list[str] | None = None,
) -> list[CorpusCandidate]:
    """Recall instruction-signal candidates from the indexed session corpus.

    For each driver query, queries BOTH the FTS5 keyword tier and the vector
    semantic tier of ``session_store`` (reusing the exact search methods the
    ``search`` command uses), then dedupes every hit on its
    ``(session_id, message_uuid)`` identity ACROSS all queries — keeping the
    highest-scoring occurrence and recording which query first surfaced it. The
    result is sorted by score (descending) and truncated to ``max_candidates``.

    The query vector is computed once per driver query and reused for the vector
    tier (the FTS tier takes the raw text). A genuinely-empty corpus yields an
    empty list rather than an error, so the CLI can report "no candidates"
    gracefully on a fresh install.

    ``exclude_patterns`` (config ``exclude_paths`` + any ``.rekolignore``) is the
    privacy chokepoint: a hit whose originating ``cwd`` matches an exclude glob is
    dropped BEFORE dedupe, so a sensitive project a user excluded from
    archiving/indexing is also excluded from recall — even on the
    ``archive_enabled=false`` + ``session_search_enabled=true`` path, which would
    otherwise leak it. Default (``None``/empty) excludes nothing, preserving the
    pure recall behaviour for callers that pass no config.

    Args:
        session_store: The transcript store to recall over (already schema-init'd).
        embedder: Embedder for the vector tier; must match the index's model.
        queries: Override the default :data:`INSTRUCTION_SIGNAL_QUERIES`.
        per_query_top_k: Hits to pull from each tier for each query.
        max_candidates: Corpus-narrowing cap on the returned shortlist.
        exclude_patterns: Glob patterns; a candidate whose ``cwd`` matches any is
            dropped from recall. Built by the CLI as
            ``cfg.exclude_paths + load_rekolignore_patterns(...)``.

    Returns:
        Deduped, score-sorted candidates with provenance, at most
        ``max_candidates`` long, with excluded-cwd candidates removed.
    """
    driver_queries = queries if queries is not None else INSTRUCTION_SIGNAL_QUERIES
    patterns = exclude_patterns or []

    # Keyed on (session_id, message_uuid) so a message matched by several driver
    # queries (or by both tiers) collapses to one candidate at its best score.
    best_by_key: dict[tuple[str, str], CorpusCandidate] = {}
    for query in driver_queries:
        query_vec = embedder.embed(query)
        fts_hits = session_store.search_fts(query, top_k=per_query_top_k)
        vec_hits = session_store.search_vec(query_vec, top_k=per_query_top_k)
        for hit in fts_hits + vec_hits:
            # Privacy filter BEFORE dedupe: drop any hit from an excluded cwd so an
            # excluded project never even competes for a candidate slot.
            cwd = hit.get("cwd")
            if patterns and cwd and path_is_excluded(cwd, patterns):
                continue
            key = (hit["session_id"], hit["message_uuid"])
            candidate = _candidate_from_hit(hit, query)
            existing = best_by_key.get(key)
            # Keep the higher-scoring occurrence; the score it carries is what
            # the final sort ranks on, so a strong vector match should win over a
            # weak keyword match for the same message (and vice versa).
            if existing is None or candidate.score > existing.score:
                best_by_key[key] = candidate

    ranked = sorted(best_by_key.values(), key=lambda c: -c.score)
    return ranked[:max_candidates]


def dedupe_against_memory(
    candidates: list[CorpusCandidate],
    memory_store: IndexStore,
    embedder: BaseEmbedder,
    *,
    threshold: float = MEMORY_DUPLICATE_THRESHOLD,
) -> tuple[list[CorpusCandidate], list[tuple[CorpusCandidate, dict]]]:
    """Split recalled candidates into genuinely-new vs already-in-memory.

    Reuses the same cosine-search dedupe the regex-propose path uses: each
    candidate's content is embedded and matched against curated memory; a top hit
    at/above ``threshold`` means the candidate is effectively already captured.
    Such candidates are still surfaced in the proposal (so the operator sees what
    was filtered and why) but in a separate bucket so the actionable "new" list
    stays focused.

    Args:
        candidates: Recalled corpus candidates.
        memory_store: The curated-memory index (already schema-init'd).
        embedder: Embedder for the candidate text; must match the memory index's model.
        threshold: Cosine similarity above which a candidate counts as captured.

    Returns:
        ``(new_candidates, already_captured)`` where ``already_captured`` pairs
        each captured candidate with its nearest curated-memory hit (for the
        "already captured (0.84: topics/deploy.md)" annotation).
    """
    new_candidates: list[CorpusCandidate] = []
    already_captured: list[tuple[CorpusCandidate, dict]] = []
    for candidate in candidates:
        candidate_vec = embedder.embed(candidate.content)
        hits = memory_store.search(candidate_vec, top_k=1)
        if hits and hits[0]["score"] >= threshold:
            already_captured.append((candidate, hits[0]))
        else:
            new_candidates.append(candidate)
    return new_candidates, already_captured


def render_corpus_proposal(
    new_candidates: list[CorpusCandidate],
    already_captured: list[tuple[CorpusCandidate, dict]],
    *,
    timestamp: str,
) -> str:
    """Render the human-review markdown for a corpus-recall proposal.

    Mirrors the regex-propose checklist shape (an ``- [ ]`` item per new
    candidate) but adds a provenance line per candidate — the source session id,
    transcript file, and line number — so the operator (or T3) can open the exact
    turn and verify it before capturing. Nothing here is auto-written; the banner
    says so explicitly (the EPIC's non-negotiable human-review gate).

    Args:
        new_candidates: Candidates not already present in curated memory.
        already_captured: ``(candidate, memory_hit)`` pairs filtered as dupes.
        timestamp: The proposal timestamp, used in the heading.

    Returns:
        The full markdown body.
    """
    lines: list[str] = [
        f"# Corpus-recall candidates from {timestamp}",
        "",
        "Recalled from the indexed session corpus via instruction-signal queries "
        "(no LLM, no auto-write). Review each item: for ones worth keeping, run "
        "`rekol capture` with the right layer. Check the cited transcript before "
        "trusting a candidate — recall is deliberately broad. Delete this file "
        "when done.",
        "",
        f"## New candidates ({len(new_candidates)})",
        "",
    ]
    if not new_candidates:
        lines.append("_(none — every recalled candidate already exists in memory)_")
        lines.append("")
    for candidate in new_candidates:
        lines.append(f"- [ ] {candidate.content.strip()}")
        lines.append(
            f"      source: session `{candidate.session_id}` · "
            f"`{candidate.jsonl_path}`:{candidate.line_number} · "
            f"[{candidate.role}] {candidate.timestamp_iso} · "
            f"matched {candidate.matched_query!r} (score {candidate.score:.2f})"
        )

    if already_captured:
        lines.append("")
        lines.append(f"## Already in memory ({len(already_captured)}) — skipped")
        lines.append("")
        for candidate, memory_hit in already_captured:
            lines.append(
                f"- ~~{candidate.content.strip()}~~ — already captured "
                f"({memory_hit['score']:.2f}: `{memory_hit['file_path']}`)"
            )
    lines.append("")
    return "\n".join(lines)
