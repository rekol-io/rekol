"""Read Claude Code ~/.claude/projects/*/*.jsonl transcripts, normalise rows, write to SessionStore.

Filters non-message rows (queue-operation, etc.) and normalises content that
may be a string or a list of content blocks into a single text payload. Dedupe
relies on the UNIQUE(session_id, message_uuid) constraint in the store.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from .store import SessionStore

if TYPE_CHECKING:
    from rekol.embeddings import BaseEmbedder


@dataclass
class IngestStats:
    """Tally of file and message outcomes from a session ingest run."""

    files_seen: int = 0
    files_ingested: int = 0
    files_skipped_unchanged: int = 0  # mtime+size match — already ingested
    messages_inserted: int = 0
    messages_skipped_dupe: int = 0
    messages_skipped_malformed: int = 0  # parse error or missing required field
    messages_skipped_no_text: int = 0  # row IS a message but has no indexable text
    # (assistant tool_use only, thinking only,
    #  user tool_result only). This is the dominant
    #  bucket on real transcripts; tracked separately
    #  so the user can see the real drop rate vs
    #  parse errors.


# Row types in the JSONL stream we treat as messages.
_MESSAGE_TYPES = ("user", "assistant")


def _flatten_content(content) -> str | None:
    """Convert message.content to plain text. Returns None if there's nothing usable."""
    if isinstance(content, str):
        text = content.strip()
        return text or None
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                # Common shapes: {"type": "text", "text": "..."} and tool blocks
                t = block.get("text")
                if isinstance(t, str):
                    parts.append(t)
                # Tool-use blocks have input dicts; skip them for text indexing.
                # Tool-result blocks have a content field we deliberately do
                # NOT index — that's command output, not user/assistant prose.
        joined = " ".join(p.strip() for p in parts if p and p.strip())
        return joined or None
    return None


def _parse_timestamp(ts: str) -> tuple[str, int]:
    """Return (iso_string, unix_seconds). Handles trailing Z suffix."""
    iso = ts
    parseable = ts.rstrip("Z") + "+00:00" if ts.endswith("Z") else ts
    dt = datetime.fromisoformat(parseable)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return iso, int(dt.timestamp())


@dataclass
class _RawIterResult:
    """Internal carrier for iter_messages_in_file's skip counts.

    Lets the iterator report rows skipped for "no indexable text" (and
    malformed rows) without forcing the caller to consume two iterators.
    The stats are mutated by the iterator; the caller reads them after the
    iteration completes.
    """

    no_text_count: int = 0
    malformed_count: int = 0


