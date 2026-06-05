"""rekol doctor: inspect index health and report degraded state with remedies.

C5 (observability): the derived stores — curated ``index.db`` and the transcript
``sessions.db`` — can drift from the markdown source-of-truth in ways that read
paths only catch lazily (a stale schema, a model swap, an embedding gap, an FTS
desync). ``doctor`` runs the existing health primitives up front and reports each
finding with an *actionable* remedy line, so a user who suspects "memory isn't
working" gets a single, honest diagnosis instead of a silent empty.

Exit code:
  * 0 — every check healthy.
  * 1 — at least one check is degraded (a PROBLEM); the remedy lines say how to
        fix it (``rekol index rebuild`` / ``rekol session-index --full``).

A missing or empty index is NOT a crash — it is reported as a finding (INFO for
"not built yet", PROBLEM only when something is genuinely inconsistent), because
the whole point is to be the tool you reach for when things look broken.
"""

from __future__ import annotations

import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

import click

from rekol.config import Config, load_config
from rekol.embeddings import BaseEmbedder, get_embedder
from rekol.sessions.store import SessionStore
from rekol.store import CURATED_SCHEMA_VERSION, IndexModelMismatchError, IndexStore


class Status(Enum):
    """Severity of a single doctor finding.

    Only :attr:`PROBLEM` makes ``doctor`` exit non-zero; ``OK`` and ``INFO`` are
    informational (an un-built index is INFO, not a failure).
    """

    OK = "ok"
    INFO = "info"
    PROBLEM = "problem"


@dataclass
class Finding:
    """One health check's outcome: a label, status, detail, and optional remedy."""

    label: str
    status: Status
    detail: str
    remedy: str | None = None


@dataclass
class DoctorReport:
    """The full set of findings plus the derived overall health.

    ``is_healthy`` is False iff any finding is a PROBLEM — that, and only that,
    drives the exit-1.
    """

    findings: list[Finding]

    @property
    def is_healthy(self) -> bool:
        """True when no finding is a PROBLEM."""
        return all(f.status is not Status.PROBLEM for f in self.findings)


