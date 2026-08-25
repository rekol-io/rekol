"""Orchestrator: discovery → classification → write → archive → pointer.

Operates on ONE source dir at a time (``migrate_dir``).  The CLI layer wraps
multiple calls for ``auto`` mode.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import frontmatter
import yaml

from rekol.config import resolve_index_dir
from rekol.migrate.archive import archive_file, write_retirement_pointer
from rekol.migrate.classify import Classification, classify_file
from rekol.migrate.discover import (
    discover_files_in_dir,
    has_migration_marker,
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
    """Tally of migration outcomes, including per-reason skip counts and errors."""

    migrated: int = 0
    would_migrate: int = 0  # dry-run only
    by_heuristic: int = 0
    by_llm: int = 0
    # Counted separately from by_heuristic (#166): a defaulted file is one we
    # learned nothing about, not one we classified. Folding it into by_heuristic
    # is what let a total failure print as a successful migration.
    by_defaulted: int = 0
    archived: int = 0
    skipped_retired: int = 0
    skipped_missing: int = 0
    skipped_duplicate: int = 0  # body-hash matched an already-migrated file
    errors: list[str] = field(default_factory=list)


def _body_hash(body: str) -> str:
    """SHA-256 of the body text, whitespace-normalized.

    Used to dedupe legacy files whose bodies are byte-identical but whose
    auto-generated frontmatter (e.g., ``description: Legacy memory from <slug>``)
    differs only because of the source path.  Same body → same hash → migrate
    only once.
    """
    normalized = "\n".join(line.rstrip() for line in body.strip().splitlines())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _existing_body_hashes(memory_home: Path) -> set[str]:
    """Return the set of body hashes for every file already in ``memory_home``."""
    hashes: set[str] = set()
    for layer in LAYER_DIR_MAP.values():
        layer_dir = memory_home / layer
        if not layer_dir.is_dir():
            continue
        for entry in layer_dir.iterdir():
            if entry.suffix != ".md" or not entry.is_file():
                continue
            try:
                post = frontmatter.load(entry)
                hashes.add(_body_hash(post.content))
            except Exception:  # noqa: BLE001 — best-effort; skip unreadable
                continue
    return hashes


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


def migrate_dir(  # noqa: C901  # complex but stable; refactor tracked separately
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

    if has_migration_marker(source_dir):
        report.skipped_retired = 1
        return report

    files = discover_files_in_dir(source_dir)
    if not files:
        # Still write the retirement pointer so re-runs skip cleanly.
        if not dry_run:
            write_retirement_pointer(source_dir, memory_home=memory_home)
        return report

    # INDEX.md now lives in the local-only cache (outside $REKOL_HOME); fall
    # back to the legacy in-tree locations for compatibility with already-
    # deployed setups that haven't been relocated yet.
    index_candidates = [
        resolve_index_dir(memory_home) / "INDEX.md",
        memory_home / ".index" / "INDEX.md",
        memory_home / "INDEX.md",
    ]
    index_context = ""
    for candidate in index_candidates:
        if candidate.exists():
            index_context = candidate.read_text(encoding="utf-8", errors="replace")
            break

    # Snapshot of body hashes already present in memory_home — drives dedup
    # below.  Updated as we migrate so duplicates within this batch also skip.
    seen_hashes = _existing_body_hashes(memory_home)

    for lf in files:
        try:
            c = classify_file(lf, index_context=index_context, allow_llm=allow_llm)
        except Exception as exc:  # noqa: BLE001 — log and continue
            report.errors.append(f"classify failed for {lf.source_path}: {exc}")
            continue

        body_hash = _body_hash(c.body)
        if body_hash in seen_hashes:
            # Same body already migrated — archive the duplicate but don't write.
            report.skipped_duplicate += 1
            if not dry_run:
                try:
                    archive_file(lf)
                    report.archived += 1
                except Exception as exc:  # noqa: BLE001
                    report.errors.append(f"archive failed for duplicate {lf.source_path}: {exc}")
            continue

        if dry_run:
            report.would_migrate += 1
            if c.method == "heuristic":
                report.by_heuristic += 1
            elif c.method == "defaulted":
                report.by_defaulted += 1
                if c.fallback_reason:
                    report.errors.append(f"{lf.source_path}: {c.fallback_reason}")
            else:
                report.by_llm += 1
            seen_hashes.add(body_hash)
            continue

        target = _resolve_target_path(memory_home, c)
        try:
            target.write_text(_serialize_memory_file(c))
            archive_file(lf)
            seen_hashes.add(body_hash)
            report.migrated += 1
            report.archived += 1
            if c.method == "heuristic":
                report.by_heuristic += 1
            elif c.method == "defaulted":
                report.by_defaulted += 1
                if c.fallback_reason:
                    report.errors.append(f"{lf.source_path}: {c.fallback_reason}")
            else:
                report.by_llm += 1
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"write/archive failed for {lf.source_path}: {exc}")

    # Write the migration marker if anything was processed — even a fully-failed
    # batch.  Without this, a source dir whose every file fails classification
    # (e.g., corrupted YAML on every file) would be retried on every install,
    # accumulating identical errors forever.  Trade-off: the operator must read
    # the install journal / report.errors to discover the failure rather than
    # noticing repeated retry attempts.  Acceptable, because errors counts ARE
    # surfaced in the install output.  The dir is NOT marked when zero files
    # were even attempted (handled by the earlier early-return when files == [])
    # or when dry-running.
    # Tombstone ONLY on genuine progress (#166). `len(report.errors) > 0` used to
    # count as progress, so a run where EVERY file failed still marked the source
    # dir retired — and a re-run then prints "skipped — already retired" and
    # refuses to try again. The files stay on disk, un-migrated, and rekol never
    # looks at them, which is permanent abandonment reported as success.
    #
    # The original worry (a dir whose every file fails is retried on every install,
    # accumulating identical errors forever) is real but strictly less bad than
    # silently abandoning the user's memory: a repeated attempt is visible and
    # recoverable, an unnoticed tombstone is neither.
    progressed = report.migrated > 0 or report.skipped_duplicate > 0
    if not dry_run and progressed:
        write_retirement_pointer(source_dir, memory_home=memory_home)

    return report
