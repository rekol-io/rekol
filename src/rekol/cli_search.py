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
from datetime import date, datetime
from pathlib import Path

import click

from rekol.config import load_config
from rekol.embeddings import get_embedder
from rekol.ranking import _as_date, _layer_of
from rekol.search_combined import Source, search_all
from rekol.sessions.store import SessionStore, SessionStoreDimMismatchError
from rekol.store import IndexModelMismatchError, IndexStore


def _format_session_timestamp(ts_unix: int) -> str:
    """Format a unix timestamp as YYYY-MM-DD in local time for the session line."""
    try:
        return datetime.fromtimestamp(ts_unix).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return "unknown"


def _review_tag(
    hit: dict,
    *,
    memory_home: Path,
    exempt_layers: list[str],
    interval_days: int,
    today: date,
) -> str:
    """Return ' [review?]' for an overdue durable (exempt-layer) hit, else ''.

    Confirmation age keys off ``last_confirmed`` (#87, fallback ``updated``/
    ``created``) so the tag aligns with ``rekol review``'s overdue logic.
    """
    if _layer_of(hit["file_path"], memory_home) not in set(exempt_layers):
        return ""
    ref = _as_date(hit.get("last_confirmed") or hit.get("updated") or hit.get("created"))
    if ref is None or (today - ref).days > interval_days:
        return " [review?]"
    return ""


def _warn_if_silently_empty(
    result,
    *,
    query_text: str,
    memory_store: IndexStore | None,
    session_store: SessionStore | None,
) -> None:
    """Backstop for issue #18: never let search return a *silent* empty.

    The never-silent invariant: search must not return zero results when the
    index holds rows it should have matched. When a tier was queried, returned
    no hits, yet its underlying store is non-empty and looks stale/inconsistent,
    emit one actionable warning to stderr (not stdout, so ``--json`` output stays
    clean) telling the user to rebuild. A genuinely-empty corpus, or a query that
    truly has no matches, stays quiet — we only warn when data is present but
    unreachable.
    """
    stale_tiers: list[str] = []

    # Sessions: the exact #18 signature is an FTS index whose postings match the
    # query but whose rowids are orphaned, so the JOIN in search drops them all.
    if (
        "sessions" in result.sources_queried
        and not result.session_hits
        and session_store is not None
        and session_store.count_messages() > 0
        and session_store.fts_index_is_stale(query_text)
    ):
        stale_tiers.append("session transcript")

    # Curated memory: cosine search over a non-empty chunks table should always
    # surface *something*. Zero curated hits while chunks exist and none were
    # merely filtered (invalidated / not-yet-valid) means the index is stale or
    # inconsistent rather than legitimately silent.
    if (
        "memory" in result.sources_queried
        and not result.memory_hits
        and result.memory_filtered_count == 0
        and memory_store is not None
        and _curated_chunk_count(memory_store) > 0
    ):
        stale_tiers.append("curated memory")

    if not stale_tiers:
        return
    which = " and ".join(stale_tiers)
    click.echo(
        f"⚠ {which} index has data but returned 0 hits for this query — "
        "the index looks stale or inconsistent. Rebuild it:\n"
        "    rekol index rebuild && rekol session-index --full",
        err=True,
    )


def _curated_chunk_count(memory_store: IndexStore) -> int:
    """Total curated chunk rows, or 0 if the table can't be read.

    A best-effort backstop probe: a malformed/old store that raises here is
    itself a sign of trouble, but we don't want the probe to crash the search
    command, so a read error is treated as "no reachable chunks".
    """
    try:
        row = memory_store.conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()
    except Exception:
        return 0
    return int(row["n"]) if row else 0


def _relative_age(day: date, *, today: date) -> str:
    """Phrase the age of ``day`` relative to ``today`` (e.g. '3 weeks ago')."""
    days = (today - day).days
    if days <= 0:
        return "today"
    for unit, span in (("year", 365), ("month", 30), ("week", 7), ("day", 1)):
        if days >= span:
            value = days // span
            return f"{value} {unit}{'s' if value > 1 else ''} ago"
    return "today"


