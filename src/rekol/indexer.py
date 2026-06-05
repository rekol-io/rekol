"""Indexer: walk $MEMORY_HOME, embed changed files, write INDEX.md into the cache."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .chunker import chunk_body
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
INDEXED_DIRS = ("always", "when", "topics", "knowledge")
PROJECTS_DIR = "projects"


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

    def _index_one(self, path: Path, content_hash: str) -> int:
        """Parse, embed, and atomically write a single file's row + chunks.

        Embedding (the slow, failure-prone step) happens BEFORE the store is
        touched, so a parse/embed error leaves the index completely untouched.
        The files-row hash and the chunks are then written in ONE transaction
        via ``replace_file_and_chunks`` — the hash never advances ahead of the
        chunks, so a crash mid-write re-indexes cleanly next run (the #18 fix).

        Returns:
            Number of chunk rows written.
        """
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
        self.store.replace_file_and_chunks(
            str(path),
            mtime,
            content_hash,
            records,
            created=mf.created,
            updated=mf.updated,
            valid_from=mf.valid_from,
            invalidated_at=mf.invalidated_at,
        )
        return len(records)

    def rebuild(self) -> IndexStats:
        """Drop all existing index data and reindex every file from scratch."""
        stats = IndexStats()
        for f in self.store.all_files():
            self.store.delete_file(f["path"])
        for path in _iter_memory_files(self.memory_root):
            try:
                content_hash = _hash_file(path)
                # One atomic write per file: hash + chunks commit together, so a
                # crash never leaves a new hash with stale/missing chunks (#18).
                n = self._index_one(path, content_hash)
            except ValidationError as exc:
                # parse_file fails before any store write, so there is nothing to
                # roll back; just record WHY so the caller can warn loudly
                # instead of leaving the user a silent near-empty index.
                stats.files_skipped += 1
                stats.skipped_files.append((str(path), _skip_reason(exc)))
                continue
            # An embed/store failure inside the atomic write rolls itself back
            # (the `with conn:` block) and propagates — nothing was committed for
            # this file, so there is no orphan files row to clean up.
            stats.files_indexed += 1
            stats.chunks_written += n
        self._write_index_md()
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
        return stats

    def _write_index_md(self) -> None:  # noqa: C901  # complex but stable; refactor tracked separately
        """Regenerate ``INDEX.md``: tag → files, alias → file, per-layer listing.

        Groups files by their top-level directory name (``always``, ``when``,
        ``topics``, ``knowledge``) rather than by the ``type`` frontmatter field.
        The directory names are intentionally plural (e.g. ``topics/``), while
        the type field value is singular (``topic``).

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

        for path in _iter_memory_files(self.memory_root):
            try:
                mf = parse_file(path)
            except ValidationError:
                continue
            rel = str(path.relative_to(self.memory_root))
            for t in mf.tags:
                tag_to_files.setdefault(t, []).append(rel)
            for a in mf.aliases:
                alias_to_file[a] = rel
            parts = Path(rel).parts
            if parts and parts[0] == PROJECTS_DIR and len(parts) >= 3:
                # projects/<slug>/<layer>/<file>.md
                slug = parts[1]
                per_project.setdefault(slug, []).append((mf.name, rel))
            elif parts and parts[0] in per_layer_by_dir:
                per_layer_by_dir[parts[0]].append((mf.name, rel))

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
