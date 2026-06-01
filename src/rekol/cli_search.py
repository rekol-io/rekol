"""memory-search CLI: combined semantic + keyword search over memory and sessions.

Two presentation modes:
  - Layered text (default): two sections, FROM MEMORY then FROM SESSIONS,
    so curated truth is visually distinct from raw-transcript recall.
  - JSON (--json): single object with memory and sessions arrays.

Source selection:
  - --source memory     query curated memory only
  - --source sessions   query transcripts only
  - --source all        both (default)

Promotion candidates:
  - --promote-candidates  print a one-line hint when sessions have hits
                          but memory does not, suggesting memory-capture.
"""

from __future__ import annotations

import json as json_mod
import sys
from datetime import UTC, datetime

import click

from rekol.config import load_config
from rekol.embeddings import get_embedder
from rekol.search_combined import Source, search_all
from rekol.sessions.store import SessionStore, SessionStoreDimMismatchError
from rekol.store import IndexStore


def _format_session_timestamp(ts_unix: int) -> str:
    """Format a unix timestamp as YYYY-MM-DD for the text-output session line."""
    try:
        return datetime.fromtimestamp(ts_unix, tz=UTC).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return "unknown"


def _render_text(
    result,
    top_k_memory: int,
    top_k_sessions: int,
    source: str,
    promote_candidates: bool,
) -> str:
    """Render the layered text output. Two tiers, visually separated.

    Memory is curated truth; sessions are raw transcript. The two tiers are
    NOT merged into one ranked list — a popular phrase in many session
    messages can drown the canonical memory file even when memory is the
    right answer.
    """
    lines: list[str] = []
    if source in ("memory", "all"):
        lines.append(f"━━ FROM MEMORY (curated, {len(result.memory_hits)} hits) ━━━━━━━━━━━━━━")
        for h in result.memory_hits:
            heading = f" #{h['heading']}" if h.get("heading") else ""
            ts = f" · updated {h['updated']}" if h.get("updated") else ""
            inv = " [INVALIDATED]" if h.get("invalidated_at") else ""
            lines.append(
                f"{h.get('final_score', h['cosine_score']):.3f}  "
                f"{h['file_path']}{heading}  (L{h['line_start']}-{h['line_end']}){ts}{inv}"
            )
            for snippet_line in h["text"].strip().splitlines()[:3]:
                lines.append(f"    {snippet_line}")
            lines.append("")
    if source in ("sessions", "all"):
        lines.append(f"━━ FROM SESSIONS (top {len(result.session_hits)}) ━━━━━━━━━━━━━━━━━━━━")
        for h in result.session_hits:
            date_str = _format_session_timestamp(h["timestamp_unix"])
            cwd = h.get("cwd") or "?"
            session_id_short = h["session_id"][:8]
            lines.append(f"{h['score']:.3f}  {date_str} — {cwd} — session {session_id_short}")
            lines.append(f"    [{h['role']}] {h['content'][:200]}")
            lines.append("")
    if promote_candidates and result.is_promotion_candidate:
        lines.append(
            "⚑ promotion candidate: 0 memory hits, "
            f"{len(result.session_hits)} session hits — consider memory-capture."
        )
    return "\n".join(lines)


@click.command()
@click.argument("query", nargs=-1, required=True)
@click.option(
    "--top",
    "top_k",
    default=5,
    show_default=True,
    type=int,
    help="Maximum number of results per tier.",
)
@click.option(
    "--source",
    "source",
    type=click.Choice(["memory", "sessions", "all"]),
    default="all",
    show_default=True,
    help="Which layer(s) to query.",
)
@click.option(
    "--promote-candidates",
    is_flag=True,
    help="Annotate when sessions hit but memory does not.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Output results as a single JSON object.",
)
@click.option(
    "--include-invalidated",
    is_flag=True,
    default=False,
    help="Include invalidated memories (tagged; ranked below live hits).",
)
def main(
    query: tuple[str, ...],
    top_k: int,
    source: Source,
    promote_candidates: bool,
    as_json: bool,
    include_invalidated: bool,
) -> None:
    r"""Search memory and conversation transcripts. Layered output by default.

    QUERY is a natural-language phrase. Multiple words are joined with
    spaces before embedding, so quoting is optional:

    \b
        memory-search prometheus url
        memory-search "cassandra backup schedule" --top 10
        memory-search "litellm" --source memory
        memory-search "anything" --promote-candidates
    """
    cfg = load_config()
    embedder = get_embedder(cfg.embedding_model)
    memory_store: IndexStore | None = None
    session_store: SessionStore | None = None
    if source in ("memory", "all"):
        # init_schema() so a missing DB doesn't crash the CLI on first run
        memory_store = IndexStore(db_path=cfg.index_db_path, dim=embedder.dim)
        memory_store.init_schema()
        if memory_store.needs_schema_migration():
            memory_store.close()
            click.echo(
                "curated index schema is out of date — run `rekol index rebuild`",
                err=True,
            )
            sys.exit(1)
    if source in ("sessions", "all") and cfg.session_search_enabled:
        session_store = SessionStore(db_path=cfg.sessions_db_path, dim=embedder.dim)
        session_store.init_schema()
        # The vector tier can only be queried at the index's own width. If the
        # embedding model changed since the index was built, fail with the same
        # actionable message the ingest path uses rather than letting sqlite-vec
        # raise a cryptic width error mid-query.
        try:
            session_store.reconcile_embedding_dim(embedder.dim)
        except SessionStoreDimMismatchError as exc:
            session_store.close()
            click.echo(str(exc), err=True)
            sys.exit(2)
    try:
        query_text = " ".join(query)
        result = search_all(
            query=query_text,
            embedder=embedder,
            memory_store=memory_store,
            session_store=session_store,
            source=source,
            memory_top_k=top_k,
            sessions_top_k=top_k,
            config=cfg,
            include_invalidated=include_invalidated,
        )
        if as_json:
            click.echo(
                json_mod.dumps(
                    dict(
                        query=query_text,
                        memory=[
                            dict(
                                file_path=h["file_path"],
                                heading=h.get("heading"),
                                line_start=h["line_start"],
                                line_end=h["line_end"],
                                cosine_score=h["cosine_score"],
                                final_score=h.get("final_score", h["cosine_score"]),
                                tags=h.get("tags", []),
                                aliases=h.get("aliases", []),
                                snippet=h["text"][:300],
                            )
                            for h in result.memory_hits
                        ],
                        sessions=[
                            dict(
                                session_id=h["session_id"],
                                message_uuid=h["message_uuid"],
                                role=h["role"],
                                cwd=h.get("cwd"),
                                timestamp_iso=h["timestamp_iso"],
                                jsonl_path=h["jsonl_path"],
                                line_number=h["line_number"],
                                score=h["score"],
                                source_kind=h["source_kind"],
                                snippet=h["content"][:300],
                            )
                            for h in result.session_hits
                        ],
                        is_promotion_candidate=result.is_promotion_candidate,
                        sources_queried=result.sources_queried,
                    ),
                    indent=2,
                )
            )
        else:
            click.echo(_render_text(result, top_k, top_k, source, promote_candidates))
    finally:
        if memory_store is not None:
            memory_store.close()
        if session_store is not None:
            session_store.close()


if __name__ == "__main__":
    sys.exit(main())
