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

import contextlib
import errno
import os
import re
import sqlite3
import stat
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path

import click

from rekol.config import Config, load_config, resolve_claude_config_dir
from rekol.embeddings import BaseEmbedder, get_embedder
from rekol.include_coverage import compute_coverage
from rekol.indexer import (
    INDEXED_DIRS,
    NON_MEMORY_DIRS,
    NON_MEMORY_DIRS_DISPLAY,
    NON_MEMORY_FILES,
    PROJECTS_DIR,
    _iter_memory_files,
    _skip_reason,
    is_in_indexed_layer,
    is_non_memory,
    iter_all_markdown,
)
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


class _CuratedFiles(Enum):
    """Whether there is any curated memory to index.

    THREE states, not two (same lesson as #231's prior-index check). Collapsing
    "cannot tell" into "none" is what turns an unreadable memory home into a
    reassuring first-run line, which is the defect this check is being fixed to
    stop making.
    """

    NONE = "none"  # readable, and holds nothing the indexer would index
    SOME = "some"  # holds at least one file the indexer would index
    # Markdown IS there, but all of it outside the layers the indexer walks, so
    # a rebuild would produce an empty index. Reporting this as NONE would be
    # true about the index and useless to the user, who has written memory that
    # nothing will ever read (Codex, 2026-11-06).
    OUTSIDE_THE_LAYERS = "outside"
    UNKNOWN = "unknown"  # could not be read, so none of the above is known


def _indexer_entry_points(memory_home: Path, on_error: Callable[[OSError], None]) -> list[Path]:
    """Every directory the indexer resolves BY NAME, and so follows a link at.

    This list IS the indexer's link policy, read off ``_iter_memory_files``:
    each of these paths is reached by naming it and calling ``is_dir()``, which
    follows a symlink, and everything below it is reached by
    ``glob("**/*.md")``, which does not. So a link AT one of these names is
    followed and a link anywhere deeper is not.
    """
    # ORDERED MOST-SPECIFIC-FIRST, and the order is load-bearing. The caller
    # walks these before the root and prunes by inode, so whichever entry point
    # reaches an inode first decides how its files are classified. An entry
    # point that makes a path INDEXABLE therefore has to claim it before one
    # that makes the same path OUTSIDE.
    #
    # The first version listed `projects/` before the per-slug layers, which
    # fixed the top-level alias and left the same bug one level down (#253,
    # found by review 2026-11-08): with `projects/a/topics -> projects/a/notes`,
    # the `projects/` walk met the file as `projects/a/notes/s.md`, classified it
    # correctly as outside the layers under THAT name, and claimed the inode — so
    # the `projects/a/topics` walk was pruned and doctor said
    # OUTSIDE_THE_LAYERS while the indexer read it through `topics`. Doctor then
    # advised moving files that were already indexed.
    root_layers = [memory_home / sub for sub in INDEXED_DIRS]
    projects_root = memory_home / PROJECTS_DIR
    try:
        slugs = sorted(projects_root.iterdir())
    except OSError as exc:
        # An absent `projects/` is the ordinary case, not a failure to read.
        if exc.errno not in (errno.ENOENT, errno.ENOTDIR):
            on_error(exc)
        return [*root_layers, projects_root]

    slug_dirs: list[Path] = []
    slug_layers: list[Path] = []
    for slug_dir in slugs:
        try:
            if not slug_dir.is_dir():
                continue
        except OSError as exc:  # is_dir() can raise EACCES on 3.11
            on_error(exc)
            continue
        slug_dirs.append(slug_dir)
        slug_layers.extend(slug_dir / sub for sub in INDEXED_DIRS)

    # Indexable-claiming first (root layers, then per-slug layers), then the
    # containers whose walks can only produce `outside` classifications.
    return [*root_layers, *slug_layers, projects_root, *slug_dirs]


def _count_markdown_by_scope(
    memory_home: Path, on_error: Callable[[OSError], None]
) -> tuple[int, int]:
    """``(indexable, outside)`` markdown counts under ``memory_home``.

    Split out of :func:`_curated_memory_file_state` only to keep that function
    under the complexity limit; the reasoning lives in its docstring.

    LINKS ARE FOLLOWED EXACTLY WHERE THE INDEXER FOLLOWS THEM, and the history
    is a correction in each direction:

    * ``followlinks=False`` was too narrow. The indexer reaches a symlinked
      LAYER (``home/topics`` pointing elsewhere), so a walk refusing to follow
      it counted ZERO and reported "nothing to index" for a home whose memory
      was sitting right there — memory silently dark on an unbuilt index, which
      is the worst direction to be wrong in (Codex, 2026-11-07).
    * ``followlinks=True`` was then too wide, in a way that is wrong in the
      other direction and so looks harmless until you read the remedy. It also
      follows a link NESTED INSIDE a layer, which ``Path.glob("**/*.md")`` never
      does. With only ``home/topics/linked-subdir -> external/``, doctor said
      SOME and the indexer reached nothing, so doctor's advice to run
      ``rekol index rebuild`` could not work: the user would be told their
      memory is there and a rebuild would keep producing an empty index
      (Codex + product, 2026-11-07). Doctor's job here is to AGREE with the
      indexer, not to be more generous than it.

    So the walk itself never follows a link, and it is run once per entry point
    from :func:`_indexer_entry_points` — the places the indexer names, which are
    the places it follows one. That a memory reachable only through a nested
    link goes unindexed with nothing saying so is a real gap, filed separately
    (#247): it is not doctor's to paper over by disagreeing with the indexer.

    Note what this does NOT change: ``is_in_indexed_layer`` is a predicate on
    path components and knows nothing about links. It is asked only about files
    this walk actually found, so the two together answer "reachable AND in
    scope"; the predicate alone answers only the second half.
    """
    # Keyed on (device, inode) rather than on the path, so the same directory
    # reached twice — by the root walk and then as an entry point, or through
    # `home/topics/loop -> home/topics` — is counted once. This is what makes
    # running one walk per entry point safe rather than double-counting.
    visited: set[tuple[int, int]] = set()

    def _ignore_absent(exc: OSError) -> None:
        # An entry point that does not exist is the ordinary case, not a
        # failure to read one. Without this, walking `home/topics` on a home
        # that has no `topics/` would set `walk_failed` and report UNKNOWN for
        # every empty home — a check that answers "cannot tell" always is worth
        # no more than one that answers "fine" always.
        # ELOOP alongside ENOENT/ENOTDIR: a self-looping entry point
        # (`topics -> topics`) is absent as far as the indexer is concerned,
        # because `Path.is_dir()` swallows the OSError and returns False. Left
        # out, it reached `on_error` and made the whole answer UNKNOWN for a home
        # whose other layers held perfectly readable memory (review,
        # 2026-11-08). Matching the indexer is the whole contract of this walk.
        if exc.errno in (errno.ENOENT, errno.ENOTDIR, errno.ELOOP):
            return
        on_error(exc)

    # THE ENTRY POINTS FIRST, THEN THE HOME, and the order is the fix for an
    # ALIAS (QA, 2026-11-07). With `home/topics -> home/projects/shared`, the
    # root walk reached `projects/shared/s.md` by its OTHER name, classified it
    # as outside the layers (which `projects/shared/...` is), and marked the
    # inode visited — so the `topics` entry-point walk was then pruned and the
    # file was never counted as indexable. Doctor said OUTSIDE_THE_LAYERS while
    # the indexer reached it through `topics`, and doctor's advice there was to
    # move files that are already indexed.
    #
    # Walking the entry points first means the indexable NAME claims the inode,
    # and the root walk skips the alias. That is the right precedence for the
    # same reason the list exists: the entry points are what the indexer
    # resolves, so when one inode has two names, the indexer's name wins.
    indexable = 0
    outside = 0
    for entry_point in _indexer_entry_points(memory_home, on_error):
        more_indexable, more_outside = _count_tree(
            entry_point, memory_home=memory_home, visited=visited, on_error=_ignore_absent
        )
        indexable += more_indexable
        outside += more_outside
    # The home last: root-level markdown, and anything under `projects/` that no
    # entry point already claimed.
    more_indexable, more_outside = _count_tree(
        memory_home, memory_home=memory_home, visited=visited, on_error=on_error
    )
    return indexable + more_indexable, outside + more_outside


