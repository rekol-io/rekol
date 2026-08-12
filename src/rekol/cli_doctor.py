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

import os
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

import click

from rekol.config import Config, load_config
from rekol.embeddings import BaseEmbedder, get_embedder
from rekol.include_coverage import compute_coverage
from rekol.indexer import _iter_memory_files, _skip_reason, is_non_memory, iter_all_markdown
from rekol.model import ValidationError, parse_file
from rekol.onboarding.detect import default_cloud_sync_candidates
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


# Cap the per-file offender list so a store with many rejects doesn't flood the
# report; the count is always exact, only the enumeration is truncated.
_MAX_LISTED_REJECTS = 20


def _relpath(path: Path, root: Path) -> str:
    """Path relative to the store root for a compact report, or absolute on failure."""
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _classify_unindexed(
    on_disk: list[Path],
    indexed: set[str],
    unwalked: set[Path],
    root: Path,
) -> list[tuple[str, str]]:
    """Why each on-disk file is absent from the index: ``[(relpath, reason)]``.

    Two causes with different remedies, which is why they are named differently:
    *NEVER WALKED* is a scope bug in the indexer (the user's file is fine), while a
    validation failure is a problem with the file. Files that are valid and simply
    not indexed yet are NOT listed here — the caller counts them separately, since
    they need a reindex rather than a fix.
    """
    rejected: list[tuple[str, str]] = []
    for path in on_disk:
        if str(path) in indexed:
            continue
        if path in unwalked:
            rejected.append(
                (_relpath(path, root), "NEVER WALKED by the indexer — outside its scope")
            )
            continue
        try:
            parse_file(path)
        except ValidationError as exc:
            rejected.append((_relpath(path, root), _skip_reason(exc)))
        except OSError as exc:
            rejected.append((_relpath(path, root), f"unreadable: {exc}"))
    return rejected


def _check_curated_coverage(cfg: Config, embedder: BaseEmbedder) -> list[Finding]:
    """Disk-vs-index coverage for the curated store (#123).

    The scanner walks the whole store on every index run and REJECTS files whose
    frontmatter fails validation — they stay perfectly readable on disk but never
    enter the index, so they are silently invisible to ``rekol search`` and no
    other check notices (the curated-content check only inspects files already in
    the index). Walk the indexable layers, diff against the ``files`` table, and
    name every rejected on-disk file with its reason. "Index is healthy" must be
    unclaimable while indexable files are being rejected.

    A file that parses cleanly but is not yet indexed is transient staleness (the
    next incremental run picks it up), so it is deliberately NOT flagged here.

    The denominator comes from the FILESYSTEM, never from the indexer's walk
    (#158). Deriving it from ``_iter_memory_files`` meant this check inherited the
    indexer's scope exactly, so any directory outside that scope was invisible to
    both — it reported "52/52 indexed (none rejected)" on a store with 62 files,
    of which 10 were unreachable by search. A green check over a missing file is
    worse than the missing file. The two categories below are therefore reported
    separately, because they have different causes and different fixes:
    **rejected** (walked, failed validation) and **outside the walk** (never
    visited — a scope bug in the indexer, not a problem with the user's file).
    """
    db_path = cfg.index_db_path
    if not db_path.exists():
        return []  # an unbuilt index is already reported by _check_curated_index
    root = cfg.memory_home
    all_markdown = list(iter_all_markdown(root))
    excluded = [p for p in all_markdown if is_non_memory(p, root)]
    on_disk = [p for p in all_markdown if p not in set(excluded)]
    walked = set(_iter_memory_files(root))
    unwalked = [p for p in on_disk if p not in walked]
    if not on_disk:
        return []

    try:
        store = IndexStore(db_path=db_path, dim=embedder.dim, embedding_model=cfg.embedding_model)
    except sqlite3.DatabaseError:
        return []  # corruption is already reported by _check_curated_index
    try:
        indexed = {row["path"] for row in store.conn.execute("SELECT path FROM files")}
    except sqlite3.DatabaseError:
        return []  # ditto — surfaced by the curated-index check that runs first
    finally:
        store.close()

    indexed_count = sum(1 for path in on_disk if str(path) in indexed)
    rejected = _classify_unindexed(on_disk, indexed, set(unwalked), root)

    # Always state the excluded count. Silence about them is what let "52/52"
    # read as complete coverage when it was not.
    suffix = f"; {len(excluded)} deliberately excluded (tasks/, MEMORY.md)" if excluded else ""
    # The verdict must consider the NUMERATOR too. #158 fixed the denominator
    # (disk truth, not the indexer's walk) but left OK/PROBLEM keyed on `rejected`
    # alone — so `1/6 curated files indexed (none rejected)` printed a green tick
    # and "index is healthy" while five of six files were unreachable by search.
    # Computing a number, printing it, and then not judging it is the same class
    # of bug one level up.
    unindexed = len(on_disk) - indexed_count
    if not rejected and unindexed <= 0:
        return [
            Finding(
                label="curated coverage",
                status=Status.OK,
                detail=(
                    f"{indexed_count}/{len(on_disk)} curated files indexed (none rejected){suffix}"
                ),
            )
        ]
    if not rejected:
        # Valid files that simply have not been indexed yet. Transient for one
        # cycle; indefinite if the reindex hook is dead — which is exactly the
        # #159 world, so it cannot be waved through as "the next run picks it up".
        return [
            Finding(
                label="curated coverage",
                status=Status.PROBLEM,
                detail=(
                    f"{indexed_count}/{len(on_disk)} curated files indexed{suffix} — "
                    f"{unindexed} valid file(s) on disk are NOT in the index and are "
                    "therefore invisible to search"
                ),
                remedy="rekol index update   (if this persists, the reindex hook is not running)",
            )
        ]
    shown = rejected[:_MAX_LISTED_REJECTS]
    lines = "\n".join(f"      {rel} — {reason}" for rel, reason in shown)
    if len(rejected) > _MAX_LISTED_REJECTS:
        lines += f"\n      … and {len(rejected) - _MAX_LISTED_REJECTS} more"
    return [
        Finding(
            label="curated coverage",
            status=Status.PROBLEM,
            detail=(
                f"{indexed_count}/{len(on_disk)} curated files indexed{suffix} — "
                f"{len(rejected)} invisible to search:\n{lines}"
            ),
            remedy="fix the frontmatter of the files above, then `rekol index update`",
        )
    ]


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


