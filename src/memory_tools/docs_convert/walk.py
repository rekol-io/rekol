"""Group a source tree into sessions by immediate-child directory.

A session is each immediate child directory of SOURCE_DIR; its messages are all
text-native files found recursively beneath it. Files placed directly under
SOURCE_DIR are grouped into a synthetic "_root" session. A child directory with
no text-native files anywhere beneath it yields no SessionGroup.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List

from .extract import is_text_native

ROOT_SESSION_NAME = "_root"


@dataclass
class FileEntry:
    path: Path
    rel_to_source: str   # posix, relative to SOURCE_DIR — uuid salt component
    rel_to_session: str  # posix, relative to the session folder — content prefix
    mtime_unix: int


@dataclass
class SessionGroup:
    session_name: str
    folder_abs: str
    rel_to_source: str
    files: List[FileEntry] = field(default_factory=list)


def _entry_for(file_path: Path, source_dir: Path, session_dir: Path) -> FileEntry:
    return FileEntry(
        path=file_path,
        rel_to_source=file_path.relative_to(source_dir).as_posix(),
        rel_to_session=file_path.relative_to(session_dir).as_posix(),
        mtime_unix=int(file_path.stat().st_mtime),
    )


def group_sessions(source_dir: Path, max_bytes: int) -> List[SessionGroup]:
    """Return one SessionGroup per immediate-child dir (plus _root) with text files.

    ``max_bytes`` is accepted for signature symmetry with the rest of the
    pipeline but size-filtering happens in extract_text at conversion time, so a
    file that is too large still appears here and is reported as skipped later.
    Sorting (children alphabetical, files by rel_to_session) makes output
    deterministic.
    """
    source_dir = Path(source_dir)
    groups: List[SessionGroup] = []

    # _root session: text files directly under source_dir (non-recursive)
    root_files = sorted(
        (p for p in source_dir.iterdir() if p.is_file() and is_text_native(p)),
        key=lambda p: p.name,
    )
    if root_files:
        groups.append(SessionGroup(
            session_name=ROOT_SESSION_NAME,
            folder_abs=str(source_dir.resolve()),
            rel_to_source=".",
            files=[_entry_for(p, source_dir, source_dir) for p in root_files],
        ))

    # One session per immediate child directory
    for child in sorted((p for p in source_dir.iterdir() if p.is_dir()), key=lambda p: p.name):
        text_files = sorted(
            (p for p in child.rglob("*") if p.is_file() and is_text_native(p)),
            key=lambda p: p.relative_to(child).as_posix(),
        )
        if not text_files:
            continue
        groups.append(SessionGroup(
            session_name=child.name,
            folder_abs=str(child.resolve()),
            rel_to_source=child.relative_to(source_dir).as_posix(),
            files=[_entry_for(p, source_dir, child) for p in text_files],
        ))

    return groups