def _count_tree(
    top: Path,
    *,
    memory_home: Path,
    visited: set[tuple[int, int]],
    on_error: Callable[[OSError], None],
) -> tuple[int, int]:
    """``(indexable, outside)`` for one tree, never following a link below ``top``."""
    indexable = 0
    outside = 0
    for dirpath, dirnames, filenames in os.walk(top, onerror=on_error, followlinks=False):
        here, elsewhere = _count_one_directory(
            dirpath,
            dirnames,
            filenames,
            memory_home=memory_home,
            visited=visited,
            on_error=on_error,
        )
        indexable += here
        outside += elsewhere
    return indexable, outside


def _count_one_directory(
    dirpath: str,
    dirnames: list[str],
    filenames: list[str],
    *,
    memory_home: Path,
    visited: set[tuple[int, int]],
    on_error: Callable[[OSError], None],
) -> tuple[int, int]:
    """Count one directory's markdown, and prune what the indexer never reads.

    Mutates ``dirnames`` (that is how ``os.walk`` is told not to descend) and
    ``visited``. Split out of :func:`_count_markdown_by_scope` only to clear the
    complexity limit.
    """
    indexable = 0
    outside = 0
    try:
        entry = os.stat(dirpath)
    except OSError as exc:
        on_error(exc)
        dirnames[:] = []
        return 0, 0
    key = (entry.st_dev, entry.st_ino)
    if key in visited:
        dirnames[:] = []
        return 0, 0
    visited.add(key)

    if os.path.samestat(entry, os.stat(memory_home)):
        # PRUNE WHAT THE INDEXER NEVER READS, BEFORE DESCENDING (Codex).
        # `tasks/` is the machine-managed task layer, excluded by design — but an
        # unreadable one still tripped `onerror`, so a home whose curated memory
        # is perfectly fine reported UNKNOWN because of a directory the answer
        # does not depend on. Pruned only at the root, which is where the
        # exclusion applies (`rel.parts[0]`).
        #
        # Only the layer dirs and `projects/` can hold anything indexable, so
        # nothing else is descended into — markdown sitting at the root is still
        # seen by the file loop below. A hidden directory INSIDE a layer is kept:
        # the indexer's globs match there, so `always/.old/x.md` really is
        # indexed.
        dirnames[:] = [
            d
            for d in dirnames
            if d not in NON_MEMORY_DIRS and (d in INDEXED_DIRS or d == PROJECTS_DIR)
        ]
    for filename in filenames:
        if not filename.endswith(".md"):
            continue
        path = Path(dirpath) / filename
        if is_non_memory(path, memory_home):
            continue
        if is_in_indexed_layer(path, memory_home):
            indexable += 1
        elif not filename.startswith("."):
            # OUTSIDE the layers, and NOT hidden. A root `.draft.md` is not
            # something the user is waiting to see in search: `iter_all_markdown`
            # excludes hidden paths, so reporting it as unreachable memory was
            # noise on an otherwise empty home (Codex). Hidden files INSIDE a
            # layer are a different question and are counted as indexable above,
            # because the indexer's globs really do match them — the two rules
            # disagree on purpose.
            outside += 1
    return indexable, outside


def _curated_memory_file_state(memory_home: Path) -> tuple[_CuratedFiles, int]:
    """``(state, count)``; the count is meaningful for ``SOME``/``OUTSIDE_THE_LAYERS``.

    THE QUESTION IS "would a rebuild index anything", so the answer has to use
    the indexer's own scope. It used to count every ``.md`` under the home that
    was not hidden and not deliberately excluded, which is a different question:
    the indexer walks the layer dirs and ``projects/``, not the whole tree. A
    home holding only a stray ``note.md`` at its root, or only ``tasks/t.md``,
    therefore read as SOME — "search returns nothing for your files", with
    ``rekol index rebuild`` as the remedy, which would have built an empty index
    and changed nothing (Codex, 2026-11-06).

    ``is_in_indexed_layer`` is the indexer's scope as a predicate, pinned to the
    real walk by a property test. A predicate rather than the walk itself
    because this needs ``os.walk``'s ``onerror`` to SEE an unreadable directory;
    ``glob`` swallows that, which is #232.

    Every disagreement between this and the indexer has been the same fault
    wearing a different hat — a symlinked layer, a hidden file, a dangling
    pointer — so they are now checked TOGETHER, as a table of shapes run through
    both this function and the real ``_iter_memory_files`` and required to
    agree. That table is the test that matters here.
    """
    try:
        if not memory_home.is_dir():
            return _CuratedFiles.UNKNOWN, 0
        # Listing is not traversal (#231): a directory can be listable while its
        # children cannot be opened, so entries are stat'd rather than only
        # named.
        #
        # ONLY THE ENTRIES THE ANSWER DEPENDS ON, which is the fix for a home
        # whose `MEMORY.md` is a dangling pointer (Codex, 2026-11-07). This
        # probed `next(iterdir(), None)` — whichever name the filesystem
        # happened to return first — so a dangling retirement pointer made the
        # whole check report UNKNOWN, "a missing answer", about a home that was
        # perfectly readable. The entries that decide the answer are the layer
        # dirs and `projects/`; a broken pointer at the root is not one of them.
        for layer in (*INDEXED_DIRS, PROJECTS_DIR):
            candidate = memory_home / layer
            if not (candidate.is_symlink() or candidate.exists()):
                continue
            try:
                candidate.stat()  # follows the link on purpose: the indexer does
            except OSError as exc:
                # A DANGLING OR LOOPING LAYER LINK IS ABSENT, NOT UNKNOWABLE
                # (review, 2026-11-08). `stat` raises ENOENT on a link to
                # nothing and ELOOP on `topics -> topics`, and that used to make
                # the whole check answer UNKNOWN — so a home holding
                # `always/s.md` beside a broken `topics` link reported "cannot be
                # inspected" when it could have said SOME, and the indexer read
                # the file perfectly well.
                #
                # The indexer's own `is_dir()` returns False for both, silently,
                # so treating them as absent is what AGREES with it. The comment
                # above says "the indexer does" follow the link, and for these
                # two cases that was simply wrong. A dangling `projects/<slug>`
                # was already handled this way, so the two are consistent now.
                if exc.errno in (errno.ENOENT, errno.ELOOP, errno.ENOTDIR):
                    continue
                raise
    except OSError:
        return _CuratedFiles.UNKNOWN, 0

    # NOT `Path.glob`, which SWALLOWS a PermissionError on a subdirectory and
    # simply yields nothing for it. A readable memory home with one unreadable
    # layer dir therefore counted 0 files and reported "there is no curated
    # memory to index" — a file was there, and the answer was identical to
    # having none. That is the #158 shape: output the same whether or not the
    # thing held. `os.walk`'s onerror makes the failure visible instead.
    walk_failed = False

    def _note_walk_error(_exc: OSError) -> None:
        nonlocal walk_failed
        walk_failed = True

    indexable, outside = _count_markdown_by_scope(memory_home, _note_walk_error)

    if walk_failed:
        return _CuratedFiles.UNKNOWN, 0
    if indexable:
        return _CuratedFiles.SOME, indexable
    if outside:
        return _CuratedFiles.OUTSIDE_THE_LAYERS, outside
    return _CuratedFiles.NONE, 0