def _check_archive(cfg: Config) -> list[Finding]:
    """Inspect the durable transcript archive: presence, writability, count, age.

    Also emits a SECURITY warning when the archive resolves under a cloud-sync
    mount. The archive holds verbatim prompts (and any pasted secrets). The default is a
    local, non-synced dir; if a user relocated it under Dropbox/iCloud/Drive/
    OneDrive, on-demand/streaming sync can dehydrate files (a rebuild then reads
    placeholders) AND the secrets leave the machine. We flag that loudly.
    """
    findings: list[Finding] = []
    if not cfg.archive_enabled:
        findings.append(
            Finding(
                label="transcript archive",
                status=Status.INFO,
                detail="archive_enabled=false (durable archive off; rebuilds read live only)",
            )
        )
        return findings

    archive_dir = cfg.archive_dir

    # Cloud-mount heuristic: is the resolved archive under a known sync root? The
    # matchable signal is the candidate's real PATH VALUE, NOT the display label's
    # first word — iCloud Drive's on-disk path is
    # ``~/Library/Mobile Documents/com~apple~CloudDocs``, which contains no
    # "iCloud" segment, so the old label-word match silently missed it. We resolve
    # the archive and test containment against each candidate path
    # (``is_relative_to``); we ALSO test a set of on-disk segment signals (the
    # iCloud container name; provider folder names) so a relocated archive that
    # sits under such a folder ANYWHERE (an explicit REKOL_ARCHIVE_DIR off the home
    # tree) is still caught.
    resolved_archive = archive_dir.resolve()
    cloud_label: str | None = None
    for label, candidate in default_cloud_sync_candidates().items():
        if resolved_archive.is_relative_to(candidate.resolve()):
            cloud_label = label
            break
    if cloud_label is None:
        # Path-segment fallback for archives outside the home-tree candidates: the
        # iCloud container + provider folder names as they appear on disk.
        archive_parts = set(resolved_archive.parts)
        cloud_segment_signals = {
            "com~apple~CloudDocs": "iCloud Drive",
            "Mobile Documents": "iCloud Drive",
            "Dropbox": "Dropbox",
            "Google Drive": "Google Drive",
            "OneDrive": "OneDrive",
        }
        for segment, label in cloud_segment_signals.items():
            if segment in archive_parts:
                cloud_label = label
                break
    if cloud_label is not None:
        findings.append(
            Finding(
                label="archive location",
                status=Status.PROBLEM,
                detail=(
                    f"archive resolves under {cloud_label} ({archive_dir}) — a synced archive "
                    f"puts verbatim transcripts (and any pasted secrets) in the cloud, and "
                    f"on-demand sync can dehydrate files so a rebuild reads placeholders"
                ),
                remedy=(
                    "move it local: set REKOL_ARCHIVE_DIR (or archive_dir) to a non-synced path"
                ),
            )
        )

    if not archive_dir.exists():
        findings.append(
            Finding(
                label="transcript archive",
                status=Status.INFO,
                detail=f"not built yet (no {archive_dir})",
                remedy="rekol archive",
            )
        )
        return findings

    # Count archived sessions (flat .jsonl files) and find the newest mtime.
    archived = list(archive_dir.glob("**/*.jsonl"))
    newest_mtime: int | None = None
    for path in archived:
        try:
            mtime = int(path.stat().st_mtime)
        except OSError:
            continue
        if newest_mtime is None or mtime > newest_mtime:
            newest_mtime = mtime
    writable = os.access(archive_dir, os.W_OK)
    findings.append(
        Finding(
            label="transcript archive",
            status=Status.OK if writable else Status.PROBLEM,
            detail=(
                f"{len(archived)} archived session file(s) at {archive_dir}; "
                f"last archived {_format_last_indexed(newest_mtime)}; "
                f"{'writable' if writable else 'NOT writable'}"
            ),
            remedy=None if writable else "fix permissions on the archive dir, or relocate it",
        )
    )
    return findings


