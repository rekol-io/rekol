"""memory-propose CLI: surface candidate memories for human review.

Two input modes, both no-LLM and both write a review checklist to
``$REKOL_HOME/pending-review/<timestamp>.md`` (never auto-captured):

  - default (notes): read a markdown file or stdin, find lines that look like
    memorable statements (TODO/Decision/Note/correction/preference markers).
  - ``--from-corpus`` (#40): run instruction-signal queries against the indexed
    SESSION corpus (reusing the FTS5 + vector search infra), collect the top
    candidate transcript messages with provenance, and dedupe against curated
    memory. This is the cold-start recall path: it distils "we always X /
    repos live in Y / we decided Z" out of past conversations.

Each candidate is checked against existing memory via the semantic index so the
operator can decide: capture as new, update an existing memory, or drop it.

Does NOT call any LLM in either mode — heuristics + semantic retrieval only.
The privacy/cost reason LLM-over-transcripts was deferred is sidestepped: the
extractor (T3) is the user's own Claude reading this review file. The output is
a checklist the user acts on manually with ``rekol capture``.
"""

from __future__ import annotations

import datetime as dt
import re
import sys
from pathlib import Path

import click

from rekol.cli_common import guard_curated_schema
from rekol.config import load_config, load_rekolignore_patterns
from rekol.corpus_propose import (
    dedupe_against_memory,
    recall_corpus_candidates,
    render_corpus_proposal,
)
from rekol.embeddings import get_embedder
from rekol.sessions.store import SessionStore, SessionStoreDimMismatchError
from rekol.store import IndexStore

# Common leading whitespace + optional bullet marker, applied uniformly.
_LEAD = r"^\s*(?:[-*]\s+)?"

# Regex for candidate-memory lines.  Each pattern targets a phrasing that
# typically signals a durable fact worth remembering across sessions.
_CANDIDATE_PATTERNS = [
    # Imperative reminders
    re.compile(_LEAD + r"(?:remember|don't forget|note|todo|fyi)[:\s]\s*(.+)$", re.IGNORECASE),
    # Decisions
    re.compile(_LEAD + r"(?:decision|chose|going with)[:\s]\s*(.+)$", re.IGNORECASE),
    # Preferences and corrections
    re.compile(_LEAD + r"(?:prefer|use|don't use|avoid)\s+(.+)$", re.IGNORECASE),
    # Direct user-style "you forgot" / "I told you" corrections
    re.compile(_LEAD + r"(?:you forgot|i told you|i said)\s+(.+)$", re.IGNORECASE),
]


# Threshold above which a candidate is considered already-captured (skip).
DUPLICATE_THRESHOLD = 0.80


def extract_candidates(text: str) -> list[str]:
    """Return distinct candidate-memory lines from ``text``."""
    seen: set[str] = set()
    out: list[str] = []
    for line in text.splitlines():
        for pat in _CANDIDATE_PATTERNS:
            m = pat.match(line)
            if m:
                snippet = m.group(1).strip().rstrip(".")
                if snippet and snippet.lower() not in seen:
                    seen.add(snippet.lower())
                    out.append(snippet)
                break
    return out


def _pending_proposal_path(cfg) -> tuple[Path, str]:
    """Return ``(proposal_path, timestamp)`` under the memory-home review dir.

    Creates ``pending-review/`` if absent. The timestamp is the filename stem and
    is reused in the proposal heading so the two always agree.
    """
    pending_dir = cfg.memory_home / "pending-review"
    pending_dir.mkdir(parents=True, exist_ok=True)
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return pending_dir / f"{ts}.md", ts


def _propose_from_notes(cfg, input_file: str | None, quiet: bool) -> None:
    """Notes-file / stdin path: regex-extract candidate lines, dedupe, write review."""
    text = Path(input_file).read_text(encoding="utf-8") if input_file else sys.stdin.read()

    candidates = extract_candidates(text)
    if not candidates:
        if not quiet:
            click.echo("no candidates found")
        return

    embedder = get_embedder(cfg.embedding_model)
    store = IndexStore(db_path=cfg.index_db_path, dim=embedder.dim)
    store.init_schema()
    guard_curated_schema(store)
    try:
        proposal, ts = _pending_proposal_path(cfg)

        lines: list[str] = [
            f"# Candidate memories from {ts}",
            "",
            "Review each item.  For ones worth keeping, run `rekol capture` "
            "with the suggested layer.  Delete this file when done.",
            "",
        ]

        new_count = 0
        dup_count = 0
        for cand in candidates:
            vec = embedder.embed(cand)
            hits = store.search(vec, top_k=1)
            if hits and hits[0]["score"] >= DUPLICATE_THRESHOLD:
                dup_count += 1
                lines.append(
                    f"- ~~{cand}~~ — already captured "
                    f"({hits[0]['score']:.2f}: `{hits[0]['file_path']}`)"
                )
            else:
                new_count += 1
                lines.append(f"- [ ] {cand}")
        lines.append("")
    finally:
        store.close()

    proposal.write_text("\n".join(lines))

    if not quiet:
        click.echo(f"wrote {proposal} ({new_count} new, {dup_count} already-captured)")


