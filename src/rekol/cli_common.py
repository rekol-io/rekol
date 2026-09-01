"""Shared helpers across the rekol CLI commands."""

from __future__ import annotations

import click

from rekol.config import Config
from rekol.embeddings import BaseEmbedder
from rekol.index_lock import IndexBusyError, index_lock_path, index_write_lock
from rekol.indexer import Indexer, IndexStats
from rekol.safety import RealIndexClobberError
from rekol.store import IndexModelMismatchError, IndexStore

# The actionable message every read/write path falls back to when it can't
# (or shouldn't) self-heal a stale curated schema.
_SCHEMA_STALE_MSG = "curated index schema is out of date — run `rekol index rebuild`"


def warn_skipped_files(stats: IndexStats) -> None:
    """Emit a loud stderr warning naming files skipped for bad frontmatter.

    Issue #34: the indexer skips any file whose required ``name``/``description``/
    ``type`` frontmatter is missing or invalid, but previously surfaced only a
    bare count buried in the summary line. A user hand-authoring memory files
    then gets an empty (or near-empty) index and "search returns nothing" with
    no signal which file is wrong. Make a non-zero skip visible and actionable:
    name each offending file and the reason, on stderr, so it stands out from
    the normal stdout summary. No-op when nothing was skipped.
    """
    if not stats.skipped_files:
        return
    count = len(stats.skipped_files)
    noun = "file" if count == 1 else "files"
    click.echo(
        f"⚠ skipped {count} {noun} (invalid/missing frontmatter — "
        "required: name, description, type):",
        err=True,
    )
    for path, reason in stats.skipped_files:
        click.echo(f"    {path} — {reason}", err=True)


def guard_curated_schema(store: IndexStore) -> None:
    """Exit with an actionable message if the curated index predates the timestamp columns.

    Any command that opens the curated store and then reads/writes the timestamp
    columns must call this right after ``init_schema()`` — on a legacy index the
    columns are absent and the query/insert would otherwise crash. Closes the
    store before raising ``SystemExit(1)``.
    """
    if store.needs_schema_migration():
        store.close()
        click.echo(_SCHEMA_STALE_MSG, err=True)
        raise SystemExit(1)


def ensure_curated_schema_current(
    store: IndexStore,
    cfg: Config,
    embedder: BaseEmbedder,
    *,
    auto_rebuild: bool,
) -> None:
    """Reconcile a stale curated schema, optionally self-healing it.

    Issue #97: a schema bump (e.g. v4→v5) leaves an existing user's index too old
    to query. Every read path already *detects* this via
    :meth:`IndexStore.needs_schema_migration`, but the only response was to print
    to stderr and exit 1 — which, on the agent-facing ``rekol search --json`` path,
    leaves stdout empty and reads to the assistant as a legitimate "no results"
    rather than "your index needs rebuilding." The fix is to self-heal where it is
    safe to do so.

    ``auto_rebuild=True`` (the interactive ``rekol search`` path): rebuild the index
    in place — crash-safe (temp-DB build + atomic swap), offline-first, and the
    curated corpus is small — then return so the caller can continue with real
    results. The notice goes to stderr only, keeping ``--json`` stdout valid.

    ``auto_rebuild=False`` (everything else): behave exactly like
    :func:`guard_curated_schema` — loud message, ``SystemExit(1)``.

    Self-heal is deliberately narrow. It fires ONLY on a genuine schema-version
    mismatch (a fresh/empty index reports no migration, so first run never triggers
    it), and NOT on an embedding-model identity mismatch — rebuilding *that* would
    silently re-embed under a different model and discard the user's prior model
    choice, so callers must keep handling ``IndexModelMismatchError`` loudly and
    separately. When a rebuild can't run or fails (another index op holds the
    write lock, or the cache dir is read-only) we fall back to the same loud,
    actionable message instead of crashing; the live index is left intact by
    :meth:`Indexer.rebuild`'s rollback.
    """
    if not store.needs_schema_migration():
        return

    if not auto_rebuild:
        store.close()
        click.echo(_SCHEMA_STALE_MSG, err=True)
        raise SystemExit(1)

    # Do NOT auto-heal an index whose stored model identity no longer matches the
    # configured model: a rebuild would re-embed under the *new* model and silently
    # discard the user's prior choice. Leave that for the caller's loud identity
    # guard (which offers "restore the previous model, or rebuild"). Returning here
    # — without closing — lets the caller's own check_model_identity raise. A legacy
    # index with no recorded identity returns cleanly and is safe to rebuild.
    try:
        store.check_model_identity(cfg.embedding_model, embedder.dim)
    except IndexModelMismatchError:
        return

    indexer = Indexer(
        memory_root=cfg.memory_home,
        store=store,
        embedder=embedder,
        chunk_max_bytes=cfg.chunk_max_bytes,
        index_dir=cfg.index_dir,
    )
    try:
        # Non-blocking: if a rebuild/update is already running, don't pile a second
        # rebuild on top — the running op will land the new schema. Fall back to the
        # loud message so the user re-runs once it finishes.
        with index_write_lock(index_lock_path(cfg.index_dir), blocking=False):
            click.echo(
                "curated index schema is out of date — rebuilding automatically "
                "(one-time, after an upgrade)…",
                err=True,
            )
            indexer.rebuild()
    except RealIndexClobberError as exc:
        # The guard fired on the auto-rebuild path. Refusing is correct — but a
        # SEARCH must not die with a traceback because of it. Report why and
        # leave the index untouched; the user's data is intact and the message
        # names the cause (a leaked REKOL_INDEX_DIR, almost always).
        store.close()
        click.echo(f"error: {exc}", err=True)
        raise SystemExit(1) from exc
    except IndexBusyError:
        store.close()
        click.echo(
            "curated index schema is out of date and another rekol index operation "
            "is in progress — run `rekol index rebuild` once it finishes.",
            err=True,
        )
        raise SystemExit(1) from None
    except OSError as exc:
        # The rebuild couldn't run or complete for a filesystem reason. OSError
        # intentionally wraps the WHOLE lock+rebuild block: a read-only/unwritable
        # cache dir can fail either creating the lock file OR building the temp DB,
        # and both should yield the same actionable message rather than a traceback.
        # The live index is left intact (Indexer.rebuild rolls back on any failure).
        # We do NOT broaden beyond OSError: unexpected errors (and the embedder, which
        # is already loaded before we get here) propagate loudly rather than masquerade
        # as a schema-heal failure.
        store.close()
        click.echo(
            f"curated index schema is out of date and an automatic rebuild could not "
            f"complete ({exc}) — run `rekol index rebuild`.",
            err=True,
        )
        raise SystemExit(1) from None
    # rebuild() reopened `store` onto the freshly-swapped-in DB; the caller can
    # continue querying it directly.