def _check_include_scope(cfg: Config) -> list[Finding]:
    """Report include-scope coverage: indexed-vs-discoverable files (T8 #63).

    SILENT-SKIP when no ``include_dirs`` are configured — the feature is additive,
    so an existing user who never opted in sees no finding (mirrors the spec's
    "never 'Found 0 sources'" rule and keeps ``doctor`` quiet for them).

    When include-scope IS configured, emit the "Z of ~Y discoverable files
    indexed (~C%)" coverage line. PROBLEM-vs-INFO: a partial coverage (< 100%)
    means some in-scope files have changed since the last index and are awaiting
    the next ``session-index`` run — a degraded-but-self-healing state, so we
    flag it as a PROBLEM with the catch-up remedy. Full coverage is OK.
    """
    if not cfg.include_dirs:
        return []
    report = compute_coverage(cfg)
    if report.discoverable == 0:
        # Configured dirs but nothing discoverable (empty/missing dirs): INFO, not
        # a failure — there is simply nothing in scope to index yet.
        return [
            Finding(
                label="include scope",
                status=Status.INFO,
                detail="include_dirs configured but no discoverable files in scope yet",
            )
        ]
    if report.indexed < report.discoverable:
        return [
            Finding(
                label="include scope",
                status=Status.PROBLEM,
                detail=f"{report.summary()} — some in-scope files await (re)indexing",
                remedy="rekol session-index --incremental",
            )
        ]
    return [
        Finding(
            label="include scope",
            status=Status.OK,
            detail=report.summary(),
        )
    ]


# Deep-probe semantic pair: a healthy model separates the related sentence from
# the unrelated one by a clear margin. A model that failed to load and silently
# fell back to a different (mean-pooling) model keeps the SAME recorded identity
# but produces meaningless vectors — the gap collapses. This is the runtime catch
# the model-identity check (which only compares the recorded name) cannot make.
_DEEP_BASE = "how do I reset my account password"
_DEEP_RELATED = "steps to recover access to my login"
_DEEP_UNRELATED = "the geological formation of volcanic basalt rock"
_DEEP_MARGIN = 0.10