def _check_curated_index(cfg: Config, embedder: BaseEmbedder) -> list[Finding]:
    """Inspect the curated ``index.db``: existence, schema, identity, counts, age."""
    findings: list[Finding] = []
    db_path = cfg.index_db_path
    if not db_path.exists():
        # "Not built yet" has three meanings and they are not interchangeable
        # (#232). Reporting all of them as a PROBLEM made `doctor` red out of the
        # box on a machine where nothing was wrong — the third time this project
        # has shipped that, after #165 and #231, and each time it teaches people
        # to stop reading the report.
        #
        # The two checks either side of this one already model it: `transcript
        # archive` prints a neutral line for the same condition, and `session
        # index` only fires when transcripts actually exist. This now matches
        # them.
        curated, curated_count = _curated_memory_file_state(cfg.memory_home)
        if curated is _CuratedFiles.SOME:
            # The real #165 bar: content exists and search cannot reach it.
            findings.append(
                Finding(
                    label="curated index",
                    status=Status.PROBLEM,
                    detail=(
                        f"not built yet (no {db_path}) — but {curated_count} curated "
                        "memory file(s) exist, so searching curated memory returns "
                        "nothing"
                    ),
                    remedy="rekol index rebuild",
                )
            )
        elif curated is _CuratedFiles.OUTSIDE_THE_LAYERS:
            # A rebuild would index nothing, so "rekol index rebuild" is the
            # wrong remedy and "nothing to index" is the wrong reassurance. The
            # files are real and unreachable; say where they have to live.
            findings.append(
                Finding(
                    label="curated index",
                    status=Status.PROBLEM,
                    detail=(
                        f"not built yet (no {db_path}), and the {curated_count} "
                        f"markdown file(s) in {cfg.memory_home} are all OUTSIDE the "
                        f"directories rekol indexes ({', '.join(INDEXED_DIRS)}, "
                        f"{PROJECTS_DIR}/) — a rebuild would index none of them, so "
                        "they are unsearchable wherever the index stands"
                    ),
                    remedy=(
                        f"move them into one of {', '.join(INDEXED_DIRS)} (or "
                        f"{PROJECTS_DIR}/<name>.md), then rekol index rebuild"
                    ),
                )
            )
        elif curated is _CuratedFiles.UNKNOWN:
            # Its own report, because "cannot tell" is not "nothing to index".
            findings.append(
                Finding(
                    label="curated index",
                    status=Status.PROBLEM,
                    detail=(
                        f"not built yet (no {db_path}) and {cfg.memory_home} could "
                        "not be read, so whether there is anything to index is "
                        "UNKNOWN — this is not a first run, it is a missing answer"
                    ),
                    remedy=(
                        f"check that {cfg.memory_home} exists and is readable, then "
                        "rekol index rebuild"
                    ),
                )
            )
        else:
            # Nothing to index: an index would be empty, and `rekol index
            # rebuild` would only silence the line rather than make it true.
            findings.append(
                Finding(
                    label="curated index",
                    status=Status.INFO,
                    detail=(
                        f"not built yet, and there is no curated memory to index in "
                        f"{cfg.memory_home} — it is built when you capture your first "
                        "memory"
                    ),
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


def _check_index_file_migration(memory_home: Path) -> list[Finding]:
    """Report a store holding BOTH ``REKOL.md`` and ``MEMORY.md`` (only one is read).

    `MEMORY.md` -> `REKOL.md` was a rename that shipped without a migration. The
    SessionStart hook resolves `REKOL.md` and falls back to `MEMORY.md`, so the
    fallback *is* the entire migration strategy — and it only fires when REKOL.md
    is absent. Where both exist, MEMORY.md is never read and anything living only
    there is silently unreachable, exactly like the rejected-frontmatter files
    #123 exists to surface.

    Found on a live machine (shipwright, 2026-10-08): both files present, content
    diverged, neither a superset — MEMORY.md held the infra/topic pointers while
    REKOL.md held the always-on rules. A session read one of them and got a
    partial picture with nothing saying so.

    Deliberately NOT flagged:
      - only `MEMORY.md` — the un-migrated state, which the fallback handles
        correctly. Flagging it would fire on every store that has simply not been
        renamed, and a check that fires on the healthy path gets ignored.
      - neither file — reported by the coverage/seeding checks; duplicating it
        here would double one problem into two findings.

    Byte-identical copies are still reported, because one file being silently
    ignored is a state to resolve — but the remedy says "identical", since
    telling someone to merge two identical files wastes their time.
    """
    rekol_md = memory_home / "REKOL.md"
    memory_md = memory_home / "MEMORY.md"
    if not (rekol_md.is_file() and memory_md.is_file()):
        return []

    try:
        rekol_bytes = rekol_md.read_bytes()
        memory_bytes = memory_md.read_bytes()
    except OSError as error:
        # Unreadable is still two files, so still worth reporting — just without
        # the divergence detail. Never swallow it into a clean bill of health.
        return [
            Finding(
                label="index file migration",
                status=Status.PROBLEM,
                detail=(
                    f"both REKOL.md and MEMORY.md exist in {memory_home}; only REKOL.md is "
                    f"read at session start, and they could not be compared ({error})"
                ),
                remedy="compare them by hand, fold anything needed into REKOL.md, remove MEMORY.md",
            )
        ]

    identical = rekol_bytes == memory_bytes
    if identical:
        detail = (
            f"both REKOL.md and MEMORY.md exist in {memory_home} and are byte-identical; "
            f"only REKOL.md is read at session start"
        )
        remedy = "the files are identical, so MEMORY.md is redundant — remove it"
    else:
        detail = (
            f"both REKOL.md and MEMORY.md exist in {memory_home} and their contents DIFFER "
            f"({len(rekol_bytes)} vs {len(memory_bytes)} bytes); only REKOL.md is read at "
            f"session start, so anything living only in MEMORY.md is invisible"
        )
        remedy = (
            "diff them, fold anything you still need from MEMORY.md into REKOL.md, "
            "then remove MEMORY.md"
        )
    return [
        Finding(label="index file migration", status=Status.PROBLEM, detail=detail, remedy=remedy)
    ]


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
    # Name the real list rather than a hardcoded prose copy of it: the previous
    # literal said "(tasks/, MEMORY.md)" and silently went stale when REKOL.md was
    # added, which is how a wrong exclusion list stayed invisible in the output.
    excluded_names = ", ".join([*NON_MEMORY_DIRS_DISPLAY, *NON_MEMORY_FILES])
    suffix = f"; {len(excluded)} deliberately excluded ({excluded_names})" if excluded else ""
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


class _PriorIndex(Enum):
    """What the sessions DB says about whether anything was ever indexed.

    Four states, kept distinct because collapsing them is what hid a real
    failure: returning 0 for a corrupt or unreadable DB made it indistinguishable
    from a fresh machine, so "unknown" was reported as first use (Codex,
    2026-11-06). Unknown is not evidence of absence.
    """

    ABSENT = "absent"  # no DB file at all
    EMPTY = "empty"  # a DB exists and holds no messages
    POPULATED = "populated"  # a DB exists and holds messages
    UNKNOWN = "unknown"  # a DB exists but could not be read


def _prior_index_state(cfg: Config, embedder: BaseEmbedder) -> tuple[_PriorIndex, int]:
    """``(state, message_count)``; the count is meaningful only when POPULATED."""
    db_path = cfg.sessions_db_path
    # INSIDE the error boundary, and by errno — not ``Path.exists()``, whose
    # behaviour here is WORSE THAN EITHER OUTCOME ALONE because it depends on the
    # Python version. MEASURED, both with the same sealed directory:
    #
    #   3.11.15  exists() raises PermissionError  -> doctor dies with a traceback
    #   3.15.0   exists() returns False           -> reads as ABSENT, i.e. "fresh
    #                                               machine, nothing indexed yet"
    #
    # So the same install reports a crash or a reassuring lie depending on which
    # interpreter it is running under. ELOOP is False on both (a self-looping
    # symlink at the DB path reads as absent), which is the silent case with no
    # version dependence at all.
    #
    # ENOENT and ENOTDIR are the two answers that genuinely mean "there is no
    # such file". Everything else means we could not find out, and this enum
    # exists precisely so that can be said rather than rounded to absence.
    try:
        os.stat(db_path)
    except OSError as exc:
        # A DANGLING LINK AT THE DB IS NOT "NOT BUILT YET" (review, 2026-11-08).
        # `os.stat` follows the link, so `sessions.db -> somewhere/gone` gives
        # ENOENT and read as a fresh install — with a remedy, `session-index
        # --full`, that would then fail on the same broken link. The name is
        # there; only the target is missing.
        if _path_exists_but_cannot_be_a_directory(db_path):
            return _PriorIndex.UNKNOWN, 0
        if exc.errno in (errno.ENOENT, errno.ENOTDIR):
            return _PriorIndex.ABSENT, 0
        return _PriorIndex.UNKNOWN, 0
    store: SessionStore | None = None
    try:
        store = SessionStore(db_path=db_path, dim=embedder.dim)
        store.init_schema()
        count = store.count_messages()
    except (sqlite3.DatabaseError, OSError):
        return _PriorIndex.UNKNOWN, 0
    finally:
        # Closed on every path: an open handle on a DB doctor only inspected is
        # a leak, and on Windows it would block the very remedy we print.
        if store is not None:
            with contextlib.suppress(Exception):
                store.close()
    return (_PriorIndex.POPULATED if count > 0 else _PriorIndex.EMPTY), count


def _path_exists_but_cannot_be_a_directory(path: Path) -> bool:
    """True when the NAME is there but can never resolve to a usable directory.

    `os.stat` follows links, so it collapses three different situations into one
    ENOENT/ENOTDIR, and reporting all three as "first use" was neutral on a
    broken install (review, 2026-11-08):

    * the name does not exist at all — genuinely first use, False here;
    * the name EXISTS as a dangling symlink — ENOENT from `stat`, but it is not
      absent, and a relocated `~/.claude` whose target moved is exactly this;
    * a path COMPONENT is a regular file — ENOTDIR, and nothing can `mkdir`
      under a file, so "Claude Code creates it on first use" is false.

    `os.lstat` separates them because it does not follow the final link.
    """
    try:
        os.lstat(path)
    except OSError as exc:
        return exc.errno == errno.ENOTDIR
    return True


def _transcript_source_finding(
    projects_root: Path, cfg: Config, embedder: BaseEmbedder
) -> Finding | None:
    """A problem with the transcript SOURCE itself, or None when it looks usable.

    ``is_dir()`` alone proves type, not access, and its False result conflates
    three very different states — absent, present-but-not-a-directory, and
    present-but-unreadable. Reporting all of them as "does not exist yet" is how
    a real failure hid behind first-run neutrality.

    Which is why the type question is asked with ``os.stat`` INSIDE an error
    boundary rather than with ``exists()``/``is_dir()``. What those two do with
    an access failure depends on the Python version — measured 3.11.15 raises
    ``PermissionError`` while 3.15.0 answers False — so a projects dir whose
    PARENT has no search bit either killed ``doctor`` with a traceback or looked
    exactly like a path that is simply not there. A self-looping symlink answers
    False on both, with no version dependence, and read as absent.

    The probe below separates the answers by errno instead: ENOENT/ENOTDIR is
    really absent, anything else means we could not find out and must say so.
    """
    try:
        source_mode: int | None = os.stat(projects_root).st_mode
    except OSError as exc:
        if exc.errno not in (errno.ENOENT, errno.ENOTDIR):
            return Finding(
                label="transcript source",
                status=Status.PROBLEM,
                detail=(
                    f"claude_projects_dir cannot be inspected: {projects_root} "
                    f"({exc}) — so whether transcripts can be read from it is "
                    "unknown, and session-index fails silently on every session "
                    "end if they cannot"
                ),
                remedy=(
                    f"fix access to {projects_root} and its parent directories "
                    "(each must be traversable), or point claude_projects_dir at "
                    "a readable location"
                ),
            )
        source_mode = None

    if source_mode is not None and not stat.S_ISDIR(source_mode):
        return Finding(
            label="transcript source",
            status=Status.PROBLEM,
            detail=(
                f"claude_projects_dir is not a directory: {projects_root} — "
                "session-index cannot enumerate transcripts, so the index can "
                "never build"
            ),
            remedy=(
                f"remove or rename {projects_root}, or point claude_projects_dir "
                "at Claude Code's projects directory"
            ),
        )

    if source_mode is None:
        # "ABSENT" IS THREE DIFFERENT THINGS, and reporting all three as first
        # use was neutral on a broken install (review, 2026-11-08). `os.stat`
        # follows links and collapses them:
        #   * the name does not exist at all          -> genuinely first use
        #   * the name EXISTS as a dangling symlink   -> ENOENT, but not absent;
        #     a relocated `~/.claude` whose target moved is exactly this shape
        #   * a path COMPONENT is a regular file      -> ENOTDIR, and Claude Code
        #     cannot create the directory, so "creates it on first use" is false
        # `os.lstat` separates them, because it does not follow the final link.
        if _path_exists_but_cannot_be_a_directory(projects_root):
            return Finding(
                label="transcript source",
                status=Status.PROBLEM,
                detail=(
                    f"claude_projects_dir cannot be used: {projects_root} — the "
                    f"path exists but does not resolve to a directory (a broken "
                    f"symlink, or a file where a directory must be), so Claude "
                    f"Code cannot create it and session-index fails on every "
                    f"session end"
                ),
                remedy=(
                    f"inspect {projects_root} — remove or repoint it if it is a "
                    f"broken link, or move the file that is in the way; then run "
                    f"rekol session-index --full"
                ),
            )

        # AT THE DEFAULT LOCATION, absent usually means Claude Code has not
        # created it yet: it does so on first use, and rekol can be installed
        # before that happens. (Measured on a live machine: the directory
        # predated the rekol install by nearly three months, so the ordinary
        # case is that it already exists — but a brand-new machine legitimately
        # hits the gap.) Reporting that as a PROBLEM made `doctor` red out of the
        # box, which this project has shipped twice and which teaches people to
        # ignore the report.
        #
        # Compared against the RESOLVED default, so a relocated
        # `CLAUDE_CONFIG_DIR` counts as the default too.
        at_default_location = projects_root == resolve_claude_config_dir() / "projects"
        if not at_default_location:
            # The user said where it is and it is not there, so `session-index`
            # exits 2 on every SessionEnd — into `>/dev/null 2>&1`, where nobody
            # ever sees it — and the index can NEVER be built. That is #165.
            return Finding(
                label="transcript source",
                status=Status.PROBLEM,
                detail=(
                    f"claude_projects_dir is set to a path that does not exist: "
                    f"{projects_root} — session-index fails on every session end "
                    "and the index can never build"
                ),
                remedy=(
                    "correct claude_projects_dir in rekol.config.yaml, or unset it "
                    "to use Claude Code's default location"
                ),
            )

        # First use and LOST STORAGE are both "absent at the default location",
        # and only the index can tell them apart. A populated sessions DB proves
        # transcripts were being read from here, so the directory did not fail to
        # appear — it went away, and everything indexed from it is now orphaned.
        prior, indexed = _prior_index_state(cfg, embedder)
        if prior is _PriorIndex.POPULATED:
            return Finding(
                label="transcript source",
                status=Status.PROBLEM,
                detail=(
                    f"claude_projects_dir has GONE: {projects_root} does not "
                    f"exist, yet the session index holds {indexed} message(s) — "
                    "so transcripts were indexed at some point and this path is "
                    "no longer there. No new session can be indexed, and searches "
                    "still return the older results"
                ),
                remedy=(
                    "restore the directory, or set claude_projects_dir to its new "
                    "location and run rekol session-index --full"
                ),
            )
        if prior is _PriorIndex.UNKNOWN:
            # Not first use, and not provably lost either. Saying "nothing to
            # index yet" here would be a claim the evidence does not support.
            return Finding(
                label="transcript source",
                status=Status.PROBLEM,
                detail=(
                    f"claude_projects_dir does not exist ({projects_root}) and "
                    f"{cfg.sessions_db_path} could not be read, so whether "
                    "anything was ever indexed from it is UNKNOWN — this is not a "
                    "first run, it is a missing answer"
                ),
                # NOT "rekol doctor reports it below": on this branch the source
                # finding RETURNS, so the session-index checks that would have
                # reported the DB never run. Pointing at a report that is not
                # printed is worse than no remedy — it reads as if the answer is
                # somewhere on screen. Name the file and an action instead.
                remedy=(
                    f"inspect {cfg.sessions_db_path} — if it is corrupt, move it "
                    "aside and run rekol session-index --full to rebuild; if it is "
                    "merely unreadable, fix its permissions. Restore "
                    "claude_projects_dir first if the transcripts are what moved"
                ),
            )
        return Finding(
            label="transcript source",
            status=Status.INFO,
            detail=(
                f"claude_projects_dir does not exist yet: {projects_root} — "
                "Claude Code creates it on first use, so there is nothing to "
                "index until a session has ended"
            ),
        )

    # Present and a directory, but access is a separate question: enumeration is
    # what `session-index` actually does, so that is what gets tested. Treating a
    # PermissionError as "empty" would report a locked-out source as first use.
    # LISTING IS NOT TRAVERSAL (product, 2026-11-06). Mode 0400 lets `iterdir`
    # enumerate names while every child is unopenable, so enumeration alone
    # still reported a locked-out source as healthy. `session-index` has to
    # traverse INTO the slug dirs to read transcripts, so that is what gets
    # tested.
    #
    # EVERY ENTRY, NOT THE FIRST ONE. This stat'd `next(iterdir(), None)` — a
    # single sample — so a PARTIAL lockout read as healthy whenever the
    # unreadable slug was not the one the filesystem happened to return first
    # (review, 2026-11-08). Measured: four slugs with `charlie` at mode 000
    # produced no finding at all, while `Path.glob("**/*.jsonl")` — which is
    # what `sessions/ingest.py` walks — silently returned 3 of 4 transcripts.
    # A whole project's history missing from the index, and doctor said nothing.
    #
    # The same single-sample fault was fixed on the curated side earlier today;
    # this is its sibling, and fixing one and not the other is how it survived.
    # `stat` ON THE CHILD IS NOT ENOUGH EITHER: stat'ing a mode-000 directory
    # SUCCEEDS, because what governs it is the search bit on the PARENT. So a
    # slug the indexer cannot read still looked fine. What `session-index` needs
    # is read+search on each slug, which `os.access` answers in one syscall per
    # entry — cheap enough to do for every slug rather than sampling one.
    try:
        for entry in projects_root.iterdir():
            entry.stat()
            if entry.is_dir() and not os.access(entry, os.R_OK | os.X_OK):
                raise PermissionError(errno.EACCES, f"cannot read or traverse {entry}", str(entry))
    except OSError as exc:
        return Finding(
            label="transcript source",
            status=Status.PROBLEM,
            detail=(
                f"claude_projects_dir cannot be read: {projects_root} ({exc}) — "
                "session-index fails on every session end, and the failure is "
                "discarded, so nothing reports it"
            ),
            remedy=f"fix the permissions on {projects_root} (it must be readable and traversable)",
        )

    return None


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

    # The transcript source must exist before "not built yet" can mean anything.
    # A configured-but-absent projects dir makes `session-index` exit 2 on every
    # SessionEnd — into `>/dev/null 2>&1`, so nobody ever sees it — and the index
    # can then NEVER be built. Reporting that as INFO let `doctor` print "index is
    # healthy" and exit 0 for an install that had never indexed one transcript
    # (#165). It is a PROBLEM, and a distinct one from "nothing to index yet".
    projects_root = cfg.claude_projects_dir
    source_finding = _transcript_source_finding(projects_root, cfg, embedder)
    if source_finding is not None:
        findings.append(source_finding)
        # An INFO here means "nothing to index yet", which is a complete answer:
        # there is no point inspecting an index of a source that has nothing in
        # it. A PROBLEM means the source is unusable, so the downstream checks
        # would only add noise about a cause already reported.
        return findings

    db_path = cfg.sessions_db_path
    # Same reason as in _prior_index_state: exists() cannot tell "absent" from
    # "cannot look", and the absent branch below says "not built yet", which for
    # an unreadable DB is a false statement about a working install.
    try:
        os.stat(db_path)
        db_absent = False
    except OSError as exc:
        if exc.errno not in (errno.ENOENT, errno.ENOTDIR):
            findings.append(
                Finding(
                    label="session index",
                    status=Status.PROBLEM,
                    detail=(
                        f"cannot inspect {db_path} ({exc}) — so whether the "
                        "session index exists at all is unknown, and session "
                        "search cannot be relied on"
                    ),
                    remedy=(
                        f"fix access to {db_path} and its parent directories, "
                        "then run rekol doctor again"
                    ),
                )
            )
            return findings
        db_absent = True

    # THE SAME lstat SPLIT AS THE SOURCE CHECK, applied to the check a user
    # actually reads (QA, 2026-11-08). Fixing `_prior_index_state` was not
    # enough: this is the visible `session index` finding, and for
    # `sessions.db -> somewhere/gone` it printed "not built yet" with the remedy
    # `rekol session-index --full` — a remedy that **crashes**. Measured: with
    # the link target's parent missing, that command exits 1 with
    # `sqlite3.OperationalError: unable to open database file` and a traceback.
    # (With the parent present SQLite creates the file through the link instead,
    # so the crash depends on the shape; both are reported the same way now,
    # because in neither case is "not built yet" true.)
    #
    # My test for the earlier half asserted on `_prior_index_state()`, a helper,
    # rather than on what doctor prints — the same mistake a review caught in
    # #224 hours earlier: the helper returning the right answer is not the claim
    # that the user sees the right thing.
    if db_absent and _path_exists_but_cannot_be_a_directory(db_path):
        findings.append(
            Finding(
                label="session index",
                status=Status.PROBLEM,
                detail=(
                    f"{db_path} exists but does not resolve — it is a broken "
                    f"symlink, so the session index cannot be read or rebuilt "
                    f"through it, and session search returns nothing"
                ),
                remedy=(
                    f"remove or repoint {db_path} (it points at a path that is "
                    f"not there), then run rekol session-index --full"
                ),
            )
        )
        return findings

    if db_absent:
        # Only INFO when there is genuinely nothing to index. If transcripts are
        # sitting there unindexed, the feature is silently not working and saying
        # "healthy" would be false.
        has_transcripts = any(projects_root.glob("*/*.jsonl"))
        findings.append(
            Finding(
                label="session index",
                status=Status.PROBLEM if has_transcripts else Status.INFO,
                detail=(
                    f"not built yet (no {db_path})"
                    + (" — but transcripts exist and are unsearchable" if has_transcripts else "")
                ),
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
        # Hook executability now has its own unconditional check
        # (_check_hook_executability) so it also runs when no manifest exists
        # (#215). Kept here only to avoid changing this finding's shape.
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


def _check_update_staleness(cfg: Config) -> list[Finding]:
    """Has the update check actually managed to run? — the backstop (#27).

    The check must soft-fail so it can never break a session — which
    reintroduces silent failure one level up: a checker that has been broken for
    months looks exactly like one that has nothing to report. This is the
    backstop, and it keys on the last SUCCESS rather than on the state file
    existing, because a file written by a permanently failing check would
    otherwise read as health.

    Never a PROBLEM. Not knowing whether a newer version exists does not make
    the installation unhealthy, and a doctor that exits 1 over it would train
    people to ignore a non-zero exit.
    """
    import datetime as _dt

    from rekol.release import STALE_AFTER_DAYS, last_success, read_state

    if not getattr(cfg, "update_check", True):
        return [
            Finding(
                label="update check",
                status=Status.INFO,
                detail="disabled (update_check: false)",
            )
        ]

    seen = last_success(read_state(cfg.index_dir))
    if seen is None:
        return [
            Finding(
                label="update check",
                status=Status.INFO,
                detail="never completed — cannot tell whether a newer version exists",
                remedy="rekol update --force",
            )
        ]
    age = (_dt.datetime.now() - seen).days
    if age >= STALE_AFTER_DAYS:
        return [
            Finding(
                label="update check",
                status=Status.INFO,
                detail=(
                    f"no successful check in {age} days (last {seen:%Y-%m-%d}) — "
                    "the checking hook may be broken"
                ),
                remedy="rekol update --force",
            )
        ]
    return [
        Finding(
            label="update check",
            status=Status.OK,
            detail=f"last successful {seen:%Y-%m-%d} ({age} day(s) ago)",
        )
    ]


def _rekol_scripts_on_path(path_value: str | None = None) -> list[Path]:
    """Every executable named ``rekol`` on PATH, in resolution order, deduped.

    Deliberately NOT ``shutil.which``, which answers only "what would run here".
    The whole point of #203 is the entries AFTER the first: a working wrapper
    early on PATH hides a broken console script later, and a hook with different
    PATH order resolves the broken one.
    """
    raw = os.environ.get("PATH", "") if path_value is None else path_value
    found: list[Path] = []
    seen: set[str] = set()
    for directory in raw.split(os.pathsep):
        if not directory:
            continue
        candidate = Path(directory) / "rekol"
        if not os.access(candidate, os.X_OK) or candidate.is_dir():
            continue
        # Dedupe on the real path: a symlinked ~/bin/rekol and the venv script it
        # points at are one program, and reporting it twice reads like two faults.
        real = str(candidate.resolve())
        if real in seen:
            continue
        seen.add(real)
        found.append(candidate)
    return found


def _check_hook_executability() -> list[Finding]:
    """Is the fallback path recorded in each wired hook command runnable? (#215).

    Each wired hook command renders as::

        "$(command -v rekol || echo '<recorded absolute path>')" _hook <handler>

    This verifies that recorded fallback is runnable. It does **not** verify which
    executable the hook resolves: `command -v rekol` is evaluated in the hook's
    environment, wins whenever it succeeds, and the fallback is reached only when
    it fails. So a passing result here does not mean the hook runs the right
    program — only that the recorded path is not itself broken.

    WHY IT IS ITS OWN CHECK: `unrunnable_hooks()` has existed since the #159 work,
    but doctor only reached it from inside `_check_install_drift`, which returns
    early when there is no install manifest. So on any machine without one —
    an install predating manifest recording, or a REKOL_HOME the installer was
    never pointed at — wired-but-dead hooks were never probed at all, even though
    probing them needs nothing from the manifest (#215). It depends only on
    settings.json, so it runs unconditionally.
    """
    from rekol.update import load_settings, unrunnable_hooks

    settings = load_settings()
    if not (settings.get("hooks") or {}):
        return [
            Finding(
                label="hook executability",
                status=Status.INFO,
                detail="no hooks registered in settings.json — nothing to probe",
            )
        ]

    broken = unrunnable_hooks(settings)
    if not broken:
        return [
            Finding(
                label="hook executability",
                status=Status.OK,
                detail=(
                    "every wired hook's recorded fallback path is runnable "
                    "(not a check of which executable a hook resolves)"
                ),
            )
        ]
    lines = "; ".join(f"{name} — {why}" for name, why in broken)
    return [
        Finding(
            label="hook executability",
            status=Status.PROBLEM,
            detail=(
                f"{len(broken)} wired hook(s) have a recorded fallback that cannot run: "
                f"{lines}. Note this is the fallback only — a hook prefers whatever "
                "`command -v rekol` finds in its own PATH, which is not checked here."
            ),
            remedy="./install.sh   (re-renders hook commands with a working path)",
        )
    ]


def _check_hook_resolution_order() -> list[Finding]:
    """Does any wired hook still resolve PATH before the install's own binary? (#221).

    THE COMPANION TO AN UPGRADE THAT CANNOT REACH EVERYTHING. The installer
    rewrites legacy hook commands to the recorded-path-first rendering, but it
    only rewrites forms it recognises — and QA found one it did not (an unquoted
    ``echo <path>``, which is in Leon's own file on three handlers). Product's
    ruling: a form left behind must not also be silent.

    This is the bug #203 is about, so it deserves its own line rather than being
    folded into `_check_hook_executability`. That check asks "is the recorded
    fallback runnable?" and explicitly says it does **not** verify which
    executable a hook resolves. This one asks exactly that, and they can disagree
    — a hook can have a perfectly runnable fallback and still prefer an orphaned
    `rekol` on PATH, which is the whole of #203.
    """
    from rekol.update import load_settings, path_first_hooks

    settings = load_settings()
    if not (settings.get("hooks") or {}):
        return [
            Finding(
                label="hook resolution order",
                status=Status.INFO,
                detail="no hooks registered in settings.json — nothing to check",
            )
        ]

    stale = path_first_hooks(settings)
    if not stale:
        return [
            Finding(
                label="hook resolution order",
                status=Status.OK,
                detail=(
                    "every rekol hook this check recognises tries the install's "
                    "recorded binary before PATH — a PATH-first command whose path "
                    "is not one rekol writes is left alone and not inspected"
                ),
            )
        ]
    lines = "; ".join(f"{name} — {form}" for name, form in stale)
    return [
        Finding(
            label="hook resolution order",
            status=Status.PROBLEM,
            detail=(
                f"{len(stale)} wired hook(s) still resolve PATH FIRST, so an orphaned "
                f"`rekol` earlier on PATH runs instead of this install: {lines}. "
                f"Re-running the installer upgrades the forms it recognises; if a "
                f"hook is still listed here afterwards, its command is in a shape "
                f"the upgrade does not own and has to be edited by hand."
            ),
            remedy="./install.sh   (re-renders hook commands recorded-path-first)",
        )
    ]


class _ScriptVerdict(Enum):
    """What static inspection can honestly conclude about a `rekol` on PATH."""

    RESOLVES = "resolves"
    ORPHAN = "orphan"
    UNCLASSIFIED = "unclassified"
    CANT_TELL = "cant-tell"


_PY_SHEBANG_RE = re.compile(r"^python(\d+(\.\d+)?)?$")


def _site_packages_for(interpreter: Path) -> tuple[Path | None, str]:
    """Guess an interpreter's site-packages WITHOUT running it.

    Layout guesswork, and it says so: venv, framework, pyenv, Debian
    ``dist-packages`` and user-site all differ. When the directory cannot be
    located the answer is "cannot tell", never "absent" — reading cannot-tell as
    absent is the mistake behind QA's F3 and it is what would produce removal
    advice for a perfectly good install.
    """
    name = interpreter.name
    prefix = interpreter.parent.parent
    match = _PY_SHEBANG_RE.match(name)
    if match and match.group(1) and "." in match.group(1):
        candidate = prefix / "lib" / f"python{match.group(1)}" / "site-packages"
        if candidate.is_dir():
            return candidate, ""
        return None, f"no site-packages at {candidate}"
    # Unversioned (`python3`): the version is not in the name, so glob — but only
    # an unambiguous single match is evidence.
    try:
        matches = sorted((prefix / "lib").glob("python3.*/site-packages"))
    except OSError as exc:
        return None, f"could not read {prefix / 'lib'}: {exc}"
    live = [m for m in matches if m.is_dir()]
    if len(live) == 1:
        return live[0], ""
    if not live:
        return None, f"no python3.*/site-packages under {prefix / 'lib'}"
    return None, f"{len(live)} candidate site-packages under {prefix / 'lib'} — ambiguous"


def _editable_verdict(site_packages: Path) -> tuple[_ScriptVerdict, str] | None:
    """Weigh EVERY matching ``.pth`` before deciding, or None when there are none.

    An editable install has no ``rekol/`` in site-packages — only a ``.pth`` — so
    ``dist-info`` proves a package was installed, NOT that it resolves. The real
    #203 orphan was exactly that: ``dist-info`` present, ``.pth`` naming a
    worktree that had been deleted.

    EVIDENCE IS AGGREGATED, not taken from whichever file sorts first. An earlier
    version returned on the first ``.pth`` it read, so a stale leftover file
    condemned a healthy install standing right beside it — removal advice for a
    working editable install, decided by filename order.

    Precedence:

    1. any target resolving ``rekol/__init__.py`` wins — RESOLVES;
    2. otherwise any inconclusive form (a setuptools *finder* ``.pth`` whose
       import hook we decline to execute or reimplement, an unreadable file, a
       relative path that does not resolve from here, or a file naming no path at
       all) — CANT_TELL, no remedy;
    3. ORPHAN only when every file gave conclusive absolute-path evidence and
       none of it resolved.

    So removal advice requires that nothing anywhere was ambiguous.
    """
    try:
        pths = sorted(site_packages.glob("__editable__*.pth")) + sorted(
            site_packages.glob("*rekol*.pth")
        )
    except OSError as exc:
        return _ScriptVerdict.CANT_TELL, f"could not list {site_packages}: {exc}"
    if not pths:
        return None

    inconclusive: list[str] = []
    absolute_misses: list[Path] = []
    for pth in pths:
        try:
            lines = [
                line.strip()
                for line in pth.read_text(encoding="utf-8", errors="replace").splitlines()
                if line.strip()
            ]
        except OSError as exc:
            inconclusive.append(f"{pth.name} is unreadable ({exc})")
            continue

        absolute = [Path(line) for line in lines if line.startswith("/")]
        relative = [
            site_packages / line
            for line in lines
            if not line.startswith("/") and not line.startswith(("import ", "import\t"))
        ]
        # Rule 1 outranks everything, including an import hook in the same file.
        if any((target / "rekol" / "__init__.py").is_file() for target in absolute + relative):
            return _ScriptVerdict.RESOLVES, f"editable install resolves via {pth.name}"

        if any(line.startswith(("import ", "import\t")) for line in lines):
            inconclusive.append(f"{pth.name} resolves rekol through an import hook")
            continue
        if not absolute and not relative:
            inconclusive.append(f"{pth.name} names no path")
            continue
        if relative:
            inconclusive.append(f"{pth.name} names a relative path that does not resolve here")
        absolute_misses.extend(absolute)

    if inconclusive:
        return _ScriptVerdict.CANT_TELL, "; ".join(inconclusive)
    if absolute_misses:
        listed = ", ".join(str(target) for target in absolute_misses)
        return (
            _ScriptVerdict.ORPHAN,
            f"editable install points at {listed}, none of which has rekol/__init__.py",
        )
    return None


def _classify_rekol_script(script: Path) -> tuple[_ScriptVerdict, str]:
    """Decide what a `rekol` on PATH is, by reading files only — never executing.

    Executing an arbitrary program named ``rekol`` is not a safe health probe:
    ``--version`` is not a boundary, it can mutate state, read stdin or spawn
    children, and bounding it per-candidate still lets a scripted ``doctor`` stall
    for minutes. So nothing here runs the candidate.

    The verdicts are deliberately four, not two. Only ORPHAN authorizes removal
    advice, and it requires exact evidence — a missing Python interpreter, or a
    site-packages that was read and does not resolve ``rekol``. Everything else
    is reported without a remedy, because telling someone to delete a file on a
    guess is worse than staying quiet.
    """
    try:
        with script.open("rb") as handle:
            first = handle.readline(512).decode("utf-8", "replace").strip()
    except OSError as exc:
        return _ScriptVerdict.CANT_TELL, f"could not read it: {exc}"

    if not first.startswith("#!"):
        return _ScriptVerdict.UNCLASSIFIED, "no shebang (binary or wrapper)"

    parts = first[2:].strip().split()
    if not parts:
        return _ScriptVerdict.UNCLASSIFIED, "empty shebang"
    interpreter = Path(parts[0])

    # `#!/usr/bin/env python3` and `#!/usr/bin/env bash` both resolve their real
    # interpreter through PATH at exec time, so which one runs is not knowable
    # statically. The shipped `bin/rekol` shim is `#!/usr/bin/env bash`: treating
    # "bash has no site-packages" as "rekol absent" would advise deleting the
    # user's own working shim.
    if interpreter.name == "env":
        return (
            _ScriptVerdict.UNCLASSIFIED,
            f"interpreter resolved via PATH at exec time ({first[2:].strip()})",
        )
    if not _PY_SHEBANG_RE.match(interpreter.name):
        return _ScriptVerdict.UNCLASSIFIED, f"not a Python shebang ({interpreter})"

    if not interpreter.exists():
        return _ScriptVerdict.ORPHAN, f"its interpreter {interpreter} no longer exists"

    site_packages, why = _site_packages_for(interpreter)
    if site_packages is None:
        return _ScriptVerdict.CANT_TELL, why

    if (site_packages / "rekol" / "__init__.py").is_file():
        return _ScriptVerdict.RESOLVES, f"rekol resolves in {site_packages}"

    # An EDITABLE install has no `rekol/` in site-packages — only a .pth pointing
    # at the source tree — so dist-info alone proves it was installed, not that it
    # resolves. The real #203 orphan was exactly this: dist-info present, .pth
    # pointing at a worktree that had been deleted.
    editable = _editable_verdict(site_packages)
    if editable is not None:
        return editable

    return _ScriptVerdict.ORPHAN, f"rekol is not importable from {site_packages}"


def _check_console_scripts(path_value: str | None = None) -> list[Finding]:
    """Every `rekol` on PATH, classified by STATIC inspection only (#203).

    A console script can be left pointing at an interpreter where the package no
    longer resolves, and every call through it dies with
    ``ModuleNotFoundError: No module named 'rekol'``. An interactive shell
    resolves the working wrapper first, so ``command -v rekol`` is green and this
    report passed — while a hook, whose PATH order differs, resolves the orphan
    and the session starts with no memory.

    LIMIT, stated here because a green line must not be read as more than it is:
    this sees only the PATH of the process running ``doctor``. A hook runs with a
    different PATH, so an entry present only there is invisible from here — and
    nothing in this module establishes which executable a hook resolves. The
    companion check verifies only that the recorded fallback is runnable, which is
    a different question.
    """
    scripts = _rekol_scripts_on_path(path_value)
    if not scripts:
        return [
            Finding(
                label="rekol on PATH",
                status=Status.INFO,
                detail="no `rekol` found on PATH; hooks fall back to the recorded absolute path",
            )
        ]

    orphans: list[tuple[Path, str]] = []
    unknown: list[tuple[Path, str]] = []
    resolved = 0
    for script in scripts:
        verdict, why = _classify_rekol_script(script)
        if verdict is _ScriptVerdict.RESOLVES:
            resolved += 1
        elif verdict is _ScriptVerdict.ORPHAN:
            orphans.append((script, why))
        else:
            unknown.append((script, why))

    if orphans:
        listed = "; ".join(f"{path} — {why}" for path, why in orphans)
        return [
            Finding(
                label="rekol on PATH",
                status=Status.PROBLEM,
                detail=(
                    f"{len(orphans)} of {len(scripts)} `rekol` on PATH cannot resolve the "
                    f"package: {listed}. Your shell may resolve a working one first, but a "
                    "hook with different PATH order can resolve one of these and start a "
                    "session with no memory."
                ),
                remedy=(
                    "Remove the script(s) named above — they belong to a Python environment "
                    "where rekol is no longer resolvable. Check with `which -a rekol`."
                ),
            )
        ]

    detail = f"{resolved} of {len(scripts)} on PATH resolve rekol"
    if unknown:
        detail += "; not classified: " + "; ".join(f"{p} — {w}" for p, w in unknown)
    detail += (
        ". Only this process's PATH was inspected — an entry present only on a hook's "
        "PATH is not visible here."
    )
    return [
        Finding(
            label="rekol on PATH",
            # INFO, not OK, when something could not be classified: a green tick
            # over an unclassified entry would read as "all clear".
            status=Status.INFO if unknown else Status.OK,
            detail=detail,
        )
    ]


def _check_version_metadata() -> list[Finding]:
    """``rekol --version`` reads installed metadata, not the code being run.

    ``cli.py`` uses ``@click.version_option(package_name="rekol")``, which
    reports the version recorded in the installed distribution's metadata. For an
    **editable** install that metadata is a snapshot from install time, so it
    keeps reporting the version that was current when `pip install -e` last ran
    while the source tree moves on.

    This is not cosmetic: it is the number a user pastes into a bug report. On the
    machine this check was written for, `rekol --version` said 0.5.5 while the
    code being executed was 0.6.1 — six patch releases apart, and it misled a
    reader for days.

    INFO rather than PROBLEM on purpose: a drifting editable install is the normal
    state of a development checkout, and making doctor red for it would train
    people to ignore the report — which is the failure this project keeps hitting.
    """
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import version as dist_version

    from rekol import __version__ as code_version

    try:
        metadata_version = dist_version("rekol")
    except PackageNotFoundError:
        return [
            Finding(
                label="version reporting",
                status=Status.INFO,
                detail=(
                    f"code is {code_version}; no installed distribution metadata found, "
                    "so `rekol --version` may fail or report nothing"
                ),
            )
        ]

    if metadata_version == code_version:
        return [
            Finding(
                label="version reporting",
                status=Status.OK,
                detail=f"`rekol --version` and the running code agree ({code_version})",
            )
        ]

    return [
        Finding(
            label="version reporting",
            status=Status.INFO,
            detail=(
                f"`rekol --version` reports {metadata_version} but the code being run is "
                f"{code_version}. The running code is authoritative; --version reads "
                "install-time metadata, which an editable install does not refresh."
            ),
            remedy=(
                "Reinstall to resync the metadata if the reported version matters "
                "(e.g. before quoting it in a bug report)."
            ),
        )
    ]


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
    findings.extend(_check_index_file_migration(cfg.memory_home))
    findings.extend(_check_curated_index(cfg, embedder))
    findings.extend(_check_curated_coverage(cfg, embedder))
    findings.extend(_check_session_index(cfg, embedder))
    findings.extend(_check_archive(cfg))
    findings.extend(_check_include_scope(cfg))
    findings.extend(_check_install_drift(cfg))
    # #203: both of these look at the ENVIRONMENT rather than the stores, so they
    # are the only checks here that can see a break in the hook path. Wired
    # unconditionally (not behind --deep) because the failure they catch is
    # invisible to every other check and silent at runtime.
    findings.extend(_check_hook_executability())
    findings.extend(_check_hook_resolution_order())
    findings.extend(_check_console_scripts())
    findings.extend(_check_version_metadata())
    findings.extend(_check_update_staleness(cfg))
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
