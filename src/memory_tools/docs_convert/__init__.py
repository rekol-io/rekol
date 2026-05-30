"""docs_convert: turn an arbitrary text-file tree into synthetic Claude Code JSONL.

The synthetic transcripts are written under ``claude_projects_dir`` so the
existing ``claude-session-index`` ingester surfaces them in ``memory-search``
with no changes to the ingester or store.
"""

# Extensions read as plain UTF-8 text. .jsonl is intentionally NOT here — its
# extension collides with this tool's own output format and source .jsonl files
# are not transcript-shaped (they would parse as malformed transcript rows).
TEXT_EXTENSIONS: frozenset[str] = frozenset(
    {"md", "txt", "log", "csv", "tsv", "json", "py", "sh", "xml", "yaml", "yml"}
)

from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class ConvertStats:
    folders_seen: int = 0
    jsonl_written: int = 0
    files_converted: int = 0
    files_skipped_unsupported: int = 0
    files_skipped_empty: int = 0
    files_skipped_too_large: int = 0
    errors: int = 0

    def as_line(self) -> str:
        return (
            f"folders_seen={self.folders_seen} "
            f"jsonl_written={self.jsonl_written} "
            f"files_converted={self.files_converted} "
            f"files_skipped_unsupported={self.files_skipped_unsupported} "
            f"files_skipped_empty={self.files_skipped_empty} "
            f"files_skipped_too_large={self.files_skipped_too_large} "
            f"errors={self.errors}"
        )


def convert_tree(
    source_dir: Path,
    target_dir: Path,
    prefix: str,
    max_bytes: int,
    dry_run: bool = False,
) -> ConvertStats:
    """Walk source_dir, extract text, build rows, write one .jsonl per session.

    Returns a ConvertStats accounting of every file's disposition. When
    ``dry_run`` is True, stats report what WOULD be written but nothing is
    written to disk. ``files_skipped_unsupported`` counts every non-text-native
    file found anywhere under the tree (so the user sees the real drop rate).
    """
    # Local imports avoid a circular import at module load (submodules import
    # TEXT_EXTENSIONS from this package).
    from .walk import group_sessions
    from .extract import extract_text, is_text_native
    from .transcript import build_rows
    from .writer import write_sessions

    source_dir = Path(source_dir)
    stats = ConvertStats()

    # Count unsupported files across the whole tree (informational).
    for p in source_dir.rglob("*"):
        if p.is_file() and not is_text_native(p):
            stats.files_skipped_unsupported += 1

    groups = group_sessions(source_dir)
    # folders_seen = immediate child directories (the unit a session maps to).
    stats.folders_seen = sum(1 for c in source_dir.iterdir() if c.is_dir())

    rows_by_session: dict[str, list[dict]] = {}
    for group in groups:
        texts: dict[str, str] = {}
        for entry in group.files:
            try:
                text: Optional[str] = extract_text(entry.path, max_bytes=max_bytes)
            except Exception:
                stats.errors += 1
                continue
            if text is None:
                # Distinguish too-large from empty for honest accounting.
                try:
                    if entry.path.stat().st_size > max_bytes:
                        stats.files_skipped_too_large += 1
                    else:
                        stats.files_skipped_empty += 1
                except OSError:
                    stats.errors += 1
                continue
            texts[entry.rel_to_session] = text
        rows = build_rows(group, prefix=prefix, texts=texts)
        if rows:
            rows_by_session[group.session_name] = rows
            stats.files_converted += len(rows)

    stats.jsonl_written = sum(1 for rows in rows_by_session.values() if rows)

    if not dry_run:
        write_sessions(target_dir, prefix=prefix, rows_by_session=rows_by_session)

    return stats