def _format_last_indexed(indexed_at_unix: int | None) -> str:
    """Render a unix indexed-at timestamp as local time, or 'unknown'."""
    if indexed_at_unix is None:
        return "unknown"
    try:
        return datetime.fromtimestamp(indexed_at_unix).strftime("%Y-%m-%d %H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return "unknown"


def _check_curated_index(cfg: Config, embedder: BaseEmbedder) -> list[Finding]:
    """Inspect the curated ``index.db``: existence, schema, identity, counts, age."""
    findings: list[Finding] = []
    db_path = cfg.index_db_path
    if not db_path.exists():
        # A curated index that was never built is a PROBLEM, not merely INFO:
        # memory search has nothing to return, which is exactly the "memory
        # doesn't work" state a user runs `doctor` to diagnose. Reported, not
        # crashed (we never open a non-existent DB), with the build remedy.
        findings.append(
            Finding(
                label="curated index",
                status=Status.PROBLEM,
                detail=f"not built yet (no {db_path})",
                remedy="rekol index rebuild",
            )
        )
        return findings

    # Opening or querying a corrupt index.db raises sqlite3.DatabaseError ("file
    # is not a database" / "disk image is malformed"). doctor is the tool a user
    # reaches for when memory looks broken, so a corrupt index must surface as a
    # clean PROBLEM with a rebuild remedy — never a traceback. The open itself can
    # raise (the constructor runs pragmas), so it is guarded separately.
    try:
        store = IndexStore(db_path=db_path, dim=embedder.dim, embedding_model=cfg.embedding_model)
    except sqlite3.DatabaseError as exc:
        findings.append(
            Finding(
                label="curated index",
                status=Status.PROBLEM,
                detail=f"index file is unreadable or corrupt ({exc})",
                remedy="rekol index rebuild",
            )
        )
        return findings
    try:
        # Schema version + migration need.
        if store.needs_schema_migration():
            stored = store.get_metadata("schema_version") or "unknown"
            findings.append(
                Finding(
                    label="curated schema",
                    status=Status.PROBLEM,
                    detail=(
                        f"schema is out of date (stored={stored}, current={CURATED_SCHEMA_VERSION})"
                    ),
                    remedy="rekol index rebuild",
                )
            )
        else:
            findings.append(
                Finding(
                    label="curated schema",
                    status=Status.OK,
                    detail=f"version {CURATED_SCHEMA_VERSION} (current)",
                )
            )

        # Model identity: a mismatch returns confidently-wrong results (C4).
        try:
            store.check_model_identity(cfg.embedding_model, embedder.dim)
            stored_model = store.get_metadata("embedding_model")
            if stored_model is None:
                findings.append(
                    Finding(
                        label="model identity",
                        status=Status.INFO,
                        detail="no embedding model recorded (pre-identity index)",
                        remedy="rekol index rebuild",
                    )
                )
            else:
                findings.append(
                    Finding(
                        label="model identity",
                        status=Status.OK,
                        detail=f"built by {stored_model!r} ({embedder.dim}-dim), matches config",
                    )
                )
        except IndexModelMismatchError as exc:
            findings.append(
                Finding(
                    label="model identity",
                    status=Status.PROBLEM,
                    detail=(
                        f"built by {exc.stored_model!r} ({exc.stored_dim}-dim) but config "
                        f"wants {exc.wanted_model!r} ({exc.wanted_dim}-dim)"
                    ),
                    remedy="rekol index rebuild  (or restore the previous embedding_model)",
                )
            )

        # Chunk-count vs file-count: every indexed file must contribute chunks.
        file_count = int(store.conn.execute("SELECT COUNT(*) AS n FROM files").fetchone()["n"])
        chunk_count = int(store.conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()["n"])
        files_without_chunks = int(
            store.conn.execute(
                "SELECT COUNT(*) AS n FROM files f "
                "WHERE NOT EXISTS (SELECT 1 FROM chunks c WHERE c.file_path = f.path)"
            ).fetchone()["n"]
        )
        if file_count == 0:
            findings.append(
                Finding(
                    label="curated content",
                    status=Status.PROBLEM,
                    detail="index is empty (0 files) — nothing to search",
                    remedy="rekol index rebuild",
                )
            )
        elif files_without_chunks > 0:
            findings.append(
                Finding(
                    label="curated content",
                    status=Status.PROBLEM,
                    detail=(
                        f"{files_without_chunks} of {file_count} indexed files have NO chunks "
                        f"(total {chunk_count} chunks) — search cannot reach them"
                    ),
                    remedy="rekol index rebuild",
                )
            )
        else:
            findings.append(
                Finding(
                    label="curated content",
                    status=Status.OK,
                    detail=f"{file_count} files, {chunk_count} chunks (all files have chunks)",
                )
            )

        # Last-build time (best available signal: newest files.indexed_at).
        row = store.conn.execute("SELECT MAX(indexed_at) AS m FROM files").fetchone()
        last_indexed = int(row["m"]) if row and row["m"] is not None else None
        findings.append(
            Finding(
                label="last indexed",
                status=Status.INFO,
                detail=_format_last_indexed(last_indexed),
            )
        )
    except sqlite3.DatabaseError as exc:
        # Corruption surfaced mid-inspection (a query hit a malformed page).
        findings.append(
            Finding(
                label="curated index",
                status=Status.PROBLEM,
                detail=f"index file is corrupt ({exc})",
                remedy="rekol index rebuild",
            )
        )
    finally:
        store.close()
    return findings


def _check_session_index(cfg: Config, embedder: BaseEmbedder) -> list[Finding]:
    """Inspect ``sessions.db``: embedding coverage and FTS sync."""
    findings: list[Finding] = []
    if not cfg.session_search_enabled:
        findings.append(
            Finding(
                label="session index",
                status=Status.INFO,
                detail="session_search_enabled=false in config (transcripts not searched)",
            )
        )
        return findings

    db_path = cfg.sessions_db_path
    if not db_path.exists():
        findings.append(
            Finding(
                label="session index",
                status=Status.INFO,
                detail=f"not built yet (no {db_path})",
                remedy="rekol session-index --full",
            )
        )
        return findings

    try:
        store = SessionStore(db_path=db_path, dim=embedder.dim)
    except sqlite3.DatabaseError as exc:
        findings.append(
            Finding(
                label="session index",
                status=Status.PROBLEM,
                detail=f"sessions DB is unreadable or corrupt ({exc})",
                remedy="rekol session-index --full",
            )
        )
        return findings
    try:
        store.init_schema()
        message_count = store.count_messages()
        if message_count == 0:
            findings.append(
                Finding(
                    label="session index",
                    status=Status.INFO,
                    detail="no transcript messages ingested yet",
                    remedy="rekol session-index --full",
                )
            )
            return findings

        # Embedding coverage: count_embeddings == count_messages iff every message
        # is embedded (embeddings are a subset of message rowids).
        embedding_count = store.count_embeddings()
        if embedding_count < message_count:
            findings.append(
                Finding(
                    label="session embeddings",
                    status=Status.PROBLEM,
                    detail=(
                        f"{embedding_count} of {message_count} messages embedded "
                        f"({message_count - embedding_count} missing) — semantic search is partial"
                    ),
                    remedy="rekol session-index --full",
                )
            )
        else:
            findings.append(
                Finding(
                    label="session embeddings",
                    status=Status.OK,
                    detail=f"{embedding_count}/{message_count} messages embedded",
                )
            )

        # FTS desync: orphaned postings (the #18 silent-empty cause) and/or
        # messages the inverted index never indexed.
        orphaned, unindexed = store.fts_consistency()
        if orphaned > 0 or unindexed > 0:
            findings.append(
                Finding(
                    label="session FTS",
                    status=Status.PROBLEM,
                    detail=(
                        f"keyword index out of sync: {orphaned} orphaned postings, "
                        f"{unindexed} unindexed messages — keyword search may return 0"
                    ),
                    remedy="rekol session-index --full",
                )
            )
        else:
            findings.append(
                Finding(
                    label="session FTS",
                    status=Status.OK,
                    detail=f"keyword index in sync with {message_count} messages",
                )
            )
    except sqlite3.DatabaseError as exc:
        findings.append(
            Finding(
                label="session index",
                status=Status.PROBLEM,
                detail=f"sessions DB is corrupt ({exc})",
                remedy="rekol session-index --full",
            )
        )
    finally:
        store.close()
    return findings


def run_doctor(cfg: Config, embedder: BaseEmbedder) -> DoctorReport:
    """Run every health check and return the collected findings.

    Pure (no I/O beyond reading the stores), so it is unit-testable against a
    sandboxed config without invoking the CLI. The cache-location finding is
    always first so the report leads with WHERE the index lives.
    """
    findings: list[Finding] = [
        Finding(
            label="index cache",
            status=Status.INFO,
            detail=str(cfg.index_dir),
        )
    ]
    findings.extend(_check_curated_index(cfg, embedder))
    findings.extend(_check_session_index(cfg, embedder))
    return DoctorReport(findings=findings)


_STATUS_GLYPH = {Status.OK: "✓", Status.INFO: "·", Status.PROBLEM: "✗"}


@click.command(name="doctor")
def main() -> None:
    """Report index health; exit 1 if any check is degraded.

    Runs the curated-index and transcript-index health checks and prints one line
    per finding with an actionable remedy for each problem. A missing/empty index
    is reported, not treated as a crash.
    """
    cfg = load_config()
    embedder = get_embedder(cfg.embedding_model)
    report = run_doctor(cfg, embedder)

    for finding in report.findings:
        glyph = _STATUS_GLYPH[finding.status]
        click.echo(f"{glyph} {finding.label}: {finding.detail}")
        if finding.remedy is not None and finding.status is Status.PROBLEM:
            click.echo(f"    remedy: {finding.remedy}")

    click.echo("")
    if report.is_healthy:
        click.echo("index is healthy.")
        sys.exit(0)
    problems = [f for f in report.findings if f.status is Status.PROBLEM]
    click.echo(f"index is DEGRADED — {len(problems)} problem(s) found. Remedies above.")
    sys.exit(1)


if __name__ == "__main__":
    main()
