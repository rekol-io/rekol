"""Indexer: walk $MEMORY_HOME, embed changed files, write INDEX.md into the cache."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .chunker import chunk_body
from .config import SKIP_MANIFEST_NAME
from .embeddings import BaseEmbedder
from .model import ValidationError, parse_file
from .store import IndexStore


@dataclass
class IndexStats:
    """Tally of file and chunk outcomes from an indexing run."""

    files_indexed: int = 0
    files_skipped: int = 0
    files_removed: int = 0
    chunks_written: int = 0
    # (file_path, reason) for every file skipped because its frontmatter failed
    # validation. Carried out of the indexer so the CLI can name the offenders
    # loudly instead of reporting a bare count — a silent skip that leaves the
    # index empty is exactly what makes "search returns nothing" baffling.
    skipped_files: list[tuple[str, str]] = field(default_factory=list)


# Top-level layer dirs walked by the indexer.  Memory files in any of these
# are searchable.  ``projects/`` is also walked but treated specially below —
# its sublayer is ``projects/<slug>/<layer>/<file>.md``.
INDEXED_DIRS = ("always", "when", "topics", "knowledge", "feedback")
PROJECTS_DIR = "projects"

# Paths under the store root that are deliberately NOT curated memory. Listed
# explicitly rather than left to "whatever the walk happens to miss" (#157/#158):
# a coverage check has to tell "excluded on purpose" apart from "silently outside
# the walk", and it cannot do that if the only definition of scope IS the walk.
#
# `tasks/` is the #113 operational task layer — its own schema, machine-managed,
# and indexing it would pollute memory search with task churn. `MEMORY.md` is the
# pointer index injected at SessionStart; it carries no frontmatter by design, so
# indexing it would report a permanent rejection for a file that is working
# exactly as intended.
NON_MEMORY_DIRS = ("tasks",)
NON_MEMORY_FILES = ("MEMORY.md",)


def iter_all_markdown(root: Path) -> Iterable[Path]:
    """Every ``.md`` file under ``root`` — the FILESYSTEM truth, not the walk's.

    Deliberately independent of :func:`_iter_memory_files`. ``doctor``'s coverage
    check used the indexer's own walk, so it compared the index against itself and
    structurally could not report a file the indexer never discovered — it printed
    "52/52 indexed" for a store holding 62 files (#158). A check that shares the
    walk shares its blind spot, so this one does not share the walk.
    """
    if not root.is_dir():
        return []
    return sorted(p for p in root.glob("**/*.md") if not _in_hidden_dir(p, root))


def _in_hidden_dir(path: Path, root: Path) -> bool:
    try:
        rel = path.relative_to(root)
    except ValueError:  # pragma: no cover — glob results are always under root
        return False
    return any(part.startswith(".") for part in rel.parts)


def is_non_memory(path: Path, root: Path) -> bool:
    """True for paths excluded from memory ON PURPOSE (see NON_MEMORY_*)."""
    try:
        rel = path.relative_to(root)
    except ValueError:
        return False
    if rel.parts and rel.parts[0] in NON_MEMORY_DIRS:
        return True
    return len(rel.parts) == 1 and rel.parts[0] in NON_MEMORY_FILES


def _iter_memory_files(root: Path) -> Iterable[Path]:
    """Yield every indexable .md file under ``root``.

    Walks the four top-level layer dirs first, then any per-project layer dirs
    under ``projects/<slug>/<layer>/``.  Project files keep the same layer
    semantics as their global counterparts — only the path differs.
    """
    for sub in INDEXED_DIRS:
        d = root / sub
        if d.is_dir():
            yield from sorted(d.glob("**/*.md"))
    projects_root = root / PROJECTS_DIR
    if projects_root.is_dir():
        # Flat `projects/<name>.md` as well as `projects/<slug>/<layer>/`. Real
        # stores contain BOTH — the legacy Claude "project" memory type migrates
        # to a flat file (declaring `type: project` → `topic`), while rekol's own
        # per-project layout nests under a slug. Walking only the nested form left
        # the flat files unreachable by search, and the coverage check could not
        # see it because it shared this walk (#157/#158).
        yield from sorted(projects_root.glob("*.md"))
        for slug_dir in sorted(projects_root.iterdir()):
            if not slug_dir.is_dir():
                continue
            for sub in INDEXED_DIRS:
                d = slug_dir / sub
                if d.is_dir():
                    yield from sorted(d.glob("**/*.md"))


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def _skip_reason(exc: ValidationError) -> str:
    """Extract the human-readable reason from a ValidationError message.

    ``parse_file`` raises ``ValidationError(f"{path}: <reason>")``; the path is
    already carried separately in ``skipped_files``, so strip the leading
    ``<path>: `` prefix and keep just the reason (e.g. ``missing required field
    'name'``) for a compact, de-duplicated warning.
    """
    message = str(exc)
    _, separator, tail = message.partition(": ")
    return tail if separator else message


class Indexer:
    """Indexes memory .md files into the store, embedding their chunks."""

    def __init__(
        self,
        memory_root: Path,
        store: IndexStore,
        embedder: BaseEmbedder,
        chunk_max_bytes: int = 4000,
        index_dir: Path | None = None,
    ) -> None:
        self.memory_root = Path(memory_root)
        self.store = store
        self.embedder = embedder
        self.chunk_max_bytes = chunk_max_bytes
        # SECURITY: INDEX.md is derived state and must land in the local-only
        # cache (outside $REKOL_HOME) alongside the SQLite stores, so the synced
        # memory folder holds pure markdown only.  When no index_dir is given we
        # fall back to the legacy in-tree location for backward compatibility.
        self.index_dir = Path(index_dir) if index_dir is not None else self.memory_root / ".index"

    def _index_one(self, path: Path, content_hash: str, store: IndexStore | None = None) -> int:
        """Parse, embed, and atomically write a single file's row + chunks.

        Embedding (the slow, failure-prone step) happens BEFORE the store is
        touched, so a parse/embed error leaves the index completely untouched.
        The files-row hash and the chunks are then written in ONE transaction
        via ``replace_file_and_chunks`` — the hash never advances ahead of the
        chunks, so a crash mid-write re-indexes cleanly next run (the #18 fix).

        Args:
            path: The memory ``.md`` file to parse, embed, and index.
            content_hash: Pre-computed content hash to stamp on the files row.
            store: Where to write. Defaults to the live ``self.store``; the atomic
                rebuild passes the side-built temp store so the live index is never
                touched until the swap.

        Returns:
            Number of chunk rows written.
        """
        store = store if store is not None else self.store
        mf = parse_file(path)
        mtime = int(path.stat().st_mtime)
        chunks = chunk_body(mf.body, max_bytes=self.chunk_max_bytes)
        if not chunks:
            # Still register the file with one fallback chunk so it's
            # discoverable by search even when the body is empty.
            fallback_text = f"{mf.name}: {mf.description}"
            vec = self.embedder.embed(fallback_text)
            records = [
                dict(
                    heading=None,
                    line_start=1,
                    line_end=1,
                    text=fallback_text,
                    tags=mf.tags,
                    aliases=mf.aliases,
                    embedding=vec,
                )
            ]
        else:
            # Prefix each chunk with the file name to give the embedder more
            # context: improves retrieval when the user's query mentions the
            # topic but the chunk body itself doesn't repeat the name.
            texts = [f"{mf.name}\n{c.text}" for c in chunks]
            vecs = self.embedder.embed_batch(texts)
            records = [
                dict(
                    heading=c.heading,
                    line_start=c.line_start,
                    line_end=c.line_end,
                    text=c.text,
                    tags=mf.tags,
                    aliases=mf.aliases,
                    embedding=vecs[i],
                )
                for i, c in enumerate(chunks)
            ]
        store.replace_file_and_chunks(
            str(path),
            mtime,
            content_hash,
            records,
            name=mf.name,
            created=mf.created,
            updated=mf.updated,
            valid_from=mf.valid_from,
            invalidated_at=mf.invalidated_at,
            last_confirmed=mf.last_confirmed,
            suspected_at=mf.suspected_at,
            suspect_reason=mf.suspect_reason,
        )
        return len(records)

    def _stale_temp_dbs(self, live_db: Path) -> list[Path]:
        """Temp DBs (incl. their SQLite sidecars) left by a prior killed rebuild."""
        return sorted(live_db.parent.glob(live_db.name + ".tmp-*"))

    def rebuild(self) -> IndexStats:
        """Reindex every file from scratch via an atomic temp-DB build + swap.

        C2 (#index-integrity): instead of wiping the LIVE ``index.db`` and
        re-adding files one-by-one (a kill in the middle would leave the live
        index EMPTY), build the whole new index into a temp DB next to the real
        one and ``os.replace`` it over ``index.db`` only once it is complete.

        Why this is crash-safe:
          * The temp DB sits in the SAME cache dir as the live DB, so the swap is
            a same-filesystem ``os.replace`` — atomic on POSIX and Windows.
          * The live ``index.db`` is never touched until that single replace, so a
            kill (or an embed failure) anywhere during the build leaves the OLD
            index fully intact and queryable.
          * The temp DB is built through the same ``replace_file_and_chunks`` path
            as everything else, so it ends up with the C4 ``metadata`` identity
            stamp and the correct ``user_version`` — the swapped-in file is a
            complete, valid, identity-stamped index.
        """
        stats = IndexStats()
        live_db = self.store.db_path
        # Clean up any temp DB a previously-killed rebuild left behind so they
        # neither accumulate nor get mistaken for the live index.
        for stale in self._stale_temp_dbs(live_db):
            stale.unlink()

        tmp_db = live_db.with_name(f"{live_db.name}.tmp-{os.getpid()}")
        # Mirror the live store's config so the swapped-in DB is built by the
        # same model/dim/vec settings (and thus carries the matching identity).
        tmp_store = IndexStore(
            db_path=tmp_db,
            dim=self.store.dim,
            use_sqlite_vec=self.store.use_sqlite_vec,
            embedding_model=self.store.embedding_model,
        )
        try:
            tmp_store.init_schema()
            for path in _iter_memory_files(self.memory_root):
                try:
                    content_hash = _hash_file(path)
                    # One atomic write per file into the TEMP store; hash + chunks
                    # commit together (#18). The live index is untouched.
                    n = self._index_one(path, content_hash, store=tmp_store)
                except ValidationError as exc:
                    # parse_file fails before any store write, so there is nothing
                    # to roll back; record WHY so the caller can warn loudly
                    # instead of leaving the user a silent near-empty index.
                    stats.files_skipped += 1
                    stats.skipped_files.append((str(path), _skip_reason(exc)))
                    continue
                stats.files_indexed += 1
                stats.chunks_written += n
            # Build succeeded. Close BOTH connections before the swap so neither
            # holds an open handle/sidecar across the replace, then atomically
            # move the complete temp DB over the live one.
            tmp_store.close()
            self.store.conn.close()
            os.replace(tmp_db, live_db)
            # A concurrent reader (e.g. `rekol search`) holding index.db-wal open
            # blocks our close()'s checkpoint, so the OLD -wal/-shm sidecars can
            # survive the swap. Left in place, SQLite would replay those stale
            # frames onto the freshly-swapped-in DB on the next open → silent
            # corruption. The temp DB was checkpointed clean on close (all of its
            # data lives in the main file), so these sidecars belong only to the
            # old index — delete them. A reader that still holds them open keeps
            # its now-unlinked inodes, so its view stays consistent until it closes.
            for _sidecar in (
                live_db.with_name(live_db.name + "-wal"),
                live_db.with_name(live_db.name + "-shm"),
            ):
                _sidecar.unlink(missing_ok=True)
        except BaseException:
            # Any failure (embed error, kill-injected exception, swap error)
            # leaves the live index untouched. Drop the partial temp DB and make
            # sure the live store is usable again before propagating.
            tmp_store.close()
            for leftover in self._stale_temp_dbs(live_db):
                leftover.unlink()
            self.store.reopen()
            raise
        # Re-attach the live store to the freshly-swapped-in file (the old handle
        # now points at the unlinked inode), then derive INDEX.md from disk.
        self.store.reopen()
        self._write_index_md()
        self._write_skip_manifest()
        return stats

    def update(self) -> IndexStats:
        """Incremental update: skip unchanged files, remove deleted ones."""
        stats = IndexStats()
        existing = {f["path"]: f for f in self.store.all_files()}
        seen: set[str] = set()
        for path in _iter_memory_files(self.memory_root):
            seen.add(str(path))
            current_hash = _hash_file(path)
            prev = existing.get(str(path))
            if prev and prev["content_hash"] == current_hash:
                # File unchanged — skip re-embedding to save time and cost.
                continue
            try:
                # One atomic write per file: hash + chunks commit together, so a
                # crash never leaves a new hash with stale/missing chunks (#18).
                n = self._index_one(path, current_hash)
            except ValidationError as exc:
                # parse_file fails before any store write — nothing to roll back.
                # But if this path was previously indexed and its frontmatter has
                # now broken, drop the stale entry so search never serves chunks
                # under a hash that no longer matches the file on disk.
                if prev is not None:
                    self.store.delete_file(str(path))
                stats.files_skipped += 1
                stats.skipped_files.append((str(path), _skip_reason(exc)))
                continue
            # An embed/store failure inside the atomic write rolls itself back,
            # leaving the OLD hash + OLD chunks intact (this file is re-tried
            # next run), and propagates so the caller sees the error.
            stats.files_indexed += 1
            stats.chunks_written += n
        for old_path in list(existing.keys()):
            if old_path not in seen:
                self.store.delete_file(old_path)
                stats.files_removed += 1
        self._write_index_md()
        self._write_skip_manifest()
        return stats

    def _write_skip_manifest(self) -> None:
        """Persist the count of on-disk files currently invisible to search (#123).

        A file rejected at index time stays on disk but out of the index, so it is
        silently unsearchable. Record the FULL disk-vs-index gap — not just this
        run's skips, since an incremental ``update()`` only sees changed files and
        would undercount an already-broken file left untouched. The SessionStart
        hook reads the count from here so it need not walk the store itself.

        Always rewritten (even to 0) so a store that was fixed clears the banner.
        Best-effort: a write failure must never fail the index run it rides on.

        The denominator is the FILESYSTEM, minus the paths excluded on purpose —
        not ``_iter_memory_files`` (#158). Using the walk here made the banner
        report "1 memory file invisible" when the true number was 10: the nine
        files the walk never visited could not appear in a set derived from that
        same walk. A file outside the indexer's scope is exactly the case this
        banner exists to surface, so it must be counted by something that does not
        share the scope.
        """
        try:
            indexed = {f["path"] for f in self.store.all_files()}
            uncovered = sorted(
                self._relpath(path)
                for path in iter_all_markdown(self.memory_root)
                if str(path) not in indexed and not is_non_memory(path, self.memory_root)
            )
            self.index_dir.mkdir(parents=True, exist_ok=True)
            (self.index_dir / SKIP_MANIFEST_NAME).write_text(
                json.dumps({"count": len(uncovered), "paths": uncovered})
            )
        except OSError:
            return  # cache write failure is non-fatal — the index itself is fine

    def _relpath(self, path: Path) -> str:
        """Path relative to the store root for a compact manifest, else absolute."""
        try:
            return str(path.relative_to(self.memory_root))
        except ValueError:
            return str(path)

    def _write_index_md(self) -> None:  # noqa: C901  # complex but stable; refactor tracked separately
        """Regenerate ``INDEX.md`` FROM THE STORE: tag → files, alias → file, listing.

        Groups files by their top-level directory name (``always``, ``when``,
        ``topics``, ``knowledge``) rather than by the ``type`` frontmatter field.
        The directory names are intentionally plural (e.g. ``topics/``), while
        the type field value is singular (``topic``).

        C5: the source is :meth:`IndexStore.all_files_for_index_md` — the
        just-built index — NOT a second filesystem walk + re-``parse_file``. This
        eliminates the TOCTOU drift where INDEX.md could disagree with the index
        (a file skipped at index time for bad frontmatter is absent from the
        store, hence absent from INDEX.md, by construction) and halves the I/O.

        Written into ``self.index_dir`` — the local-only cache outside
        ``$REKOL_HOME`` — so it is treated as derived state and never synced.
        ``REKOL.md`` (always-on, hand-curated) is the only index file the
        memory_root carries.
        """
        tag_to_files: dict[str, list[str]] = {}
        alias_to_file: dict[str, str] = {}
        per_layer_by_dir: dict[str, list[tuple[str, str]]] = {d: [] for d in INDEXED_DIRS}
        # Project-scoped memories grouped by slug → list of (name, rel_path).
        per_project: dict[str, list[tuple[str, str]]] = {}

        for record in self.store.all_files_for_index_md():
            # ``path`` was stored as ``str(path)`` (absolute) by the indexer.
            rel = str(Path(record["path"]).relative_to(self.memory_root))
            name = record["name"]
            for t in record["tags"]:
                tag_to_files.setdefault(t, []).append(rel)
            for a in record["aliases"]:
                alias_to_file[a] = rel
            parts = Path(rel).parts
            if parts and parts[0] == PROJECTS_DIR and len(parts) >= 3:
                # projects/<slug>/<layer>/<file>.md
                slug = parts[1]
                per_project.setdefault(slug, []).append((name, rel))
            elif parts and parts[0] in per_layer_by_dir:
                per_layer_by_dir[parts[0]].append((name, rel))

        lines: list[str] = ["# Memory Index", ""]
        lines.append("*Auto-generated by `memory-index`. Do not edit by hand.*")
        lines.append("")
        for layer in INDEXED_DIRS:
            entries = sorted(per_layer_by_dir.get(layer, []))
            if not entries:
                continue
            lines.append(f"## {layer}/")
            lines.append("")
            for name, rel in entries:
                lines.append(f"- **{name}** — [`{rel}`]({rel})")
            lines.append("")
        if per_project:
            lines.append("## projects/")
            lines.append("")
            for slug in sorted(per_project):
                lines.append(f"### {slug}")
                lines.append("")
                for name, rel in sorted(per_project[slug]):
                    lines.append(f"- **{name}** — [`{rel}`]({rel})")
                lines.append("")
        if tag_to_files:
            lines.append("## Tags")
            lines.append("")
            for tag in sorted(tag_to_files):
                files = ", ".join(f"[`{f}`]({f})" for f in sorted(set(tag_to_files[tag])))
                lines.append(f"- `{tag}` → {files}")
            lines.append("")
        if alias_to_file:
            lines.append("## Aliases")
            lines.append("")
            for alias in sorted(alias_to_file):
                rel = alias_to_file[alias]
                lines.append(f"- `{alias}` → [`{rel}`]({rel})")
            lines.append("")

        self.index_dir.mkdir(parents=True, exist_ok=True)
        (self.index_dir / "INDEX.md").write_text("\n".join(lines))
        # Remove any pre-existing INDEX.md from older installs that wrote it
        # inside the synced memory tree (root-level and the legacy .index/ dir).
        for legacy in (
            self.memory_root / "INDEX.md",
            self.memory_root / ".index" / "INDEX.md",
        ):
            # Never delete the file we just wrote (covers the no-cache fallback
            # where index_dir == memory_root/.index).
            if legacy != self.index_dir / "INDEX.md" and legacy.exists():
                legacy.unlink()
