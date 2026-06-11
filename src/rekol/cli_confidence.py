"""``rekol confirm`` and ``rekol flag-suspect`` — the confidence lifecycle (#87).

These stamp the trust state of a curated memory in its frontmatter, then reindex
so search/doctor surface the signal. They are the live→suspect→invalid lifecycle's
two non-terminal writes (``rekol invalidate <file>`` is the terminal one):

* ``confirm``     — "I verified these facts still hold." Stamps ``last_confirmed``
  (distinct from ``updated`` — a plain edit is NOT a confirmation) and clears any
  prior suspect flag (a confirmation supersedes a suspicion).
* ``flag-suspect`` — "I observed something that contradicts this." Stamps
  ``suspected_at`` + ``suspect_reason`` WITHOUT forcing a rewrite/invalidate, so the
  agent can flag a contradiction mid-task and move on; ``review`` surfaces it first
  and the reason gives the next session repair context.

Both reuse ``frontmatter.load → set key → dumps`` (the invalidate pattern), which
round-trips the raw document and so PRESERVES all other frontmatter keys.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import click
import frontmatter

from rekol.cli_common import guard_curated_schema, warn_skipped_files
from rekol.config import Config, load_config
from rekol.embeddings import get_embedder
from rekol.indexer import Indexer
from rekol.store import IndexStore


def _resolve_under_memory_home(cfg: Config, target: Path) -> Path:
    """Resolve ``target`` and refuse anything outside ``MEMORY_HOME`` (typo paranoia)."""
    resolved = target.resolve()
    try:
        resolved.relative_to(cfg.memory_home.resolve())
    except ValueError as exc:
        raise click.ClickException(
            f"{resolved} is not under MEMORY_HOME ({cfg.memory_home})"
        ) from exc
    if not resolved.is_file():
        raise click.ClickException(f"{resolved} is not a file")
    return resolved


def _reindex_curated(cfg: Config) -> None:
    """Reindex curated memory so the new frontmatter is picked up by search/doctor."""
    embedder = get_embedder(cfg.embedding_model)
    store = IndexStore(db_path=cfg.index_db_path, dim=embedder.dim)
    store.init_schema()
    guard_curated_schema(store)
    idx = Indexer(
        memory_root=cfg.memory_home,
        store=store,
        embedder=embedder,
        chunk_max_bytes=cfg.chunk_max_bytes,
        index_dir=cfg.index_dir,
    )
    warn_skipped_files(idx.update())


@click.command()
@click.argument("target")
def confirm(target: str) -> None:
    """Mark a curated memory's facts as re-verified today (stamp ``last_confirmed``).

    Use after you check a memory against reality (read the file, ``git log``, the
    live system) and it still holds. This is DISTINCT from editing the file — an
    edit bumps ``updated``; only a confirmation bumps ``last_confirmed``. Clears any
    prior suspect flag, since a confirmation supersedes a suspicion.
    """
    cfg = load_config()
    path = _resolve_under_memory_home(cfg, Path(target))

    post = frontmatter.load(path)
    today = dt.date.today().isoformat()
    post.metadata["last_confirmed"] = today
    # A confirmation supersedes a suspicion — drop the suspect flag if present.
    cleared_suspect = post.metadata.pop("suspected_at", None) is not None
    post.metadata.pop("suspect_reason", None)
    path.write_text(frontmatter.dumps(post) + "\n")

    _reindex_curated(cfg)
    suffix = " (cleared prior suspect flag)" if cleared_suspect else ""
    click.echo(f"confirmed {path} (last_confirmed={today}){suffix}")


@click.command(name="flag-suspect")
@click.argument("target")
@click.option(
    "--reason",
    default="",
    help="Evidence for why this memory is suspect (e.g. 'file moved in git log'). "
    "Persisted so the next session has the repair context; strongly recommended.",
)
def flag_suspect(target: str, reason: str) -> None:
    """Flag a curated memory as SUSPECTED-stale without invalidating it.

    Use the instant you observe a contradiction (a path moved, a value changed)
    but don't want to stop and rewrite. Stamps ``suspected_at`` + ``suspect_reason``
    in frontmatter; the memory still surfaces in search (flagged ``⚠ suspected``,
    never silently dropped) and ``review`` surfaces it first. Confirm or invalidate
    it later.
    """
    cfg = load_config()
    path = _resolve_under_memory_home(cfg, Path(target))

    post = frontmatter.load(path)
    if post.metadata.get("invalidated_at"):
        raise click.ClickException(
            f"{path} is already invalidated — flag-suspect is for live memories. "
            f"Edit the file to revive it first if its facts are true again."
        )
    now_iso = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    post.metadata["suspected_at"] = now_iso
    if reason:
        post.metadata["suspect_reason"] = reason
    else:
        post.metadata.pop("suspect_reason", None)
    path.write_text(frontmatter.dumps(post) + "\n")

    _reindex_curated(cfg)
    shown_reason = f" — {reason}" if reason else ""
    click.echo(f"flagged suspect {path} (since {now_iso}{shown_reason})")
