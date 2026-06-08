"""Include = scope: discover knowledge dirs, govern them with a deny-list.

This is the engine behind T8 (#63). It does three pure-ish jobs, all I/O-light
and unit-testable against a sandbox tree:

1. **DISCOVER** candidate knowledge directories under a root, with an always-on
   **auto-junk-filter** (``node_modules``/``.git``/``build``/``dist``/``vendor``/
   ``venv``/``.venv`` and friends) so "include All" is clean rather than 5,000
   files of build noise. The per-dir ``.md`` count is the *coverage signal* the
   onboarding UI ranks the list on. The walk is **capped** (reusing the
   early-exit pattern from ``onboarding/detect.py``) so a huge tree never hangs
   the first-run flow.

2. **GOVERN** which files under an included dir are *in scope* via a user
   **deny-list** of globs (reusing :func:`rekol.config.path_is_excluded`) on top
   of the always-on junk filter. "Exclude a folder -> everything under it out."

3. **ENUMERATE** the in-scope text files (``iter_scoped_files``) that ongoing
   indexing converts into synthetic transcripts.

No prompts, no writes here — the CLI layer (``session-index``, ``doctor``) owns
side effects. ``docs_convert`` owns the file->transcript conversion this feeds.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from rekol.config import path_is_excluded
from rekol.docs_convert import TEXT_EXTENSIONS
from rekol.docs_convert.extract import is_text_native

# Directory names that are NEVER knowledge — build artifacts, vendored deps, VCS
# metadata, virtualenvs. Matched by exact path SEGMENT (not substring) so a real
# folder like ``my-build-notes`` is not swallowed. This is the "auto-junk-filter"
# the spec mandates so "include All" is clean. Kept as a frozenset for O(1)
# membership against each path part.
JUNK_DIR_NAMES: frozenset[str] = frozenset(
    {
        "node_modules",
        ".git",
        ".hg",
        ".svn",
        "build",
        "dist",
        "vendor",
        "venv",
        ".venv",
        "__pycache__",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".next",
        ".nuxt",
        "target",  # rust/java build output
        ".idea",
        ".vscode",
        "site-packages",
        ".cache",
        ".terraform",
        ".gradle",
        "bower_components",
        ".eggs",
    }
)

# Default cap on how many files the discovery/scope walk inspects before
# stopping. Mirrors ``onboarding.detect.count_claude_transcripts`` (10_000):
# discovery drives a ranked list + a coverage signal, not a precise census, so
# stopping once we've seen "a lot" is correct and keeps onboarding snappy on a
# network-synced home with hundreds of thousands of files.
DEFAULT_SCAN_CAP = 10_000


@dataclass(frozen=True)
class DiscoveredDir:
    """A candidate knowledge directory surfaced by discovery.

    ``md_count`` is the headline coverage signal (markdown is the dominant
    hand-authored knowledge format); ``text_count`` is every indexable text file
    (``md`` included) so a dir of ``.txt``/``.py`` notes still ranks. ``path`` is
    the directory holding the files (the nearest folder, not the root).
    """

    path: Path
    md_count: int
    text_count: int


def _has_junk_segment(path: Path, root: Path) -> bool:
    """True when any path segment BELOW ``root`` is an auto-junk dir name.

    We test only the segments under ``root`` so a junk-named ancestor of the
    chosen root (e.g. the user deliberately includes ``~/build/notes``) does not
    nuke their own selection — junk filtering is about what we find *inside* a
    scope, not about the scope's own location.
    """
    try:
        rel = path.relative_to(root)
    except ValueError:
        # Not under root (symlink escape, etc.) — treat as junk-free here; the
        # caller's walk only yields paths under root anyway.
        return any(part in JUNK_DIR_NAMES for part in path.parts)
    return any(part in JUNK_DIR_NAMES for part in rel.parts)


def _iter_text_files_capped(root: Path, max_files: int) -> Iterator[Path]:
    """Yield indexable text files under ``root``, junk-filtered and capped.

    Walks lazily and stops after inspecting ``max_files`` files total (inspected,
    not yielded) so the cost is bounded regardless of tree size. ``OSError`` from
    a vanished/permission-denied entry during the walk is swallowed per-file —
    one bad node never aborts discovery.
    """
    if not root.is_dir():
        return
    inspected = 0
    # rglob defaults to recurse_symlinks=False (py3.13+) / does not follow dir
    # symlinks (py3.11), so there is no symlink-loop risk here.
    for path in root.rglob("*"):
        if inspected >= max_files:
            return
        try:
            if not path.is_file():
                continue
        except OSError:
            continue
        inspected += 1
        if _has_junk_segment(path, root):
            continue
        if is_text_native(path):
            yield path


def discover_knowledge_dirs(root: Path, max_files: int = DEFAULT_SCAN_CAP) -> list[DiscoveredDir]:
    """Discover candidate knowledge dirs under ``root``, ranked by ``.md`` count.

    Groups every in-scope (junk-filtered) text file by its PARENT directory and
    returns one :class:`DiscoveredDir` per group, sorted by ``md_count`` desc then
    ``text_count`` desc then path (deterministic). The ``.md`` count is the
    coverage signal the onboarding UI ranks/groups on. A missing or empty root
    yields ``[]`` (silent-skip, per the spec's "never 'Found 0'" rule).

    The walk is capped at ``max_files`` so a huge or network-synced tree never
    hangs the first-run flow; the counts are then a lower bound, which is the
    right tradeoff for a ranking signal.
    """
    md_by_dir: dict[Path, int] = {}
    text_by_dir: dict[Path, int] = {}
    for file_path in _iter_text_files_capped(root, max_files):
        parent = file_path.parent
        text_by_dir[parent] = text_by_dir.get(parent, 0) + 1
        if file_path.suffix.lstrip(".").lower() == "md":
            md_by_dir[parent] = md_by_dir.get(parent, 0) + 1
    discovered = [
        DiscoveredDir(path=d, md_count=md_by_dir.get(d, 0), text_count=text_by_dir[d])
        for d in text_by_dir
    ]
    discovered.sort(key=lambda d: (-d.md_count, -d.text_count, str(d.path)))
    return discovered


def file_in_scope(path: Path, root: Path, deny: list[str]) -> bool:
    """True when ``path`` (under ``root``) is in indexing scope.

    A file is in scope iff it is NOT inside an auto-junk dir AND NOT matched by
    any user ``deny`` glob. The deny match reuses
    :func:`rekol.config.path_is_excluded`, so a bare segment like ``vendored``
    excludes the whole subtree ("exclude a folder -> everything under it out"),
    matching the rest of rekol's exclude semantics. The junk filter is always on,
    independent of the user deny-list.
    """
    if _has_junk_segment(path, root):
        return False
    return not path_is_excluded(str(path), deny)


def iter_scoped_files(
    root: Path, deny: list[str], max_files: int = DEFAULT_SCAN_CAP
) -> Iterator[Path]:
    """Yield every in-scope, indexable text file under ``root``.

    The enumeration ongoing indexing consumes: junk-filtered, deny-governed,
    text-native only, capped. Lazy so the caller (the convert step) can stream.
    A missing root yields nothing.
    """
    for file_path in _iter_text_files_capped(root, max_files):
        if path_is_excluded(str(file_path), deny):
            continue
        yield file_path


__all__ = [
    "DEFAULT_SCAN_CAP",
    "JUNK_DIR_NAMES",
    "TEXT_EXTENSIONS",
    "DiscoveredDir",
    "discover_knowledge_dirs",
    "file_in_scope",
    "iter_scoped_files",
]