def iter_messages_in_file(jsonl_path: Path, stats: _RawIterResult | None = None) -> Iterator[dict]:
    """Yield normalised message dicts ready for SessionStore.insert_message.

    Skips non-message rows (queue-operation, attachment, etc.) entirely.
    For rows whose ``type`` is ``user``/``assistant`` but whose content has
    no indexable text (assistant tool_use only, thinking only, user
    tool_result only — the dominant case in real transcripts), the row is
    counted in ``stats.no_text_count`` rather than yielded. Malformed rows
    (JSON decode error, missing required field, bad timestamp) bump
    ``stats.malformed_count``.

    ``line_number`` is the 1-indexed line in the file at which the message
    was found.
    """
    path = Path(jsonl_path)
    with path.open("r", encoding="utf-8") as fh:
        for line_number, raw in enumerate(fh, start=1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                if stats is not None:
                    stats.malformed_count += 1
                continue
            row_type = row.get("type")
            if row_type not in _MESSAGE_TYPES:
                continue  # not a message row at all — silent, not counted
            uuid = row.get("uuid")
            session_id = row.get("sessionId")
            timestamp = row.get("timestamp")
            message = row.get("message") or {}
            if not uuid or not session_id or not timestamp:
                if stats is not None:
                    stats.malformed_count += 1
                continue
            content = _flatten_content(message.get("content"))
            if not content:
                # IS a message turn, but no text to index (tool_use, thinking,
                # tool_result without text). Track separately so users can see
                # the v1 framing: "human-typed + assistant-text turns only".
                if stats is not None:
                    stats.no_text_count += 1
                continue
            try:
                iso, unix = _parse_timestamp(timestamp)
            except (ValueError, TypeError, AttributeError):
                if stats is not None:
                    stats.malformed_count += 1
                continue
            yield dict(
                session_id=session_id,
                message_uuid=uuid,
                parent_uuid=row.get("parentUuid"),
                role=message.get("role") or row_type,
                content=content,
                cwd=row.get("cwd"),
                timestamp_iso=iso,
                timestamp_unix=unix,
                jsonl_path=str(path),
                line_number=line_number,
            )


def ingest_file(
    jsonl_path: Path,
    store: SessionStore,
    *,
    force: bool = False,
    embedder: BaseEmbedder | None = None,
) -> IngestStats:
    """Ingest a single JSONL file.

    Honours mtime+size skip via ``files_seen``: if the file is unchanged
    since last ingest, returns a stats record with ``files_skipped_unchanged=1``
    and no other work. Set ``force=True`` to bypass the skip (used by
    ``--full`` mode).

    When ``embedder`` is provided, every newly-inserted message is also embedded
    (batched once per file) and written to the vector index, making transcript
    search semantic rather than keyword-only. ``embedder=None`` keeps the
    FTS5-only path for speed.

    All inserts for the file — messages *and* their embeddings — happen in a
    single transaction (BEGIN ... COMMIT) to avoid the per-row fsync cost that
    would otherwise dominate backfill on machines with deep transcript history,
    and to keep rows and embeddings atomic.
    """
    path = Path(jsonl_path)
    stat = path.stat()
    mtime_unix = int(stat.st_mtime)
    size_bytes = int(stat.st_size)

    stats = IngestStats(files_seen=1)
    if not force and store.should_skip_file(str(path), mtime_unix, size_bytes):
        stats.files_skipped_unchanged = 1
        return stats

    raw_stats = _RawIterResult()
    # Batched transaction — single commit per file rather than per row.
    # `with conn:` commits on normal exit, rolls back on exception, and
    # interacts correctly with Python sqlite3's implicit transaction state
    # (a manual BEGIN raises OperationalError if the module thinks a
    # transaction is already open).
    with store.conn:
        # (rowid, content) for each newly-inserted message, embedded in one
        # batch after the insert loop so model inference happens once per file.
        pending_embeddings: list[tuple[int, str]] = []
        for msg in iter_messages_in_file(path, raw_stats):
            rowid = store.insert_message_no_commit(msg)
            if rowid is None:
                stats.messages_skipped_dupe += 1
            else:
                stats.messages_inserted += 1
                if embedder is not None:
                    pending_embeddings.append((rowid, msg["content"]))
        if embedder is not None and pending_embeddings:
            vectors = embedder.embed_batch([content for _, content in pending_embeddings])
            for (rowid, _content), vector in zip(pending_embeddings, vectors, strict=True):
                store.upsert_embedding_no_commit(rowid, vector)

    # files_seen is recorded AFTER the messages+embeddings commit so a crash
    # mid-file leaves the file un-recorded and the next run reingests it.
    store.record_file_seen(str(path), mtime_unix, size_bytes)
    stats.files_ingested = 1
    stats.messages_skipped_no_text = raw_stats.no_text_count
    stats.messages_skipped_malformed = raw_stats.malformed_count
    return stats


def embed_missing(
    store: SessionStore,
    embedder: BaseEmbedder,
    *,
    batch_size: int = 256,
    progress_cb: Callable[[int], None] | None = None,
) -> int:
    """Backfill embeddings for any messages that lack one. Returns the count.

    This is the self-heal for the mtime+size skip gate: a file ingested while
    embeddings were off (FTS-only, or ``--no-embed``) is never revisited by the
    walk, and a forced re-walk does not help because its messages dedupe to
    ``rowid=None`` and so are never re-embedded. Scanning for unembedded rows
    directly is the only way to bring such an index to full semantic coverage
    without a destructive rebuild.

    Cheap in the steady state: when every message is already embedded the count
    guard returns immediately without the anti-join. The work, when needed, is
    done in batched transactions (one commit per batch) for the same fsync
    reason as ``ingest_file``.
    """
    if store.count_messages() == store.count_embeddings():
        return 0
    total = 0
    while True:
        batch = store.fetch_unembedded(limit=batch_size)
        if not batch:
            break
        with store.conn:
            vectors = embedder.embed_batch([content for _rowid, content in batch])
            for (rowid, _content), vector in zip(batch, vectors, strict=True):
                store.upsert_embedding_no_commit(rowid, vector)
        total += len(batch)
        if progress_cb is not None:
            progress_cb(total)
    return total


def ingest_directory(
    root: Path,
    store: SessionStore,
    *,
    force: bool = False,
    embedder: BaseEmbedder | None = None,
    progress_cb: Callable[[int, int], None] | None = None,
) -> IngestStats:
    """Ingest every .jsonl under root (typically ``~/.claude/projects``).

    When ``embedder`` is provided it is passed through to each file so message
    embeddings are written alongside the rows (semantic search); ``None`` keeps
    the FTS5-only path.

    When ``progress_cb`` is provided, it is invoked every 50 files with
    ``(files_done, files_total)`` so callers can print a counter for
    multi-minute backfills. Tests typically pass ``None`` to keep stdout
    clean.
    """
    root = Path(root)
    total = IngestStats()
    jsonls = sorted(root.glob("**/*.jsonl"))
    files_total = len(jsonls)
    for index, jsonl in enumerate(jsonls, start=1):
        file_stats = ingest_file(jsonl, store, force=force, embedder=embedder)
        total.files_seen += file_stats.files_seen
        total.files_ingested += file_stats.files_ingested
        total.files_skipped_unchanged += file_stats.files_skipped_unchanged
        total.messages_inserted += file_stats.messages_inserted
        total.messages_skipped_dupe += file_stats.messages_skipped_dupe
        total.messages_skipped_malformed += file_stats.messages_skipped_malformed
        total.messages_skipped_no_text += file_stats.messages_skipped_no_text
        if progress_cb is not None and (index % 50 == 0 or index == files_total):
            progress_cb(index, files_total)
    return total