def _propose_from_corpus(cfg, quiet: bool) -> None:
    """Corpus path (#40): recall instruction-signal candidates from the session index.

    No-LLM, recall-oriented. Runs the instruction-signal queries against the
    transcript store (FTS5 + vector), dedupes the hits against curated memory,
    and writes a provenance-annotated review checklist. A fresh install with no
    indexed sessions reports "no candidates" and exits 0 rather than erroring.
    """
    if not cfg.session_search_enabled:
        click.echo(
            "session_search_enabled=false in config; --from-corpus has nothing to "
            "query. Enable it in rekol.config.yaml and run `rekol session-index --full`."
        )
        return

    # Combine config excludes with any per-folder .rekolignore at the projects
    # root, so a sensitive project a user excluded is also excluded from recall
    # (matches cli_session_index.py / cli_archive.py).
    exclude_patterns = list(cfg.exclude_paths) + load_rekolignore_patterns(cfg.claude_projects_dir)

    embedder = get_embedder(cfg.embedding_model)
    # Opens live INSIDE the try so a raise in init_schema/guard_curated_schema
    # (or the session-store open) never leaks an already-opened store. ``finally``
    # closes whatever got opened (None-guarded).
    memory_store: IndexStore | None = None
    session_store: SessionStore | None = None
    try:
        memory_store = IndexStore(db_path=cfg.index_db_path, dim=embedder.dim)
        memory_store.init_schema()
        guard_curated_schema(memory_store)
        session_store = SessionStore(db_path=cfg.sessions_db_path, dim=embedder.dim)
        session_store.init_schema()

        # The vector tier can only be queried at the index's own width; fail with
        # the same actionable message the ingest/search paths use rather than
        # letting sqlite-vec raise a cryptic width error mid-query.
        try:
            session_store.reconcile_embedding_dim(embedder.dim)
        except SessionStoreDimMismatchError as exc:
            click.echo(str(exc), err=True)
            raise SystemExit(2) from exc

        candidates = recall_corpus_candidates(
            session_store, embedder, exclude_patterns=exclude_patterns
        )
        if not candidates:
            if not quiet:
                click.echo(
                    "no candidates found (no matching transcripts indexed — "
                    "run `rekol session-index --full` first?)"
                )
            return

        new_candidates, already_captured = dedupe_against_memory(candidates, memory_store, embedder)
    finally:
        if session_store is not None:
            session_store.close()
        if memory_store is not None:
            memory_store.close()

    proposal, ts = _pending_proposal_path(cfg)
    proposal.write_text(render_corpus_proposal(new_candidates, already_captured, timestamp=ts))
    if not quiet:
        click.echo(
            f"wrote {proposal} ({len(new_candidates)} new, "
            f"{len(already_captured)} already-captured)"
        )


@click.command()
@click.option(
    "--input-file",
    "-i",
    type=click.Path(exists=True, dir_okay=False),
    help="Read notes from a file. If omitted, reads stdin.",
)
@click.option(
    "--from-corpus",
    "from_corpus",
    is_flag=True,
    help="Recall candidates from the indexed SESSION corpus via instruction-signal "
    "queries (no LLM), instead of reading a notes file. Reuses transcript search; "
    "writes a provenance-annotated review checklist. Requires `rekol session-index`.",
)
@click.option("--quiet", is_flag=True, help="Suppress the summary line.")
def main(input_file: str | None, from_corpus: bool, quiet: bool) -> None:
    """Surface candidate memories for review; write a proposal, never auto-capture.

    Output goes to ``$REKOL_HOME/pending-review/<timestamp>.md``.  Each candidate
    is annotated with the most-similar existing memory (if any) so the operator
    can decide whether to capture as new, update an existing memory, or drop it.

    Default mode scans a notes file (or stdin); ``--from-corpus`` recalls from the
    indexed session transcripts instead.
    """
    if from_corpus and input_file:
        raise click.UsageError("--from-corpus reads the session index, not --input-file")
    cfg = load_config()
    if from_corpus:
        _propose_from_corpus(cfg, quiet)
    else:
        _propose_from_notes(cfg, input_file, quiet)


if __name__ == "__main__":
    sys.exit(main())