def _check_deep(cfg: Config, embedder: BaseEmbedder) -> list[Finding]:
    """Deep probes (``--deep``) for the post-install acceptance check.

    Prove the model loads AND embeds meaningfully, and that curated recall works
    end-to-end. Catches the silent-degradation class that a clean install can
    exhibit while every shallow check still passes.
    """
    import numpy as np

    findings: list[Finding] = []

    # 1. Embedding runtime + semantic separation.
    try:
        base = embedder.embed(_DEEP_BASE)
        related = embedder.embed(_DEEP_RELATED)
        unrelated = embedder.embed(_DEEP_UNRELATED)
    except Exception as exc:  # noqa: BLE001 — ANY load/run failure means a broken model
        msg = str(exc)
        # Intel macOS ships an older torch (NumPy 1.x ABI); under NumPy 2.x the
        # first embedding raises "_ARRAY_API not found" / "Numpy is not available".
        # Give the exact fix instead of a generic model-cache hint.
        if any(s in msg for s in ("_ARRAY_API", "NumPy 1.x", "Numpy is not available")):
            remedy = (
                "Intel-mac torch/NumPy ABI mismatch — pin NumPy 1.x in the venv:\n"
                "    ~/.local/share/rekol/.venv/bin/pip install 'numpy<2'"
            )
        else:
            remedy = "verify the model cache; check it loads offline (local_files_only)"
        findings.append(
            Finding(
                label="embedding runtime",
                status=Status.PROBLEM,
                detail=f"embedding model failed to run: {exc}",
                remedy=remedy,
            )
        )
        return findings  # no working embedder → can't probe recall
    sim_related = float(np.dot(base, related))
    sim_unrelated = float(np.dot(base, unrelated))
    margin = sim_related - sim_unrelated
    if margin < _DEEP_MARGIN:
        findings.append(
            Finding(
                label="embedding semantics",
                status=Status.PROBLEM,
                detail=(
                    f"semantic separation collapsed (related {sim_related:.2f} vs unrelated "
                    f"{sim_unrelated:.2f}, margin {margin:.2f} < {_DEEP_MARGIN}) — the model may "
                    "have loaded degraded (wrong/mean-pooling)"
                ),
                remedy="rebuild the model cache; verify the embedding model loads offline",
            )
        )
    else:
        findings.append(
            Finding(
                label="embedding semantics",
                status=Status.OK,
                detail=(
                    f"related {sim_related:.2f} vs unrelated {sim_unrelated:.2f} "
                    f"(margin {margin:.2f})"
                ),
            )
        )

    # 2. End-to-end curated recall: a known chunk must come back from its own text.
    db_path = cfg.index_db_path
    if not db_path.exists():
        findings.append(
            Finding(
                label="recall probe",
                status=Status.INFO,
                detail="no curated index to probe (build it with `rekol index rebuild`)",
            )
        )
        return findings
    store = IndexStore(db_path=db_path, dim=embedder.dim)
    try:
        store.init_schema()
        row = store.conn.execute(
            "SELECT text, file_path FROM chunks WHERE text != '' LIMIT 1"
        ).fetchone()
        if row is None:
            findings.append(
                Finding(
                    label="recall probe",
                    status=Status.INFO,
                    detail="curated index has no chunks to probe",
                )
            )
            return findings
        hits = store.search(embedder.embed(row["text"]), top_k=3)
    finally:
        store.close()
    if any(h["file_path"] == row["file_path"] for h in hits):
        findings.append(
            Finding(
                label="recall probe",
                status=Status.OK,
                detail="a known chunk is retrievable end-to-end (embed → vector search)",
            )
        )
    else:
        findings.append(
            Finding(
                label="recall probe",
                status=Status.PROBLEM,
                detail="a known chunk did not return from its own text — the search path is broken",
                remedy="rekol index rebuild",
            )
        )
    return findings