def _confidence_tag(h: dict) -> str:
    """Build the #87 confidence segment for a memory hit (surface, never enforce).

    Order: a suspected-stale warning (exclude-but-explain — the fact still shows,
    just flagged with its evidence), then confirmation recency. ``last_confirmed``
    is distinct from ``updated`` (an edit is not a confirmation), so ``unconfirmed``
    means "never verified against reality" — the signal that lets the agent hedge.
    Invalidated hits already carry ``[INVALIDATED]`` from the caller, so we skip the
    confirmation noise there.
    """
    if h.get("invalidated_at"):
        return ""
    parts: list[str] = []
    if h.get("suspected_at"):
        reason = f" — {h['suspect_reason']}" if h.get("suspect_reason") else ""
        parts.append(f" ⚠ suspected (since {h['suspected_at']}{reason})")
    if h.get("last_confirmed"):
        parts.append(f" · confirmed {h.get('confirmed_rel') or h['last_confirmed']}")
    else:
        parts.append(" · unconfirmed")
    return "".join(parts)


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
            rel = f" ({h['updated_rel']})" if h.get("updated_rel") else ""
            ts = f" · updated {h['updated']}{rel}" if h.get("updated") else ""
            inv = " [INVALIDATED]" if h.get("invalidated_at") else ""
            lines.append(
                f"{h.get('final_score', h['cosine_score']):.3f}  "
                f"{h['file_path']}{heading}  (L{h['line_start']}-{h['line_end']}){ts}{inv}"
                f"{_confidence_tag(h)}{h.get('review_tag', '')}"
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
            srel = f" ({h['ts_rel']})" if h.get("ts_rel") else ""
            lines.append(f"{h['score']:.3f}  {date_str}{srel} — {cwd} — session {session_id_short}")
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
        memory_store = IndexStore(
            db_path=cfg.index_db_path, dim=embedder.dim, embedding_model=cfg.embedding_model
        )
        memory_store.init_schema()
        if memory_store.needs_schema_migration():
            memory_store.close()
            click.echo(
                "curated index schema is out of date — run `rekol index rebuild`",
                err=True,
            )
            sys.exit(1)
        # Identity guard: searching an index built by a DIFFERENT embedding model
        # returns confidently-wrong nearest neighbours (the #22 emptiness backstop
        # can't catch this — the wrong results aren't empty). Fail loudly instead.
        try:
            memory_store.check_model_identity(cfg.embedding_model, embedder.dim)
        except IndexModelMismatchError as exc:
            memory_store.close()
            click.echo(str(exc), err=True)
            sys.exit(2)
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
        _warn_if_silently_empty(
            result,
            query_text=query_text,
            memory_store=memory_store,
            session_store=session_store,
        )
        review_today = date.today()
        for hit in result.memory_hits:
            hit["review_tag"] = _review_tag(
                hit,
                memory_home=cfg.memory_home,
                exempt_layers=cfg.temporal_recency_exempt_layers,
                interval_days=cfg.temporal_confirm_interval_days,
                today=review_today,
            )
            updated_date = _as_date(hit.get("updated"))
            hit["updated_rel"] = (
                _relative_age(updated_date, today=review_today) if updated_date else ""
            )
            # #87 confidence signal: relative age of the last confirmation (distinct
            # from `updated` — an edit is not a confirmation). Empty when never confirmed.
            confirmed_date = _as_date(hit.get("last_confirmed"))
            hit["confirmed_rel"] = (
                _relative_age(confirmed_date, today=review_today) if confirmed_date else ""
            )
        for hit in result.session_hits:
            try:
                session_date = datetime.fromtimestamp(hit["timestamp_unix"]).date()
                hit["ts_rel"] = _relative_age(session_date, today=review_today)
            except (OverflowError, OSError, ValueError, KeyError):
                hit["ts_rel"] = ""
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
                                created=h.get("created"),
                                updated=h.get("updated"),
                                valid_from=h.get("valid_from"),
                                invalidated_at=h.get("invalidated_at"),
                                last_confirmed=h.get("last_confirmed"),
                                suspected_at=h.get("suspected_at"),
                                suspect_reason=h.get("suspect_reason"),
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
