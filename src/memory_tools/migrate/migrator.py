"""Orchestrator: discovery → classification → write → archive → pointer.

Operates on ONE source dir at a time (``migrate_dir``).  The CLI layer wraps
multiple calls for ``auto`` mode.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List

import yaml

from memory_tools.migrate.archive import archive_file, write_retirement_pointer
from memory_tools.migrate.classify import Classification, classify_file
from memory_tools.migrate.discover import (
    LegacyFile,
    discover_files_in_dir,
    is_retirement_pointer,
)


# Map singular layer name (in Classification.layer) to on-disk dir name.
LAYER_DIR_MAP = {
    "always": "always",
    "when": "when",
    "topic": "topics",
    "knowledge": "knowledge",
}


@dataclass
class MigrationReport:
    migrated: int = 0
    would_migrate: int = 0           # dry-run only
    by_heuristic: int = 0
    by_llm: int = 0
    archived: int = 0
    skipped_retired: int = 0
    skipped_missing: int = 0
    errors: List[str] = field(default_factory=list)


def _serialize_memory_file(classification: Classification) -> str:
    """Render a Classification as a markdown string with YAML frontmatter."""
    fm = yaml.safe_dump(
        classification.frontmatter,
        sort_keys=False,
        default_flow_style=False,
    ).strip()
    return f"---\n{fm}\n---\n\n{classification.body.strip()}\n"


def _resolve_target_path(
    memory_home: Path,
    classification: Classification,
) -> Path:
    """Return the unique target path.  Appends -N suffix if a file already exists."""
    layer_dir = memory_home / LAYER_DIR_MAP[classification.layer]
    layer_dir.mkdir(parents=True, exist_ok=True)
    base = classification.target_filename
    stem = base[:-3] if base.endswith(".md") else base
    candidate = layer_dir / f"{stem}.md"
    counter = 2
    while candidate.exists():
        candidate = layer_dir / f"{stem}-{counter}.md"
        counter += 1
    return candidate


def migrate_dir(
    source_dir: Path,
    *,
    memory_home: Path,
    dry_run: bool,
    allow_llm: bool,
) -> MigrationReport:
    """Migrate every legacy .md file in ``source_dir`` into ``memory_home``.

    Skips entirely if ``source_dir/MEMORY.md`` is already a retirement pointer
    or if ``source_dir`` does not exist.

    When ``dry_run`` is True, no filesystem writes occur; ``would_migrate`` is
    populated but ``migrated`` stays 0.
    """
    report = MigrationReport()

    if not source_dir.is_dir():
        report.skipped_missing = 1
        return report

    memory_md = source_dir / "MEMORY.md"
    if is_retirement_pointer(memory_md):
        report.skipped_retired = 1
        return report

    files = discover_files_in_dir(source_dir)
    if not files:
        # Still write the retirement pointer so re-runs skip cleanly.
        if not dry_run:
            write_retirement_pointer(source_dir, memory_home=memory_home)
        return report

    index_md = memory_home / "INDEX.md"
    index_context = index_md.read_text(encoding="utf-8", errors="replace") \
        if index_md.exists() else ""

    for lf in files:
        try:
            c = classify_file(lf, index_context=index_context, allow_llm=allow_llm)
        except Exception as exc:  # noqa: BLE001 — log and continue
            report.errors.append(f"classify failed for {lf.source_path}: {exc}")
            continue

        if dry_run:
            report.would_migrate += 1
            if c.method == "heuristic":
                report.by_heuristic += 1
            else:
                report.by_llm += 1
            continue

        target = _resolve_target_path(memory_home, c)
        try:
            target.write_text(_serialize_memory_file(c))
            archive_file(lf)
            report.migrated += 1
            report.archived += 1
            if c.method == "heuristic":
                report.by_heuristic += 1
            else:
                report.by_llm += 1
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"write/archive failed for {lf.source_path}: {exc}")

    if not dry_run and report.migrated > 0:
        write_retirement_pointer(source_dir, memory_home=memory_home)

    return report