def _check_install_drift(cfg: Config) -> list[Finding]:
    """Is what's installed actually wired? — offline drift detection (#27).

    The finding that motivated this: a machine ran a current checkout while three
    hook handlers shipped over 11 days were never registered, and *nothing could
    say so*. A version check would have reported "up to date", because the code
    genuinely was. Only comparing the shipped handlers against the wiring catches
    it, so that comparison is what decides the severity here.

    Severity is deliberately asymmetric:

    * **missing handlers → PROBLEM.** This is real, silent feature loss with a
      one-command remedy.
    * **version drift alone → INFO.** An editable/dev checkout drifts from its
      recorded install on every ``git pull``; making that a PROBLEM would put
      ``doctor`` permanently red for the people who work on rekol, and a check
      that is always red is a check nobody reads.
    * **no recorded version → INFO,** not a mismatch. It means the install
      predates version stamping; conflating "unknown" with "different" is how you
      get a warning that cannot be cleared.
    """
    from rekol.update import (
        detect_drift,
        expected_handlers,
        load_settings,
        manifest_path,
        unrunnable_hooks,
    )

    if not manifest_path(cfg.memory_home).is_file():
        # No manifest at all: install.sh was never run against this REKOL_HOME.
        # Say so plainly rather than reporting "no drift", which would be a claim
        # about wiring we have no evidence for.
        return [
            Finding(
                label="install record",
                status=Status.INFO,
                detail=f"no install manifest at {manifest_path(cfg.memory_home)} — drift unknown",
                remedy="./install.sh   (records what is installed, so drift becomes detectable)",
            )
        ]

    drift = detect_drift(cfg.memory_home)
    findings: list[Finding] = []
    if drift.missing_handlers:
        listed = ", ".join(drift.missing_handlers)
        findings.append(
            Finding(
                label="hook wiring",
                status=Status.PROBLEM,
                detail=(
                    f"{len(drift.missing_handlers)} handler(s) this version ships are NOT "
                    f"registered in settings.json: {listed}"
                ),
                remedy="./install.sh   (idempotent; repairs and adds hooks in place)",
            )
        )
    else:
        # "Registered" is a claim about TEXT. Verified reproducibly: this line
        # printed `all 6 shipped handlers registered` + `index is healthy` (exit 0)
        # for a settings.json where every command pointed at a path that does not
        # exist. So presence is necessary and nowhere near sufficient — probe that
        # each hook's absolute fallback can actually run before claiming health.
        broken = unrunnable_hooks(load_settings())
        if broken:
            lines = "\n".join(f"      {name} — {why}" for name, why in broken)
            findings.append(
                Finding(
                    label="hook wiring",
                    status=Status.PROBLEM,
                    detail=(
                        f"all {len(expected_handlers())} handlers are registered but "
                        f"{len(broken)} cannot execute:\n{lines}"
                    ),
                    remedy="./install.sh   (re-renders hook commands with a working path)",
                )
            )
        else:
            findings.append(
                Finding(
                    label="hook wiring",
                    status=Status.OK,
                    detail=(
                        f"all {len(expected_handlers())} shipped handlers registered and executable"
                    ),
                )
            )
    if drift.version_unknown:
        findings.append(
            Finding(
                label="install version",
                status=Status.INFO,
                detail=(
                    f"running {drift.running_version}; install record predates version "
                    f"stamping (installed {drift.installed_at or 'unknown'})"
                ),
                remedy="./install.sh   (re-stamps the manifest)",
            )
        )
    elif drift.version_drifted:
        findings.append(
            Finding(
                label="install version",
                status=Status.INFO,
                detail=(
                    f"running {drift.running_version} but the recorded install is "
                    f"{drift.installed_version} (installed {drift.installed_at or 'unknown'})"
                ),
                remedy="./install.sh   (re-wires hooks and re-stamps the manifest)",
            )
        )
    else:
        findings.append(
            Finding(
                label="install version",
                status=Status.OK,
                detail=f"{drift.running_version} matches the recorded install",
            )
        )
    return findings


def run_doctor(cfg: Config, embedder: BaseEmbedder, *, deep: bool = False) -> DoctorReport:
    """Run every health check and return the collected findings.

    Pure (no I/O beyond reading the stores), so it is unit-testable against a
    sandboxed config without invoking the CLI. The cache-location finding is
    always first so the report leads with WHERE the index lives. ``deep=True``
    adds the runtime model + end-to-end recall probes (``--deep``).
    """
    findings: list[Finding] = [
        Finding(
            label="index cache",
            status=Status.INFO,
            detail=str(cfg.index_dir),
        )
    ]
    findings.extend(_check_curated_index(cfg, embedder))
    findings.extend(_check_curated_coverage(cfg, embedder))
    findings.extend(_check_session_index(cfg, embedder))
    findings.extend(_check_archive(cfg))
    findings.extend(_check_include_scope(cfg))
    findings.extend(_check_install_drift(cfg))
    if deep:
        findings.extend(_check_deep(cfg, embedder))
    return DoctorReport(findings=findings)


_STATUS_GLYPH = {Status.OK: "✓", Status.INFO: "·", Status.PROBLEM: "✗"}


@click.command(name="doctor")
@click.option(
    "--deep",
    is_flag=True,
    help="Also run runtime probes: that the embedding model loads + embeds "
    "meaningfully (catches silent degradation) and that curated recall works "
    "end-to-end. Used as the post-install acceptance check.",
)
def main(deep: bool) -> None:
    """Report index health; exit 1 if any check is degraded.

    Runs the curated-index and transcript-index health checks and prints one line
    per finding with an actionable remedy for each problem. A missing/empty index
    is reported, not treated as a crash. ``--deep`` adds runtime model + recall
    probes — the one-command "is this install genuinely working" acceptance check.
    """
    cfg = load_config()
    embedder = get_embedder(cfg.embedding_model)
    report = run_doctor(cfg, embedder, deep=deep)

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
